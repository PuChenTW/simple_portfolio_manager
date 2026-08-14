<script lang="ts">
  // The summary is one blocking request whose cost scales with holdings: quotes are fetched per
  // ticker, so a cold load is seconds and a warm one is ~100ms. The server streams no progress,
  // so a percentage bar would be invented -- the honest signal is the shape of what is coming,
  // plus an elapsed hint once the wait stops feeling instant.
  let { elapsed }: { elapsed: number } = $props()

  const SLOW_AFTER_MS = 2500
  const isSlow = $derived(elapsed > SLOW_AFTER_MS)
</script>

<div class="skeleton" aria-busy="true" aria-live="polite">
  <span class="visually-hidden">
    Loading portfolio{isSlow ? ', fetching live prices' : ''}…
  </span>

  <section class="card hero" aria-hidden="true">
    <div>
      <span class="line sm"></span>
      <span class="line xl"></span>
      <span class="line xs"></span>
    </div>
    <div class="stack">
      <span class="line md"></span>
      <span class="line md"></span>
      <span class="line md"></span>
    </div>
  </section>

  <div class="grid" aria-hidden="true">
    <section class="card">
      <span class="line sm"></span>
      <span class="bar"></span>
      <span class="line xs"></span>
      {#each { length: 2 } as _, i (i)}
        <span class="line row"></span>
      {/each}
    </section>
    <section class="card">
      <span class="line sm"></span>
      {#each { length: 4 } as _, i (i)}
        <span class="line row"></span>
      {/each}
    </section>
  </div>

  <section class="card" aria-hidden="true">
    <span class="line sm"></span>
    {#each { length: 3 } as _, i (i)}
      <span class="line row"></span>
    {/each}
  </section>

  {#if isSlow}
    <p class="hint muted">
      Fetching live prices. The first load of the day is slower; later ones are cached.
    </p>
  {/if}
</div>

<style>
  .skeleton {
    display: flex;
    flex-direction: column;
    gap: 16px;
  }

  .card {
    padding: var(--pad);
    background: var(--surface);
    border: 1px solid var(--border);
    border-radius: var(--radius);
  }

  .hero {
    display: flex;
    justify-content: space-between;
    gap: 32px;
    flex-wrap: wrap;
  }

  .stack {
    display: flex;
    flex-direction: column;
    gap: 8px;
    min-width: 200px;
  }

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

  .line,
  .bar {
    display: block;
    border-radius: var(--radius-sm);
    background: linear-gradient(
      90deg,
      var(--surface-sunken) 25%,
      color-mix(in srgb, var(--surface-sunken) 55%, var(--border-strong)) 37%,
      var(--surface-sunken) 63%
    );
    background-size: 400% 100%;
    animation: shimmer 1.4s ease-in-out infinite;
  }

  .xs {
    width: 96px;
    height: 10px;
    margin-top: 10px;
  }
  .sm {
    width: 132px;
    height: 12px;
  }
  .md {
    width: 100%;
    height: 13px;
  }
  .xl {
    width: min(320px, 68vw);
    height: 40px;
    margin-top: 12px;
  }
  .row {
    width: 100%;
    height: 14px;
    margin-top: 14px;
  }
  .bar {
    width: 100%;
    height: 12px;
    border-radius: 999px;
    margin-top: 16px;
  }

  @keyframes shimmer {
    0% {
      background-position: 100% 50%;
    }
    100% {
      background-position: 0 50%;
    }
  }

  /* A shimmer is decoration, not information; respect a stated preference against motion. */
  @media (prefers-reduced-motion: reduce) {
    .line,
    .bar {
      animation: none;
      background: var(--surface-sunken);
    }
  }

  .hint {
    margin: 0;
    text-align: center;
    font-size: 13px;
  }

  .visually-hidden {
    position: absolute;
    width: 1px;
    height: 1px;
    overflow: hidden;
    clip-path: inset(50%);
  }
</style>
