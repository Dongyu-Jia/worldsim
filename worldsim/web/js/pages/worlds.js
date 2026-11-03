// Worlds: the current world's settings (name, language, period step, history, model), the list of
// worlds (open, duplicate, export, delete) and creating one (blank, from the template, import a JSON file).
export async function render(root, ctx) {
  const { api, ui } = ctx;
  const h = ui.h;
  const settingsCard = h('div', { class: 'card' });
  const listCard = h('div', { class: 'card' });
  const createCard = h('div', { class: 'card' });
  root.append(settingsCard, listCard, createCard);

  let world = null;
  if (ctx.wid) {
    try { world = (await api.get(`worlds/${api.enc(ctx.wid)}`)).world; } catch (_) { world = null; }
  }

  // ---------------------------------------------------------------- the current world's settings
  function drawSettings() {
    ui.clear(settingsCard);
    if (!world) { settingsCard.append(h('h2', null, 'Current world'), h('div', { class: 'empty' }, 'No world selected.')); return; }
    const d = { name: world.name, language: world.language,
      period_step: { ...world.period_step }, history_window: world.history_window, model: { ...world.model } };
    const status = h('span', { class: 'dirty' });
    const touch = () => { ctx.setDirty(true); status.textContent = 'Unsaved changes'; };
    const text = (key, attrs = {}) => { const el = h('input', { type: 'text', value: d[key] ?? '', ...attrs });
      el.addEventListener('input', () => { d[key] = el.value; touch(); }); return el; };
    const unit = h('select', null, ['quarter', 'month', 'year'].map((u) => h('option', { value: u, selected: d.period_step.unit === u }, u)));
    unit.addEventListener('change', () => { d.period_step.unit = unit.value; touch(); });
    const count = h('input', { type: 'number', min: 1, max: 120, value: d.period_step.count });
    count.addEventListener('input', () => { d.period_step.count = Number(count.value) || 1; touch(); });
    const win = h('input', { type: 'number', min: 0, max: 20, value: d.history_window });
    win.addEventListener('input', () => { d.history_window = Number(win.value); touch(); });
    const temp = h('input', { type: 'number', min: 0, max: 2, step: 0.1, value: d.model.temperature ?? '', placeholder: 'global' });
    temp.addEventListener('input', () => { d.model.temperature = temp.value === '' ? null : Number(temp.value); touch(); });
    const effort = h('select', null, ['', 'none', 'minimal', 'low', 'medium', 'high', 'xhigh', 'max'].map((e) =>
      h('option', { value: e, selected: (d.model.reasoning_effort || '') === e }, e || 'global setting')));
    effort.addEventListener('change', () => { d.model.reasoning_effort = effort.value; touch(); });
    const model = ui.modelInput(d.model.model, { placeholder: ctx.settings?.default_model || 'the default model',
      onchange: (v) => { d.model.model = v; touch(); } });
    const errors = h('div');
    settingsCard.append(h('h2', null, `Current world: ${world.name}`),
      h('div', { class: 'form' },
        h('div', { class: 'inline-fields' }, ui.field('Name', text('name')),
          ui.field('Output language', text('language', { placeholder: 'e.g. 中文, English' }), 'generated text is written in it')),
        h('div', { class: 'field' }, h('span', null, 'World description'),
          h('span', { class: 'hint' }, 'Shown to every actor and the synthesizer; edit it on the ', h('a', { href: '#schema' }, 'Schema page'), '.')),
        h('div', { class: 'inline-fields' }, ui.field('Period step', unit), ui.field('Steps per iteration', count, 'e.g. 1 quarter'),
          ui.field('History window', win, 'recent steps shown in prompts')),
        h('div', { class: 'inline-fields' }, ui.field('Temperature (world)', temp), ui.field('Reasoning effort (world)', effort)),
        h('div', { class: 'field' }, h('span', null, 'Model for this world (actors and synthesizer can override it)'), model.el),
        errors,
        h('div', { class: 'row' }, h('button', { class: 'btn primary', type: 'button', onclick: async () => {
          ui.clear(errors);
          try {
            const res = await api.put(`worlds/${api.enc(world.id)}`, d);
            world = res.world;
            ctx.setDirty(false); status.textContent = '';
            await ctx.reloadWorlds();
            ui.toast('World saved', 'ok');
            drawSettings(); drawList();
          } catch (e) { errors.append(ui.errorBox(e, 'Not saved.')); }
        } }, 'Save world settings'), status)));
  }

  // ---------------------------------------------------------------- list
  function drawList() {
    ui.clear(listCard);
    const worlds = ctx.worlds || [];
    listCard.append(h('div', { class: 'card-head' }, h('h2', null, `Worlds (${worlds.length})`)));
    if (!worlds.length) { listCard.append(h('div', { class: 'empty' }, 'No worlds yet: create one below.')); return; }
    listCard.append(h('table', { class: 'list' },
      h('thead', null, h('tr', null, ['Name', 'Steps', 'Period', 'Actors', 'Fields', 'Updated', ''].map((t) => h('th', null, t)))),
      h('tbody', null, worlds.map((w) => h('tr', { class: w.id === ctx.wid ? 'current' : '' },
        h('td', null, h('strong', null, w.name), w.id === ctx.wid ? h('span', { class: 'badge accent' }, ' current') : null,
          h('div', { class: 'small muted' }, w.description)),
        h('td', null, String(w.steps)), h('td', null, w.period || ''), h('td', null, String(w.actors)), h('td', null, String(w.fields)),
        h('td', { class: 'small muted' }, ui.when(w.updated)),
        h('td', null, h('div', { class: 'row' },
          w.id !== ctx.wid ? h('button', { class: 'btn tiny primary', type: 'button', onclick: () => open(w.id) }, 'Open') : null,
          h('button', { class: 'btn tiny', type: 'button', onclick: () => duplicate(w) }, 'Duplicate'),
          h('a', { class: 'btn tiny', href: `/api/worlds/${api.enc(w.id)}/export`, download: `${w.id}.worldsim.json` }, 'Export'),
          h('button', { class: 'btn tiny danger', type: 'button', onclick: () => remove(w) }, 'Delete'))))))));
  }

  async function open(wid) { await ctx.setWorld(wid); ctx.go('sim'); }

  async function duplicate(w) {
    const name = await ui.promptBox({ title: `Duplicate ${w.name}`, label: 'Name (the copy has no history)', value: `${w.name} (copy)`, ok: 'Duplicate' });
    if (name === null) return;
    try {
      const res = await api.post(`worlds/${api.enc(w.id)}/duplicate`, { name });
      ui.toast(`Created “${res.world.name}”`, 'ok');
      await ctx.setWorld(res.world.id);
    } catch (e) { ui.toast(e.message, 'bad'); }
  }

  async function remove(w) {
    const ok = await ui.confirmBox({ title: `Delete “${w.name}”?`, danger: true, ok: 'Delete permanently',
      body: `This deletes the world, its ${w.steps} step(s) and every saved iteration from this machine. Export it first if you may want it back.` });
    if (!ok) return;
    try {
      await api.del(`worlds/${api.enc(w.id)}`, { confirm: true });
      ui.toast(`Deleted “${w.name}”`, 'ok');
      const ws = await ctx.reloadWorlds();
      await ctx.setWorld(ws.some((x) => x.id === ctx.wid) && ctx.wid !== w.id ? ctx.wid : (ws[0]?.id || null));
    } catch (e) { ui.toast(e.message, 'bad'); }
  }

  // ---------------------------------------------------------------- create
  function drawCreate() {
    ui.clear(createCard);
    const file = h('input', { type: 'file', accept: '.json,application/json', 'aria-label': 'World JSON file' });
    const errors = h('div');
    file.addEventListener('change', async () => {
      ui.clear(errors);
      const f = file.files && file.files[0];
      if (!f) return;
      try {
        const data = JSON.parse(await f.text());
        const res = await api.post('worlds/import', data);
        ui.toast(`Imported “${res.world.name}”`, 'ok');
        await ctx.setWorld(res.world.id);
      } catch (e) { errors.append(ui.errorBox(e, 'Import failed.')); }
      file.value = '';
    });
    const create = async (mode) => {
      const name = await ui.promptBox({ title: mode === 'template' ? 'New world from the template' : 'New blank world', label: 'Name',
        value: mode === 'template' ? '美国与世界 · 2026Q4' : 'New world', ok: 'Create' });
      if (name === null) return;
      try {
        const res = await api.post('worlds', { mode, name });
        ui.toast(`Created “${res.world.name}”`, 'ok');
        await ctx.setWorld(res.world.id);
      } catch (e) { ui.toast(e.message, 'bad'); }
    };
    createCard.append(h('h2', null, 'Create a world'),
      h('div', { class: 'row' },
        h('button', { class: 'btn', type: 'button', onclick: () => create('blank') }, '+ Blank world'),
        h('button', { class: 'btn', type: 'button', onclick: () => create('template') }, '+ From template: 美国与世界 · 2026Q4')),
      h('hr'),
      ui.field('Import a world (.json exported by WorldSim; history included when present)', file), errors);
  }

  drawSettings();
  drawList();
  drawCreate();
}
