# Portfolio Manager 資料透明度功能需求與實作計畫

**Goal:** 在不引入交易策略、回測、再平衡規則或投資守則的前提下，將目前的 Portfolio Manager MCP 從單一投資組合帳本與即時估值服務，擴充為可稽核、可重現、可跨帳戶彙總的資產資料基礎設施。

**Architecture:** 採用事件／journal 作為資產與現金變動的唯一事實來源，instrument master 作為分類及發行人識別中心，valuation snapshot 保存每日可重現估值，再由 consolidation 層依指定 FX cutoff 產生跨 Portfolio 報表。所有衍生結果必須回傳資料來源、估值時間、換算匯率、分類覆蓋率、警告與計算方法。

**Tech Stack:** 延續既有 Portfolio Manager MCP 技術棧；資料庫 migration、Decimal-safe monetary arithmetic、MCP Tools、排程或可重跑的 snapshot job、既有市場資料 provider。

---

## 1. 背景與產品原則

目前服務已具備：

- Portfolio CRUD；
- completed spot trade 與獨立 cash transaction；
- open positions、moving-average cost、即時估值與損益；
- position tags；
- market instrument、market history、technical snapshot；
- idempotent `request_id`；
- stale price 與 provider warnings。

本期只處理四項資料基礎能力：

1. 合併 Portfolio 與 FX 換算；
2. 每日 NAV snapshot、TWR／XIRR 與 Benchmark；
3. 資產分類、issuer mapping 與 ETF look-through；
4. Corporate Actions 與原子化交易／現金帳本。

### 1.1 明確不在本期範圍

- 交易策略回測；
- 買賣訊號；
- 目標權重、配置區間、再平衡建議；
- 投資政策或交易守則；
- 下單與券商 order execution；
- 稅務最佳化、tax-loss harvesting；
- VaR、情境壓力測試或風險預算；
- 自動判斷投資品質；
- 根據資料自動產生交易決策。

### 1.2 跨模組強制原則

1. **可追溯：** 每個行情、FX、分類、ETF 成分、benchmark 與衍生數字都要包含 source/provider、effective time、fetched time 與 warnings。
2. **可重現：** 所有歷史查詢接受 `as_of` 或 date range；不得用「目前最新資料」靜默重算過去結果。
3. **不靜默猜測：** 缺行情、FX、benchmark total-return data 或分類時，保留 null／unclassified 並回傳 warning。
4. **Decimal-safe：** 金額、數量、價格、匯率及比例在 API 邊界使用 decimal string；不得以 binary float 作為帳務真值。
5. **時區明確：** 儲存 RFC 3339 UTC timestamp；每日 snapshot 同時保留 valuation date、market timezone 與 cutoff policy。
6. **事件不可覆寫：** 已入帳事件不得直接更新或刪除；更正透過 reversal + replacement，並保留完整 audit chain。
7. **向後相容：** 既有 MCP tools 在 migration 後仍可使用；若語意不變，不得靜默改變。例如既有 `record_trade` 目前不動現金，除非明確版本化，否則應保留原語意並新增原子化工具。
8. **讀取結果透明：** 所有 summary 都應提供 `calculation_method`、`valuation_as_of`、`data_coverage` 與 `warnings`。
9. **冪等：** 所有 mutation 與 snapshot jobs 支援 deterministic/idempotent request key。
10. **可重建：** 衍生資料可由 journal、instrument master 與 point-in-time market data 重新計算；snapshot 是可稽核快照，不是第二套事實帳本。

---

# 2. 功能一：合併 Portfolio＋FX 換算

## 2.1 使用情境

使用者可把台股、美股、加密貨幣等多個 Portfolio 組成一個 logical portfolio group，指定報表幣別後查看：

- 總資產、證券價值、現金；
- 各 Portfolio 對總資產的占比；
- 合併後持倉權重；
- 原始幣別金額與換算後金額；
- 幣別曝險；
- 同一 issuer 跨市場、跨 Portfolio 的合併曝險；
- 使用的每一筆 FX rate、時間與來源；
- 未成功換算的金額與覆蓋率。

## 2.2 資料模型

