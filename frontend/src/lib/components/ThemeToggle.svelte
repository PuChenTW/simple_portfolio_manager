<script lang="ts">
  import { theme, type Theme } from '../theme.svelte'

  // The icon shows the state the button is *in*, not the one it would move to. A control
  // labelled with its own effect reads as a promise about the future; readers checking what
  // theme they are on would find the answer inverted.
  const ICON: Record<Theme, string> = {
    system: '◐',
    light: '☀',
    dark: '☾',
  }

  const LABEL: Record<Theme, string> = {
    system: 'Theme: follow system',
    light: 'Theme: light',
    dark: 'Theme: dark',
  }

  const title = $derived(
    theme.choice === 'system'
      ? `${LABEL.system} (currently ${theme.resolved})`
      : LABEL[theme.choice],
  )
</script>

<button
  type="button"
  class="toggle"
  onclick={() => theme.cycle()}
  {title}
  aria-label={title}
>
  <span aria-hidden="true">{ICON[theme.choice]}</span>
</button>

<style>
  .toggle {
    display: inline-flex;
    align-items: center;
    justify-content: center;
    width: 30px;
    height: 30px;
    padding: 0;
    font: inherit;
    font-size: 14px;
    line-height: 1;
    color: var(--text-muted);
    background: var(--surface);
    border: 1px solid var(--border-strong);
    border-radius: var(--radius-sm);
    cursor: pointer;
  }

  .toggle:hover {
    color: var(--text);
    background: var(--surface-sunken);
  }

  .toggle:focus-visible {
    outline: 2px solid var(--accent);
    outline-offset: 1px;
  }
</style>
