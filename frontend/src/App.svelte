<script lang="ts">
  import { dashboard } from './lib/state.svelte'
  import NetWorth from './lib/components/NetWorth.svelte'
  import Composition from './lib/components/Composition.svelte'
  import Allocation from './lib/components/Allocation.svelte'
  import Holdings from './lib/components/Holdings.svelte'
  import Accounts from './lib/components/Accounts.svelte'
  import Skeleton from './lib/components/Skeleton.svelte'

  dashboard.load()

  function onSwitch(event: Event) {
    const target = event.currentTarget as HTMLSelectElement
    dashboard.selectGroup(target.value)
  }
</script>

<div class="shell">
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
  </header>

  {#if dashboard.refreshing}
    <!-- Indeterminate by necessity: the summary is one request that reports no progress. -->
    <div class="progress" role="progressbar" aria-label="Loading group"></div>
  {/if}

  {#if dashboard.loading}
    <Skeleton elapsed={dashboard.elapsed} />
  {:else if dashboard.error}
    <p class="state error">{dashboard.error}</p>
  {:else if !dashboard.groups.length}
    <p class="state muted">
      No portfolio groups yet. Create one to see a consolidated view.
    </p>
  {:else if dashboard.summary}
    <div class="content" class:stale={dashboard.refreshing}>
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