### `portfolio_groups`

- `id`
- `name`
- `reporting_currency`
- `created_at`
- `updated_at`

### `portfolio_group_members`

- `group_id`
- `portfolio_id`
- `effective_from`
- `effective_to` nullable
- unique `(group_id, portfolio_id, effective_from)`

### `fx_rates`

- `base_currency`
- `quote_currency`
- `rate` decimal
- `price_as_of`
- `fetched_at`
- `provider`
- `provider_symbol`
- `adjustment_or_method`
- `is_stale`
- `raw_metadata`／provider response reference
- unique key 應避免同一 provider、pair、cutoff 重複寫入

不得只保存換算結果而不保存使用的 FX rate。

## 2.3 FX 選擇規則

1. 優先使用 valuation cutoff 當下或之前最近可用匯率。
2. 查詢結果須揭露 direct、inverse 或 cross conversion。
3. 若透過 cross currency 換算，須回傳完整 conversion path，例如 `TWD -> USD -> EUR`。
4. 不得使用 valuation cutoff 之後的匯率回填歷史估值。
5. 若只能使用超過 stale threshold 的匯率，保留結果但標示 stale warning；threshold 應由 server config 決定並在結果中揭露。
6. 若無法換算，該項目仍須以原幣顯示，但不得納入已換算總額；回傳 converted coverage ratio。
7. 同幣別換算率固定為 1，method 標示 `identity`。

## 2.4 MCP Tools

### `create_portfolio_group`

輸入：

- `name`
- `reporting_currency`
- `portfolio_ids`

輸出：group metadata、有效 members、warnings。

### `get_portfolio_group`

輸入：`group_id`。

輸出：group metadata、members、effective dates。

### `update_portfolio_group_members`

原子化替換成員清單；需確認所有 Portfolio 存在。不得刪除歷史 effective membership。

### `get_fx_rate`

輸入：

- `base_currency`
- `quote_currency`
- `as_of`

輸出至少包含：

- `rate`
- `conversion_path`
- `provider`
- `price_as_of`
- `fetched_at`
- `is_stale`
- `warnings`

### `get_consolidated_summary`

輸入：

- `group_id`
- `as_of` nullable；省略代表 current valuation
- `reporting_currency` nullable override
- `lookthrough` default false

輸出：

- `group`
- `reporting_currency`
- `portfolio_summaries`
- `positions`
- `cash_by_currency`
- `currency_exposure`
- `issuer_exposure`（有 mapping 才聚合）
- `securities_value`
- `cash_value`
- `total_value`
- `converted_value_coverage_percent`
- `fx_rates_used`
- `valuation_as_of`
- `calculation_method`
- `warnings`

每個 position 必須同時保留：

- local currency／local market value；
- reporting currency／converted market value；
- FX rate、path 與 as-of；
- direct position weight；
- instrument ID 與 issuer ID（若可用）。

## 2.5 驗收標準

- 一個 USD Portfolio 與一個 TWD Portfolio 可在指定 reporting currency 下正確合併。
- direct、inverse、cross FX path 均有單元測試。
- 歷史 `as_of` 不會使用未來匯率。
- 缺少某一 FX pair 時，不會讓整個 summary 失敗；未換算項目與 coverage 會清楚列出。
- group total 等於所有成功換算 positions 和 cash 的 Decimal 總和。
- 可按 issuer 合併不同 ticker／listing 的經濟曝險，但原始 listing position 不消失。
- 所有權重的 denominator 與 excluded unconverted values 都有明確說明。

---

# 3. 功能二：每日 NAV Snapshot＋TWR／XIRR＋Benchmark

## 3.1 定義

本功能只衡量歷史資產價值與績效，不產生策略訊號。

需清楚區分：

- `NAV / total value`：某時點的資產價值；
- `P&L`：帳務損益；
- `TWR`：排除外部現金流影響後的投資績效；
- `XIRR`：考慮外部現金流時間的投資人實際資金報酬；
- `benchmark return`：比較基準報酬；
- `price return` 與 `total return` 不得混用。

## 3.2 資料模型

### `portfolio_valuation_snapshots`

