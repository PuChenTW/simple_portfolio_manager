# Portfolio Manager

A local-first record of what someone owns and owes, valued historically from an auditable
double-entry journal.

## Language

### Books

**Portfolio**:
One book of account, holding events in exactly one currency. Its `kind` says what the book is
for; a cash account and a loan are portfolios, not separate tables.
_Avoid_: Account (ambiguous — see Account below), fund

**Account**:
A portfolio as a person speaks of it: "my Firstrade account", "my bank account". Used in
user-facing text and in field names that count books, never as a type.
_Avoid_: Using this for the persisted entity, which is Portfolio

**Group**:
A named selection of portfolios reported together in one currency. A group owns no value of its
own — it only says which books to add up.
_Avoid_: Household, consolidation, aggregate

### Value

**Assets**:
What is owned, as a positive figure. Excludes liability books.
_Avoid_: Gross assets, holdings value

**Liabilities**:
What is owed, always as a negative figure so that assets plus liabilities equals net worth. A
reader must never be able to add where they should subtract.
_Avoid_: Debt, debts, loans (a loan is one liability account)

**Net worth**:
Assets less liabilities. The single number the dashboard leads with, and the line the series
charts.
_Avoid_: Total assets, net asset value, NAV, equity, 總資產

**Snapshot**:
What one portfolio was worth on one date, priced only with data available on or before that
date. An auditable record of a computation, never a second source of truth.
_Avoid_: Valuation, daily close, EOD value

**Series**:
Net worth across a range of dates for a group, one point per date that has snapshots.
_Avoid_: History, timeline, trend

### Reading a series honestly

**Account entry**:
The date an account's records begin, taken from its first journal event. The series changes what
it measures on this date, so a rise here is composition, not gain.
_Avoid_: Account opening, inception, start date

**Contributing account count**:
How many accounts are inside one point's value. Two dates are comparable only when their counts
match.
_Avoid_: Member count, active accounts

**Partial**:
A value that is known to understate, because an account that had started has no snapshot for the
date, or because a currency could not be converted. An account that had not started yet does not
make a point partial — it has no data to be missing.
_Avoid_: Incomplete, estimated, provisional

**Unconverted**:
Value left in its own currency because its pair could not be resolved, excluded from the total
and listed with the amount. Never converted at a guessed rate, and never silently dropped.
_Avoid_: Unmatched, FX failure, skipped

**Coverage**:
The share of value that reached the reporting currency, or that could be priced. Always reported
next to the total it qualifies.
_Avoid_: Completeness, confidence, quality
