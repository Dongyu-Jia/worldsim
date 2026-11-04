// Settings: the LLM backend, the OpenRouter key (stored server-side, shown masked), the default model and the call
// parameters. The key is never sent back to the browser.
const EFFORTS = ['', 'none', 'minimal', 'low', 'medium', 'high', 'xhigh', 'max'];

export async function render(root, ctx) {
  const { api, ui } = ctx;
  const h = ui.h;
  let s = await ctx.reloadSettings();

  const keyCard = h('div', { class: 'card' });
  const modelCard = h('div', { class: 'card' });
  const callCard = h('div', { class: 'card' });
  const listCard = h('div', { class: 'card' });
  root.append(keyCard, modelCard, callCard, listCard);

  async function put(patch, okMsg) {
    try {
      s = await api.put('settings', patch);
      ctx.settings = s;
      await ctx.reloadSettings();
      ui.toast(okMsg || 'Settings saved', 'ok');
      drawKey(); drawModel(); drawCall();
      return true;
    } catch (e) {
      ui.toast(e.problems && e.problems.length ? e.problems.join('; ') : e.message, 'bad');
      return false;
    }
  }

  // ---------------------------------------------------------------- backend and key
  function drawKey() {
    ui.clear(keyCard);
    const keyIn = h('input', { type: 'password', autocomplete: 'off', placeholder: 'sk-or-v1-…', 'aria-label': 'New OpenRouter key',
      spellcheck: 'false' });
    const result = h('div', { class: 'small' });
    const backendRadio = (value, label, hint) => h('label', { class: 'check' },
      h('input', { type: 'radio', name: 'backend', value, checked: s.backend === value,
        onchange: () => put({ backend: value }, `Backend: ${label}`) }), h('span', null, h('strong', null, label), ' ', h('span', { class: 'muted small' }, hint)));
    ui.add(keyCard,
      h('h2', null, 'LLM backend'),
      h('div', { class: 'stack' },
        backendRadio('openrouter', 'OpenRouter', 'real models, billed to your OpenRouter key'),
        backendRadio('mock', 'Mock (no key)', 'deterministic, schema-valid answers for trying the app; free')),
      s.forced_backend ? h('div', { class: 'banner warn' }, `The server was started with WORLDSIM_LLM=${s.forced_backend}: that backend is used whatever is chosen here.`) : null,
      h('hr'),
      h('h2', null, 'OpenRouter API key'),
      h('p', { class: 'small muted' }, 'Stored on this machine only, in settings.json in WorldSim\'s data folder (file mode 0600). It is sent only to '
        + 'openrouter.ai, never shown here in full, and never written to logs, prompts or saved iterations. Get a key at openrouter.ai/keys.'),
      h('div', { class: 'row' }, h('span', null, 'Stored key: '),
        s.has_key ? h('span', { class: 'badge ok mono' }, s.key_masked) : h('span', { class: 'badge bad' }, 'none')),
      h('div', { class: 'list-row' }, keyIn,
        h('button', { class: 'btn primary', type: 'button', onclick: async () => {
          const v = keyIn.value.trim();
          if (!v) { ui.toast('Paste a key first', 'bad'); return; }
          if (await put({ api_key: v }, 'Key saved')) keyIn.value = '';
        } }, s.has_key ? 'Replace key' : 'Save key')),
      h('div', { class: 'row' },
        h('button', { class: 'btn', type: 'button', disabled: !s.has_key, onclick: async () => {
          result.textContent = 'Testing…';
          try {
            const r = await api.post('settings/test_key', {});
            ui.clear(result);
            if (r.ok) {
              const i = r.info || {};
              const usage = typeof i.usage === 'number' ? `$${i.usage.toFixed(4)}` : '?';
              result.append(h('span', { class: 'badge ok' }, 'OK'), ' ', r.message, ' ',
                `usage ${usage} · limit ${i.limit ?? 'none'}`
                + (i.limit_remaining !== undefined && i.limit_remaining !== null ? ` · remaining $${i.limit_remaining}` : '')
                + (i.is_free_tier ? ' · free tier' : ''));
            } else result.append(h('span', { class: 'badge bad' }, 'Failed'), ' ', r.message);
          } catch (e) { ui.clear(result); result.append(h('span', { class: 'badge bad' }, 'Failed'), ' ', e.message); }
        } }, 'Test key'),
        h('button', { class: 'btn danger', type: 'button', disabled: !s.has_key, onclick: async () => {
          if (await ui.confirmBox({ title: 'Clear the stored key?', body: 'Iterations with OpenRouter will need a new key.', ok: 'Clear key', danger: true })) {
            put({ clear_key: true }, 'Key cleared');
          }
        } }, 'Clear key')),
      result);
  }

  // ---------------------------------------------------------------- default model
  function drawModel() {
    ui.clear(modelCard);
    let value = s.default_model;
    const m = ui.modelInput(value, { placeholder: '', onchange: (v) => { value = v; } });
    modelCard.append(h('h2', null, 'Default model'),
      h('p', { class: 'small muted' }, 'Used by every actor and the synthesizer unless the world, an actor or the synthesizer overrides it.'),
      m.el,
      h('div', { class: 'row' }, h('button', { class: 'btn primary', type: 'button', onclick: () => {
        if (!value) { ui.toast('The default model cannot be empty', 'bad'); return; }
        put({ default_model: value }, 'Default model saved');
      } }, 'Save default model')));
  }

  // ---------------------------------------------------------------- call parameters
  async function drawCall() {
    ui.clear(callCard);
    let info = null;
    try { info = ((await ui.loadModels()).models || []).find((x) => x.id === s.default_model) || null; } catch (_) { info = null; }
    const efforts = info && info.reasoning_efforts && info.reasoning_efforts.length ? ['', ...info.reasoning_efforts] : EFFORTS;
    const temp = h('input', { type: 'number', min: 0, max: 2, step: 0.1, value: s.temperature });
    const effort = h('select', null, efforts.map((e) => h('option', { value: e, selected: (s.reasoning_effort || '') === e },
      e || `model default${info && info.default_effort ? ` (${info.default_effort})` : ''}`)));
    const par = h('input', { type: 'number', min: 1, max: 32, step: 1, value: s.max_parallel });
    const tmo = h('input', { type: 'number', min: 10, max: 1800, step: 10, value: s.timeout_s });
    const mxt = h('input', { type: 'number', min: 256, step: 256, value: s.max_tokens ?? '', placeholder: 'provider default' });
    const think = h('input', { type: 'checkbox', checked: s.record_thinking !== false });
    callCard.append(h('h2', null, 'Calls'),
      h('div', { class: 'inline-fields' },
        ui.field('Temperature', temp, '0–2'),
        ui.field('Reasoning effort', effort, info && info.reasoning ? `${info.id}: ${info.reasoning_efforts.join(', ')}` : 'for models that reason'),
        ui.field('Max parallel calls', par, 'actors called at once'),
        ui.field('Timeout (seconds)', tmo, 'per call'),
        ui.field('Max tokens', mxt, 'blank = provider default')),
      h('label', { class: 'row small' }, think, 'Record the model\'s thinking (shown on the Audit page; reasoning models only, no extra cost: '
        + 'its tokens are billed either way)'),
      h('div', { class: 'row' }, h('button', { class: 'btn primary', type: 'button', onclick: () => put({
        temperature: temp.value, reasoning_effort: effort.value, max_parallel: par.value, timeout_s: tmo.value,
        max_tokens: mxt.value === '' ? null : mxt.value, record_thinking: think.checked }, 'Call settings saved') }, 'Save call settings')));
  }

  // ---------------------------------------------------------------- model list
  async function drawList(refresh = false) {
    ui.clear(listCard).append(h('h2', null, 'Model list'), h('div', { class: 'loading' }, 'Loading…'));
    let data;
    try { data = await ui.loadModels(refresh); } catch (e) { ui.clear(listCard).append(h('h2', null, 'Model list'), ui.errorBox(e)); return; }
    ui.clear(listCard);
    listCard.append(h('div', { class: 'card-head' }, h('h2', null, 'Model list'),
      h('button', { class: 'btn', type: 'button', onclick: () => drawList(true) }, 'Refresh')),
    h('p', { class: 'small muted' }, data.source === 'builtin' ? (data.note || '') :
      `${(data.models || []).length} models from OpenRouter's public list${data.fetched_at ? `, fetched ${new Date(data.fetched_at * 1000).toLocaleString()}` : ''}`
      + (data.note ? ` · ${data.note}` : '') + '. Use "Browse…" next to any model box to search it.'),
    h('div', null, h('button', { class: 'btn', type: 'button', onclick: async () => {
      const id = await ui.pickModel(s.default_model);
      if (id) put({ default_model: id }, `Default model: ${id}`);
    } }, 'Browse models…'), h('span', { class: 'small muted' }, ' choosing one makes it the default model')));
  }

  drawKey();
  drawModel();
  drawCall();
  drawList();
}