- `id`
- `portfolio_id`
- `valuation_date`
- `valuation_as_of`
- `base_currency`
- `securities_value`
- `cash_value`
- `total_value`
- `external_flow_amount`
- `income_amount`
- `fee_amount`
- `tax_amount`
- `pricing_coverage_percent`
- `calculation_version`
- `status`: complete / partial / failed
- `warnings`
- `created_at`
- unique `(portfolio_id, valuation_date, calculation_version)` 或明確 active revision 設計

### `position_valuation_snapshots`

- `portfolio_snapshot_id`
- `instrument_id`
- `ticker_at_time`
- `quantity`
- `average_cost`
- `local_currency`
- `price`
- `market_value`
- `price_as_of`
- `price_provider`
- `price_stale`
- `classification_snapshot_reference`

### `benchmarks`

- `id`
- `name`
- `benchmark_type`: instrument / blended
- `currency`
- `return_type`: price / total_return
- `constituents` for blended benchmark
- `rebalance_convention` for blended benchmark
- `effective_from` / `effective_to`
- `provider_metadata`

### `portfolio_benchmarks`

- `portfolio_id` or `group_id`
- `benchmark_id`
- `effective_from`
- `effective_to`

Benchmark assignment 需有歷史版本，不能覆寫過去基準。

## 3.3 Snapshot 產生與重建

需要兩種入口：

1. 排程每日建立；
2. 手動指定 date/date range 重建。

MCP／job 介面：

### `create_valuation_snapshot`

輸入：

- `portfolio_id`
- `valuation_date`
- `request_id`
- `force_revision` default false

### `rebuild_valuation_snapshots`

輸入：portfolio、start date、end date、calculation version。應是可恢復、可重跑的 bounded job；回傳 job ID 或結果摘要。

### `get_nav_history`

輸入：portfolio/group、date range、reporting currency optional。

輸出每日 NAV、cash flows、coverage、status、warnings。

規則：

- 同一 date 的正常 retry 必須冪等。
- 若行情或 FX 不完整，snapshot 可為 `partial`，不得用 0 取代未知值。
- 重建後保留 calculation version、原 revision 與差異原因。
- 歷史 position valuation 必須使用該日有效 quantity、corporate actions、價格與 FX。

## 3.4 外部與內部現金流分類

**外部流入／流出：** deposit、withdrawal，以及明確標記為 portfolio 外部來源／去向的 transfer。

**內部事件：** trade settlement、dividend、interest、fee、tax、split、merger、security conversion。這些事件影響 NAV 或資產構成，但不得被當作外部資金投入／提領。

任何無法分類的 legacy cash transaction 都應標示 `flow_classification=unknown`，並使 TWR/XIRR 回傳 warning，而不是自行猜測。

## 3.5 TWR

- 先實作 daily subperiod return，再以幾何方式鏈結。
- 每日 cash flow timing 不完整時，使用文件化且版本化的方法，例如 Modified Dietz daily return。
- API 必須回傳 `method`、`calculation_version`、cash-flow timing convention。
- 不可用 `total_pnl / cost_basis` 冒充 TWR。
- partial snapshot 或 unknown external-flow classification 對報酬結果的影響必須以 coverage/warning 揭露。

### `get_portfolio_performance`

輸入：

- portfolio/group ID
- start date
- end date
- reporting currency optional
- benchmark ID optional

輸出：

- beginning value
- ending value
- external inflows/outflows
- income/fees/taxes informational breakdown
- period TWR
- annualized TWR（期間足夠才回傳）
- XIRR
- benchmark return
- excess return
- daily/monthly series optional
- methodology
- data coverage
- warnings

## 3.6 XIRR

- 使用實際外部 cash flow 日期及期間終值。
- 明確規定符號：投資人投入 Portfolio 為負、從 Portfolio 取回及期末終值為正。
- 使用 actual-day count。
- 無實數解、多重解、期間過短或 cash-flow sign 不成立時，回傳 null + reason，不得拋出不透明的 generic error。
- 演算法與 tolerance 要有固定版本及測試案例。

## 3.7 Benchmark

