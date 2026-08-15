<script lang="ts">
  import { dashboard } from './lib/state.svelte'
  import { router } from './lib/route.svelte'
  import NetWorth from './lib/components/NetWorth.svelte'
  import Composition from './lib/components/Composition.svelte'
  import Allocation from './lib/components/Allocation.svelte'
  import Holdings from './lib/components/Holdings.svelte'
  import Accounts from './lib/components/Accounts.svelte'
  import Classification from './lib/components/Classification.svelte'
  import AccountPage from './lib/components/account/AccountPage.svelte'
  import ThemeToggle from './lib/components/ThemeToggle.svelte'
  import Skeleton from './lib/components/Skeleton.svelte'

  dashboard.load()

  function onSwitch(event: Event) {
    const target = event.currentTarget as HTMLSelectElement
    dashboard.selectGroup(target.value)
  }

  // Distinct instruments, not holdings: one unclassified ticker held in three accounts is one
  // decision to make, and counting it three times would overstate the work left.
  const unclassifiedCount = $derived(
    new Set(
      (dashboard.summary?.positions ?? [])
        .filter((p) => p.asset_class === 'unclassified')
        .map((p) => p.ticker),
    ).size,
  )
</script>

<div class="shell">
  <!-- The account page carries its own heading and breadcrumb. Repeating the group header above
       it would name a group whose numbers are not the ones on screen, and the switcher would
       imply it changes a single account's figures, which it does not. -->
  {#if router.name !== 'account'}
    <header class="topbar">
      <div>
        <h1>{dashboard.selectedGroup?.name ?? 'Portfolio'}</h1>
        {#if dashboard.summary}
          <p class="faint sub">
            {dashboard.summary.portfolio_ids.length} accounts · reporting in
            {dashboard.summary.reporting_currency}
          </p>
        {/if}
      </div>

      <div class="controls">
        {#if dashboard.groups.length > 1}
          <label class="switcher">
            <span class="visually-hidden">Group</span>
            <select
              value={dashboard.selectedGroupId}
              onchange={onSwitch}
              disabled={dashboard.refreshing}
            >
              {#each dashboard.groups as group (group.id)}
                <option value={group.id}>{group.name}</option>
              {/each}
            </select>
          </label>
        {/if}

        <nav>
          <a href={router.dashboard()} class:current={router.name === 'dashboard'}>
            Dashboard
          </a>
          <a href={router.classify()} class:current={router.name === 'classify'}>
            Classification
            <!-- The count is the point of surfacing this in the nav: an unclassified holding is
                 invisible on the dashboard, so nothing else would ever prompt someone to fix it. -->
            {#if unclassifiedCount}<span class="badge">{unclassifiedCount}</span>{/if}
          </a>
        </nav>

        <ThemeToggle />
      </div>
    </header>
  {/if}

  {#if dashboard.refreshing}
    <!-- Indeterminate by necessity: the summary is one request that reports no progress. -->
    <div class="progress" role="progressbar" aria-label="Loading group"></div>
  {/if}

  <!-- An account reads its own endpoints, so it must not wait on the group summary or be
       blocked by "no groups yet" -- an account can exist before it belongs to any group. -->
  {#if router.route.name === 'account'}
    <AccountPage
      portfolioId={router.route.id}
      tab={router.route.tab}
      groupName={dashboard.selectedGroup?.name ?? null}
      onchange={() => dashboard.reload()}
    />
  {:else if dashboard.loading}
    <Skeleton elapsed={dashboard.elapsed} />
  {:else if dashboard.error}
    <p class="state error">{dashboard.error}</p>
  {:else if !dashboard.groups.length}
    <p class="state muted">
      No portfolio groups yet. Create one to see a consolidated view.
    </p>
  {:else if dashboard.summary}
    <div class="content" class:stale={dashboard.refreshing}>
      {#if router.name === 'classify'}
        <Classification summary={dashboard.summary} onchange={() => dashboard.refresh()} />
      {:else}
        <NetWorth summary={dashboard.summary} />

        {#if dashboard.summary.warnings.length}
          <ul class="warnings">
            {#each dashboard.summary.warnings as warning (warning)}
              <li>{warning}</li>
            {/each}
          </ul>
        {/if}

        <Allocation summary={dashboard.summary} />

        <div class="grid">
          <Composition summary={dashboard.summary} />
          <Accounts portfolios={dashboard.portfolios} summary={dashboard.summary} />
        </div>

        <Holdings summary={dashboard.summary} />
      {/if}
    </div>
  {/if}
</div>

<style>
  .shell {
    max-width: 1160px;
    margin: 0 auto;
    padding: 24px 20px 64px;
    display: flex;
    flex-direction: column;
    gap: 16px;
  }

  .topbar {
    display: flex;
    align-items: flex-start;
    justify-content: space-between;
    gap: 16px;
    flex-wrap: wrap;
  }

  h1 {
    font-size: 20px;
  }

  .sub {
    margin: 2px 0 0;
    font-size: 12px;
  }

  select {
    padding: 6px 10px;
    font: inherit;
    font-size: 13px;
    color: var(--text);
    background: var(--surface);
    border: 1px solid var(--border-strong);
    border-radius: var(--radius-sm);
  }

  .controls {
    display: flex;
    align-items: center;
    gap: 12px;
    flex-wrap: wrap;
  }

  nav {
    display: flex;
    gap: 4px;
  }

  nav a {
    display: inline-flex;
    align-items: center;
    gap: 6px;
    padding: 6px 10px;
    font-size: 13px;
    text-decoration: none;
    color: var(--text-faint);
    border: 1px solid transparent;
    border-radius: var(--radius-sm);
  }

  nav a.current {
    color: var(--text);
    background: var(--surface);
    border-color: var(--border-strong);
  }

  .badge {
    padding: 0 6px;
    font-size: 11px;
    font-variant-numeric: tabular-nums;
    color: var(--text);
    background: var(--warning-soft);
    border: 1px solid color-mix(in srgb, var(--warning) 35%, transparent);
    border-radius: 999px;
  }

  /* Two columns on desktop; composition and accounts read side by side above the holdings table. */
  .grid {
    display: grid;
    grid-template-columns: 1fr 1fr;
    gap: 16px;
  }

  @media (max-width: 860px) {
    .grid {
      grid-template-columns: 1fr;
    }
  }

  .warnings {
    margin: 0;
    padding: 12px 16px 12px 34px;
    background: var(--warning-soft);
    border: 1px solid color-mix(in srgb, var(--warning) 35%, transparent);
    border-radius: var(--radius-sm);
    font-size: 13px;
  }

  .content {
    display: flex;
    flex-direction: column;
    gap: 16px;
  }

  /* Numbers on screen during a switch belong to the previous group. Dimming says "being
     replaced" without removing them; `pointer-events: none` stops a click landing on a row
     that is about to change underneath it. */
  .content.stale {
    opacity: 0.45;
    pointer-events: none;
    transition: opacity 120ms ease-out;
  }

  .progress {
    height: 2px;
    border-radius: 999px;
    background: var(--surface-sunken);
    overflow: hidden;
    position: relative;
  }

  .progress::after {
    content: '';
    position: absolute;
    inset: 0;
    width: 35%;
    border-radius: inherit;
    background: var(--accent);
    animation: slide 1.1s ease-in-out infinite;
  }

  @keyframes slide {
    0% {
      transform: translateX(-100%);
    }
    100% {
      transform: translateX(340%);
    }
  }

  @media (prefers-reduced-motion: reduce) {
    .progress::after {
      animation: none;
      width: 100%;
      opacity: 0.5;
    }
  }

  .state {
    padding: 40px 0;
    text-align: center;
  }

  .error {
    color: var(--negative);
  }

  .visually-hidden {
    position: absolute;
    width: 1px;
    height: 1px;
    overflow: hidden;
    clip-path: inset(50%);
  }
</style>
