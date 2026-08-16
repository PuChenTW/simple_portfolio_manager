<script lang="ts">
  import { router } from '../../route.svelte'
  import { shortDate } from '../../format'

  /** Snapshots from `from` forward no longer reflect the journal.
   *
   * Two things create this state and both need the same sentence: a back-dated posting, and a
   * reversal of one. Saying it is the whole point -- without it the user gets a correct ledger
   * and a performance chart still reporting the old numbers, with nothing connecting the two.
   *
   * It never rebuilds automatically. A repair rebuild replaces stored valuations, which is why
   * `AccountSnapshots` gates it behind a second click naming how many it will replace. This
   * points there instead, so the destructive step stays the user's.
   */
  let {
    from,
    portfolioId,
    children,
  }: {
    /** The `YYYY-MM-DD` the invalidation starts at. */
    from: string
    portfolioId: string
    /** The leading sentence, which differs between a posting and a reversal. */
    children?: import('svelte').Snippet
  } = $props()
</script>

<p class="stale">
  {#if children}{@render children()}{:else}
    Snapshots from {shortDate(from)} are now out of date.
  {/if}
  Rebuild them from the
  <a href={router.account(portfolioId, 'settings')}>Snapshots panel</a> in Settings.
</p>

<style>
  .stale {
    margin: 0;
    padding: 8px 10px;
    font-size: 12px;
    background: var(--surface-sunken);
    border: 1px solid var(--border);
    border-radius: var(--radius-sm);
  }
</style>