### `create_benchmark`

支援 instrument benchmark 與固定權重 blended benchmark，但本期不做動態策略 benchmark。

### `assign_portfolio_benchmark`

指定 effective period。

### `get_benchmark_history`

回傳：

- benchmark level/return series；
- currency；
- price vs total return；
- provider；
- actual period；
- data coverage；
- warnings。

規則：

- 優先 total-return data。
- 只有 price-return data 時可以回傳，但必須清楚標示，不得稱為 total return。
- Benchmark 需換算到與 Portfolio 相同 reporting currency，並揭露 FX 方法。
- Benchmark 缺資料不應阻擋 Portfolio 自身績效。

## 3.8 驗收標準

- 無外部現金流時，TWR 與期間 NAV return 一致。
- 有中途大額入金時，TWR 不會被入金扭曲，而 XIRR 反映投入時間。
- dividend 被視為內部收益，不被當作 external inflow。
- 僅有 price benchmark 時回傳明確 warning。
- 歷史 snapshot rebuild 不會使用 cutoff 之後的行情或 FX。
- 同一資料與 calculation version 重跑，結果完全一致。
- partial snapshot、unknown cash flow classification、stale prices 都可在 API output 看到。

---

# 4. 功能三：資產分類、Issuer Mapping＋ETF Look-through

## 4.1 目標

建立 instrument identity 與 exposure taxonomy，使系統不再把所有 Yahoo `quoteType` 類似標的簡化成 `stock`，並可辨認同一發行人在不同 ticker、交易所或 ADR 下的合併曝險。

## 4.2 資料模型

### `issuers`

- `id`
- `legal_name`
- `display_name`
- `country_of_domicile`
- `lei` nullable
- `provider_ids`
- `created_at` / `updated_at`

### `instruments`

- internal stable `instrument_id`
- `canonical_ticker`
- `name`
- `security_type`
- `asset_class`
- `sub_asset_class`
- `issuer_id` nullable
- `listing_market`
- `listing_currency`
- `country_of_risk` nullable
- `is_fund`
- `is_cash_equivalent`
- `active_from` / `active_to`

### `instrument_aliases`

- `instrument_id`
- `provider`
- `provider_symbol`
- `exchange`
- `effective_from` / `effective_to`

### `instrument_classifications`

每個分類欄位需保留：

- value
- source/provider
- effective date/as-of
- fetched at
- confidence nullable
- provenance type: provider / derived / manual_override
- note

優先級：有效 manual override > verified internal mapping > provider > derived heuristic > unclassified。

### `fund_holdings_snapshots`

- `fund_instrument_id`
- `as_of_date`
- `constituent_instrument_id` nullable
- raw constituent symbol/name
- `weight_percent`
- `source`
- `fetched_at`
- `coverage_percent`
- `warnings`

## 4.3 Taxonomy 最低需求

### Asset class

- Equity
- Fixed Income
- Cash
- Cash Equivalent
- Commodity
- Real Estate
- Crypto
- Multi-Asset
- Alternative
- Other / Unclassified

### Security type

至少支援：

- Common Stock
- ADR/GDR
- ETF
- Mutual Fund
- Bond
- Treasury
- Money Market
- Commodity Trust
- REIT
- Crypto Asset
- Stablecoin
- Cash
- Other

`asset_class` 與 `security_type` 必須分開。例如 GLD 可為 `security_type=Commodity Trust/ETF`、`asset_class=Commodity`。

## 4.4 MCP Tools

### `get_instrument_profile`

輸入 ticker/provider alias；輸出 stable instrument ID、issuer、classification、aliases、field-level provenance 與 warnings。

### `set_instrument_classification_override`

手動修正分類；輸入需包含 reason、effective date、request ID。不得破壞 provider 原始資料，override 必須可撤銷並可稽核。

### `map_instrument_issuer`

建立或修正 issuer mapping，需保留 mapping source 和 confidence。

### `get_fund_holdings`

輸入 fund instrument/ticker 與 `as_of`；輸出 actual holdings date、成分、權重、coverage、source、warnings。

### `get_portfolio_exposures`

輸入：

