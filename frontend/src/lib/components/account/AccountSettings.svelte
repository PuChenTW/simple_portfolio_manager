<script lang="ts">
  import { api, ApiError, type Portfolio } from '../../api/client'
  import { shortDate } from '../../format'
  import AccountSnapshots from './AccountSnapshots.svelte'

  let {
    portfolio,
    onsaved,
    ondeleted,
  }: {
    portfolio: Portfolio
    onsaved: (updated: Portfolio) => void
    ondeleted: () => void
  } = $props()

  let name = $state('')
  let institution = $state('')
  let saving = $state(false)
  let saveError = $state<string | null>(null)
  /** The account a save succeeded for, so the confirmation cannot linger onto another one. */
  let savedId = $state<string | null>(null)

  // Seed the form, and re-seed it when the page switches to a different account or a save
  // returns the updated record.
  $effect(() => {
    name = portfolio.name
    institution = portfolio.institution ?? ''
    saveError = null
  })

  const dirty = $derived(
    name.trim() !== portfolio.name || institution.trim() !== (portfolio.institution ?? ''),
  )

  const saved = $derived(savedId === portfolio.id && !dirty)

  async function save(event: SubmitEvent): Promise<void> {
    event.preventDefault()
    if (!dirty || !name.trim()) return

    saving = true
    saveError = null
    savedId = null
    try {
      // Only changed fields are sent. `institution` cannot be cleared by the API, only
      // replaced, so an empty box means "leave it alone" rather than "erase it".
      const body: { name?: string; institution?: string } = {}
      if (name.trim() !== portfolio.name) body.name = name.trim()
      if (institution.trim() && institution.trim() !== portfolio.institution) {
        body.institution = institution.trim()
      }
      const updated = await api.updatePortfolio(portfolio.id, body)
      savedId = updated.id
      onsaved(updated)
    } catch (err) {
      saveError =
        err instanceof ApiError && err.code === 'portfolio_name_exists'
          ? 'Another account already uses that name.'
          : err instanceof Error
            ? err.message
            : String(err)
    } finally {
      saving = false
    }
  }

  // Deletion cascades through every position, event, and snapshot, and there is no reversal
  // for it the way there is for a posting. Typing the name is the confirmation: a click cannot
  // be made deliberate by a dialog nobody reads.
  let confirmName = $state('')
  let deleting = $state(false)
  let deleteError = $state<string | null>(null)
  const confirmed = $derived(confirmName.trim() === portfolio.name)

  async function remove(): Promise<void> {
    if (!confirmed) return
    deleting = true
    deleteError = null
    try {
      await api.deletePortfolio(portfolio.id)
      ondeleted()
    } catch (err) {
      deleteError = err instanceof Error ? err.message : String(err)
      deleting = false
    }
  }
</script>

<div class="stack">
  <section class="card">
    <header><h2>Labels</h2></header>

    <form onsubmit={save}>
      <label>
        <span class="label">Name</span>
        <input type="text" bind:value={name} required maxlength="120" />
      </label>

      <label>
        <span class="label">Institution</span>
        <input type="text" bind:value={institution} placeholder="Bank or broker" maxlength="120" />
        <span class="hint faint">Cannot be cleared once set, only replaced.</span>
      </label>

      <div class="actions">
        <button type="submit" class="primary" disabled={!dirty || !name.trim() || saving}>
          {saving ? 'Saving…' : 'Save changes'}
        </button>
        {#if saved}<span class="ok positive">Saved.</span>{/if}
        {#if saveError}<span class="negative">{saveError}</span>{/if}
      </div>
    </form>
  </section>

  <section class="card">
    <header><h2>Fixed at creation</h2></header>
    <!-- Currency and kind are the terms every posted leg was recorded under. Changing one would
         reinterpret history rather than relabel it, so the API refuses and this page does not
         offer it. See api.py, patch_portfolio. -->
    <dl class="facts">
      <div><dt>Base currency</dt><dd class="num">{portfolio.base_currency}</dd></div>
      <div><dt>Kind</dt><dd>{portfolio.kind}</dd></div>
      <div><dt>Created</dt><dd>{shortDate(portfolio.created_at)}</dd></div>
      <div><dt>ID</dt><dd class="num id">{portfolio.id}</dd></div>
    </dl>
    <p class="note muted">
      To move money into a different currency or a different kind of book, open a new account and
      transfer to it — relabelling one would reinterpret every leg already posted.
    </p>
  </section>

  <AccountSnapshots {portfolio} />

  <section class="card danger">
    <header><h2>Delete this account</h2></header>
    <p class="note">
      Permanently deletes <strong>{portfolio.name}</strong> and cascades through its positions,
      journal events, and valuation snapshots. This cannot be undone.
    </p>

    <label>
      <span class="label">Type <strong>{portfolio.name}</strong> to confirm</span>
      <input type="text" bind:value={confirmName} placeholder={portfolio.name} />
    </label>

    <div class="actions">
      <button type="button" class="destructive" disabled={!confirmed || deleting} onclick={remove}>
        {deleting ? 'Deleting…' : 'Delete account'}
      </button>
      {#if deleteError}<span class="negative">{deleteError}</span>{/if}
    </div>
  </section>
</div>

<style>
  .stack {
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

  header {
    margin-bottom: 12px;
  }

  h2 {
    font-size: 15px;
  }

  form,
  .danger {
    display: flex;
    flex-direction: column;
    gap: 14px;
  }

  label {
    display: flex;
    flex-direction: column;
    gap: 4px;
    max-width: 420px;
  }

  .label {
    font-size: 12px;
    color: var(--text-muted);
  }

  .hint {
    font-size: 11px;
  }

  input[type='text'] {
    padding: 7px 10px;
    font: inherit;
    font-size: 13px;
    color: var(--text);
    background: var(--surface);
    border: 1px solid var(--border-strong);
    border-radius: var(--radius-sm);
  }

  input:focus-visible {
    outline: 2px solid var(--accent);
    outline-offset: 1px;
  }

  .actions {
    display: flex;
    align-items: center;
    gap: 12px;
    flex-wrap: wrap;
    font-size: 13px;
  }

  button {
    padding: 7px 14px;
    font: inherit;
    font-size: 13px;
    border-radius: var(--radius-sm);
    cursor: pointer;
  }

  button:disabled {
    opacity: 0.5;
    cursor: default;
  }

  .primary {
    color: #fff;
    background: var(--accent);
    border: 1px solid var(--accent);
  }

  .destructive {
    color: var(--negative);
    background: var(--surface);
    border: 1px solid var(--negative);
  }

  .destructive:not(:disabled):hover {
    color: #fff;
    background: var(--negative);
  }

  .danger {
    border-color: color-mix(in srgb, var(--negative) 40%, var(--border));
  }

  .danger h2 {
    color: var(--negative);
  }

  .note {
    margin: 0;
    font-size: 13px;
  }

  .facts {
    display: grid;
    grid-template-columns: repeat(auto-fit, minmax(160px, 1fr));
    gap: 12px 24px;
    margin: 0 0 12px;
  }

  .facts div {
    display: flex;
    flex-direction: column;
    gap: 2px;
  }

  dt {
    font-size: 12px;
    color: var(--text-muted);
  }

  dd {
    margin: 0;
    font-size: 14px;
  }

  .id {
    font-size: 11px;
    word-break: break-all;
  }

  .ok {
    font-size: 13px;
  }
</style>
