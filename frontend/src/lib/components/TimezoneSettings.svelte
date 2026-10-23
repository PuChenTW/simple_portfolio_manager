<script lang="ts">
  import { timezone } from '../timezone.svelte'
  import { validTimezone } from '../timezone'

  let named = $state(timezone.effective)
  let error = $state('')

  function apply() {
    if (!validTimezone(named)) {
      error = 'Use an IANA timezone, such as Asia/Taipei.'
      return
    }
    timezone.select(named)
    error = ''
  }
</script>

<details class="timezone-settings">
  <summary>Timezone · {timezone.effective}</summary>
  <div class="settings">
    <label>
      Timezone mode
      <select value={timezone.preference === 'browser' ? 'browser' : 'named'} onchange={(event) => {
        if (event.currentTarget.value === 'browser') timezone.select('browser')
        else if (validTimezone(named)) timezone.select(named)
        else error = 'Use an IANA timezone, such as Asia/Taipei.'
      }}>
        <option value="browser">Follow browser</option>
        <option value="named">Specify timezone</option>
      </select>
    </label>
    <label>
      IANA timezone
      <input list="timezone-options" bind:value={named} placeholder="Asia/Taipei" />
      <datalist id="timezone-options">
        {#each Intl.supportedValuesOf('timeZone') as zone}
          <option value={zone}></option>
        {/each}
      </datalist>
    </label>
    <button type="button" onclick={apply}>Apply timezone</button>
    <p class="faint">Stored in this browser. Reports and transaction dates use this timezone.</p>
    {#if error}<p role="alert">{error}</p>{/if}
  </div>
</details>

<style>
  summary { cursor: pointer; font-size: 13px; }
  .settings { display: grid; gap: 10px; padding: 12px 0; }
  label { display: grid; gap: 4px; font-size: 13px; }
  input, select { padding: 6px; max-width: 100%; }
  p { margin: 0; font-size: 12px; }
</style>