- portfolio/group ID
- `as_of`
- dimension: asset_class / security_type / issuer / currency / geography 等
- lookthrough false/true
- max_depth，MVP 限制為 1

輸出同時保留：

- direct exposure；
- look-through effective exposure；
- covered value；
- uncovered／unclassified value；
- coverage percent；
- ETF holdings dates；
- warnings。

## 4.5 ETF Look-through 規則

1. MVP 只做一層 look-through；不遞迴拆解 constituent fund。
2. 需避免 fund 本身與成分同時被計入 look-through total。
3. `direct exposure` 與 `look-through exposure` 是兩套並列視圖，不互相覆寫。
4. 成分權重未達 100% 時，差額放入 `Other/Unmapped`，不得按比例偷偷放大已知成分。
5. holdings as-of 晚於報告 cutoff 時不得用來計算歷史曝險。
6. 成分 ticker 無法映射 stable instrument/issuer 時仍保留 raw name、raw ticker 與 weight。
7. 同一 issuer 的 ADR、本地股及 ETF 間接持股可在 issuer view 聚合，但需保留來源拆解。
8. 對 leverage/inverse/derivative ETF 不得假設 holdings weights 等同經濟曝險；MVP 可標示 unsupported look-through + warning。

## 4.6 初始資料修正驗收案例

至少涵蓋下列目前已觀察到的類型：

- `VOO`、`VT`、`SOXX`：ETF，而非 common stock；
- `GLD`：商品／黃金曝險；
- `BOXX`：ETF，分類不得只依 ticker 名稱猜測；
- `USDT-USD`：stablecoin，可標示 cash-equivalent role，但仍保留 crypto security type；
- `TSM` 與 `2330.TW`：映射至相同 issuer，listing 與 instrument identity 仍分開。

## 4.7 驗收標準

- 分類值可逐欄追溯到來源與 effective date。
- Manual override 不會覆蓋或刪除 provider 原始資料。
- TSM 與 2330 可在 issuer exposure 聚合。
- ETF look-through 加總不重複計入 ETF 本身。
- holdings coverage 不滿 100% 時清楚顯示殘餘曝險。
- 無法分類的資產保留在 Unclassified，不使整個 summary 失敗。
- 歷史 `as_of` 使用當時可取得的 holdings snapshot，不發生 look-ahead。

---

# 5. 功能四：Corporate Actions＋原子化交易／現金帳本

## 5.1 目標

把資產、現金、費用、稅與 corporate action 統一到可稽核 journal。一次交易要能以單一原子操作寫入所有 legs，避免「持倉已更新但現金未更新」或相反狀況。

## 5.2 Journal 資料模型

### `journal_events`

- `id`
- `portfolio_id`
- `request_id`
- `event_type`
- `status`: posted / reversed
- `occurred_at`
- `trade_date` nullable
- `settlement_date` nullable
- `source`
- `source_reference`
- `memo`
- `reverses_event_id` nullable
- `created_at`
- unique `(portfolio_id, request_id)`

### `journal_legs`

- `id`
- `event_id`
- `leg_type`: security / cash / fee / tax / income / receivable / other
- `instrument_id` nullable
- `currency`
- `quantity_delta` nullable
- `amount_delta` nullable
- `unit_price` nullable
- `fx_rate` nullable
- `account_role`
- `metadata`

必須定義 balance invariant。對單幣別現貨交易，security consideration、cash、fee、tax legs 換算至 event functional currency 後應平衡；若不平衡則整個 transaction rollback。

### `corporate_actions`

- `id`
- `instrument_id`
- `action_type`
- `announcement_date` nullable
- `ex_date`
- `record_date` nullable
- `pay_date` nullable
- `effective_at`
- ratio／cash amount／currency／new instrument fields
- withholding tax fields nullable
- `source`
- `source_reference`
- `status`: announced / confirmed / applied / cancelled
- `fetched_at`

### `corporate_action_applications`

記錄 action 套用到哪些 Portfolio／position、產生哪些 journal events、原始 quantity/cost、結果 quantity/cost、rounding/cash-in-lieu 與 warnings。

## 5.3 原子化 MCP Tools

### `record_transaction`

MVP transaction types：

- buy
- sell
- deposit
- withdrawal
- transfer_in
- transfer_out
- dividend
- interest
- fee
- tax

Spot trade 輸入至少包含：

- portfolio ID
- request ID
- ticker/instrument ID
- side
- quantity
- execution price
- trade currency
- fee legs
- tax legs
- settlement cash currency/amount
- trade date
- settlement date
- source reference

伺服器需在 commit 前驗證：

- instrument identity；
- Decimal 格式；
- sell quantity 不超過可用 quantity；
- cash 是否允許為負（MVP 預設不允許，除非 Portfolio metadata 明確支援）；
- journal balance；
- request ID 冪等；
- timestamp 與 currency。

一次 DB transaction 內完成所有 legs、position/cash projection 更新與 audit record；任一步失敗全部 rollback。

### `reverse_transaction`

- 不刪除原事件；
- 建立完整相反 legs；
- 連結 `reverses_event_id`；
- 已 reversal 的事件不可再次 reversal，除非以明確 replacement workflow 處理；
- 回傳 reversal 前後 position/cash 摘要。

### `get_journal_event`

回傳 header、全部 legs、balance validation、source、reversal chain。

### `list_journal_events`

支援 portfolio、date range、event type、instrument、source reference 分頁查詢。

## 5.4 既有工具相容性

- `record_trade` 保留目前「不改變現金」語意，標記為 legacy/unlinked trade entry；不可在無版本變更下改成自動扣現金。
- `record_cash_transaction` 保留相容性，但新資料應轉成 journal event。
- 新 API output 應指出 legacy transaction 是否缺 settlement linkage。
- 提供 migration/backfill job，把既有 trade 與 cash 記錄轉為 journal events；不得自行把時間接近的 trade 和 cash 猜成同一 transaction。
- 無法確定關聯時標示 `unlinked_legacy_event`，等待人工確認。

## 5.5 Corporate Actions 最低支援

### Cash dividend / distribution

- gross income leg；
- withholding tax leg；
- net cash leg；
- pay date；
- 不視為外部 cash flow。

### Interest

- gross interest、tax、net cash；
- 不視為外部 cash flow。

### Stock split / reverse split

- quantity 依 ratio 調整；
- total cost basis 不變；
- unit average cost反向調整；
- fractional handling 與 cash-in-lieu 明確記錄。

### Stock dividend

- 新增 quantity；
- cost allocation method 必須明確且版本化；如果司法／稅務口徑未知，保存原始事件並標示 cost basis unresolved，不得猜測。

### Return of capital

- cash 增加；
- cost basis 調整；
- 超過 basis 的處理若未實作稅務規則，標示 unresolved tax treatment。

### Symbol change

- 不建立新的經濟曝險；
- instrument identity 保持或建立 successor relationship；
- 保留歷史 ticker alias。

### Merger / acquisition / security conversion

- 舊 instrument quantity 減少；
- 新 instrument／cash consideration 增加；
- ratio、cash、fractional handling、cost allocation 均可追溯。

### Spin-off

- 新 instrument 增加；
- cost allocation 若無可靠來源不得猜測，需保留 unresolved status。

## 5.6 Corporate Action Tools

### `record_corporate_action`

新增已知 action facts；需有 request ID 與 source。

### `preview_corporate_action_application`

顯示受影響 Portfolio、position、預計產生 journal legs、rounding、未解問題與 warnings，不寫入。

### `apply_corporate_action`

原子化套用並建立 journal event；相同 action/portfolio 不可重複套用。

### `reverse_corporate_action_application`

以 reversal event 撤銷，不刪除原資料。

## 5.7 驗收標準

- Buy transaction 的 security、cash、fee、tax legs 在同一 DB transaction 成功或全部失敗。
- 重複 request ID 不會重複入帳；payload 不同卻沿用 request ID 時回傳 conflict。
- Reversal 後 position、cash 與原 transaction 前一致，且 audit chain 完整。
- Cash dividend 正確區分 gross、withholding tax、net cash，且不算 external flow。
- 2-for-1 split 後 quantity 加倍、總成本不變、單位成本減半。
- Corporate action 重跑不會重複套用。
- 未知 cost allocation 不會被填 0 或任意比例，而是回傳 unresolved + warning。
- Legacy trade/cash migration 不會猜測不存在的 settlement linkage。

---

# 6. 建議實作順序

四項需求有相依性，建議不是照 UI 顯示順序開發，而是依資料真值順序：

## Phase A：Instrument identity 與 classification foundation

1. 盤點現有 ticker/instrument schema 與 provider normalization。
2. 建立 issuer、instrument、alias、classification migrations。
3. 為既有 positions 建立 stable instrument IDs。
4. 加入 profile、override、issuer mapping tools。
5. 先完成 TSM/2330、ETF、GLD、USDT 等 regression tests。

## Phase B：Journal 與 atomic transaction

1. 建立 event/legs schema 與 balance validator。
2. 先以 failing tests 定義 buy/sell/cash/fee/tax 原子性。
3. 實作 `record_transaction`、query、reversal。
4. 建立 legacy projection/migration，不猜測 trade-cash linkage。
5. 實作 corporate action preview/apply/reverse。

## Phase C：Historical valuation foundation

1. 由 journal 在任意 cutoff 重建 position/cash state。
2. 建立 point-in-time valuation snapshot schema。
3. 實作單日 snapshot、range rebuild、partial coverage。
4. 建立 daily scheduler，但確保相同命令可手動重跑。
5. 實作 NAV history、TWR、XIRR。

## Phase D：Benchmark

1. 建立 benchmark 與 historical assignment schema。
2. 實作 instrument benchmark。
3. 實作固定權重 blended benchmark。
4. 實作 benchmark currency conversion、price/total-return warnings。
5. 整合 `get_portfolio_performance`。

## Phase E：Consolidation 與 FX

1. 建立 portfolio group 與 membership history。
2. 建立 point-in-time FX service/cache。
3. 實作 consolidated current summary。
4. 實作 consolidated historical NAV/performance。
5. 整合 issuer exposure 與 direct/look-through exposure。

## Phase F：ETF look-through

1. 建立 holdings snapshot ingestion/cache。
2. 實作 one-level look-through 與 coverage。
3. 防止 double count、future holdings leakage 與 unsupported leveraged fund 誤算。
4. 整合 portfolio/group exposure output。

---

# 7. 測試與驗證要求

## 7.1 測試層級

- Unit tests：Decimal arithmetic、journal balancing、FX inversion/cross、TWR、XIRR、split、分類優先級、look-through aggregation。
- Property tests：journal reversal、FX reciprocal、weights/coverage invariant、snapshot deterministic rebuild。
- Integration tests：MCP schema、DB transaction rollback、migration、provider partial failures。
- Golden tests：以固定 point-in-time prices/FX/cash flows 驗證整份 summary/performance JSON。
- Backward compatibility tests：現有 19 個 MCP tools 的既有 schema 與語意。

## 7.2 必要 fixtures

至少建立：

1. USD Portfolio + TWD Portfolio + USD reporting group；
2. 一檔 USD 股票、一檔 TWD 股票、兩種 cash currency；
3. TSM + 2330 同 issuer；
4. ETF 含已映射、未映射與 residual holdings；
5. 期間內 deposit、withdraw、buy、sell、dividend、fee、tax、split；
6. benchmark total return 缺失，只存在 price return；
7. 某日缺價格或 FX；
8. stale FX；
9. duplicate request ID；
10. legacy unlinked trade/cash records。

## 7.3 完成定義

每一 Phase 完成前必須：

- migration 可在空資料庫與既有資料庫成功執行；
- rollback 測試成功；
- MCP schema 文件更新；
- 所有新增 output fields 有 example；
- Decimal、time zone、as-of、provider、coverage、warnings 通過 review；
- 舊工具 regression tests 全部通過；
- 使用固定 fixtures 重跑兩次結果一致；
- 實際透過 MCP client 呼叫，不只測 service function。

---

# 8. 主要風險與設計決策

1. **歷史行情可得性：** 若 provider 無法提供可靠 point-in-time adjusted prices，snapshot backfill 必須標示 provider、adjustment 與 partial coverage。
2. **Benchmark total return：** Yahoo-style price history未必是正式 total-return index；不得把 adjusted price 靜默宣稱為 total return。
3. **ETF holdings 時點：** 公開成分通常有延遲；需顯示 holdings date，不得用今天成分重建過去曝險。
4. **Corporate action cost basis：** 不同市場與稅務制度不同；本期優先保存事實與 unresolved status，不做無根據判斷。
5. **Legacy migration：** 現有 trade 與 cash 分離，無法可靠自動配對；保留 unlinked 狀態比錯誤配對更重要。
6. **跨市場 identity：** 同 issuer 不代表同 instrument；ADR、本地股、不同 share class 必須分開 instrument，只在 issuer exposure 層聚合。
7. **Group 歷史成員：** membership 必須 effective-dated，否則改成員會改寫歷史報告。
8. **計算版本：** TWR、XIRR、snapshot、classification 與 look-through 都需要 version，使未來方法改良時能解釋數字差異。

---

# 9. 開工前必做的 Repository Discovery

開始實作前，請先完成 repository 只讀盤點，確認下列現有元件及其實際路徑：

1. MCP server entrypoint 與 tool registration；
2. ORM models、migrations 與 database type；
3. portfolio、trade、cash、position projection services；
4. market data provider abstraction；
5. Decimal serialization 與 API schema；
6. 現有 tests、fixtures、migration test strategy；
7. scheduler/background job infrastructure；
8. current 19-tool compatibility surface。

請先提交一份短的 discovery note，列出：

- 現有架構；
- 具體檔案路徑；
- schema migration 風險；
- 哪些需求可沿用既有 abstraction；
- Phase A 的第一批 failing tests。

未完成 discovery 前，不要猜檔名或直接大規模重構。

---

# 10. MCP Tools、Resources、Prompts 的角色

## Tools

Tools 是可執行的函式介面，用於查詢、計算或 mutation。例如：

- `get_consolidated_summary`
- `record_transaction`
- `apply_corporate_action`

它們有明確輸入輸出 schema，可存取資料庫或 provider。金融數字與帳務規則應由 Tools／服務端程式確定，而不是由 LLM prompt 決定。

## Resources

Resources 是 MCP server 以 URI 暴露的唯讀內容或資料。它們適合提供模型／client 可查閱的背景、方法與 reference data，但不代表執行操作。

本服務可考慮：

- `portfolio://methodology/valuation`
- `portfolio://methodology/performance`
- `portfolio://taxonomy/assets`
- `portfolio://providers/market-data`
- `portfolio://benchmarks/catalog`
- `portfolio://data-quality/current`
- `portfolio://schemas/journal-events`

Resources 對「資料透明度」很有價值，因為 client 可以直接讀到：數字如何計算、分類有哪些、provider 限制為何、目前資料覆蓋率如何。它不應取代 Tool output 中必要的 provider/as-of/warnings。

## Prompts

Prompts 是 MCP server 提供的可重用 prompt template，通常可接受參數，讓 client 取得一套建議的分析流程或輸出格式。Prompt 本身不應直接改帳，也不應成為財務計算的唯一實作。

未來可考慮：

- `explain_portfolio_change`
- `summarize_data_quality`
- `explain_performance_vs_benchmark`
- `review_unclassified_exposures`
- `review_unresolved_corporate_actions`

例如 `explain_performance_vs_benchmark` prompt 可以指示 Agent 先呼叫 performance、benchmark 和 data-quality tools，再以固定章節解釋結果。真正的 TWR、XIRR 和 FX 數值仍由 Tools 計算。

## 本期建議

- **優先 Resources，暫緩 Prompts。**
- 先把 methodology、taxonomy、provider limitations 與 data-quality 定義成 Resources，有助資料透明度。
- 等四項功能的 schema 與分析模式穩定後，再把反覆使用的解讀流程抽成 Prompts。
- 不要把尚未穩定的交易守則或分析規則固化成 Prompt。
