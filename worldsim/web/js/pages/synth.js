// Synthesizer: its instructions (how decisions turn into consequences, consistency rules), how many items it writes
// per call (multi-shot: digest, indicator chunks, question chunks, narrative) and a model override.
export async function render(root, ctx) {
  const { api, ui, wid } = ctx;
  const h = ui.h;
  const world = ctx.world;
  const draft = { ...world.synthesizer };
  const status = h('span', { class: 'dirty' });
  const errors = h('div');
  const saveBtn = h('button', { class: 'btn primary', type: 'button', onclick: save }, 'Save synthesizer');

  const setDirty = (d) => { ctx.setDirty(d); status.textContent = d ? 'Unsaved changes' : ''; saveBtn.disabled = !d; };
  const instr = h('textarea', { class: 'tall', rows: 18, 'aria-label': 'Synthesizer instructions' });
  instr.value = draft.instructions || '';
  instr.addEventListener('input', () => { draft.instructions = instr.value; setDirty(true); });
  const counts = (ctx.meta && ctx.meta.calls_per_iteration) || null;
  const callsHint = h('span', { class: 'hint' });
  const drawCalls = (c) => {
    callsHint.textContent = c ? `Saved setting: ${c.synthesizer} synthesizer calls per iteration (1 digest, ${c.synth_values} indicator, `
      + `${c.synth_statuses} question, 1 narrative) for ${world.schema.fields.length} indicators and `
      + `${(world.schema.questions || []).length} questions; plus ${c.actors} actor calls.` : '';
  };
  drawCalls(counts);
  const maxItems = h('input', { type: 'number', class: 'num-narrow', min: 1, max: 30, step: 1, value: draft.max_items_per_call ?? 6,
    'aria-label': 'Maximum items per synthesizer call' });
  maxItems.addEventListener('input', () => {
    const n = Number(maxItems.value);
    draft.max_items_per_call = Number.isInteger(n) ? n : maxItems.value;
    maxItems.classList.toggle('bad', !(Number.isInteger(n) && n >= 1 && n <= 30));
    setDirty(true);
  });
  const model = ui.modelInput(draft.model, { placeholder: world.model?.model || ctx.settings?.default_model || 'the default model',
    onchange: (v) => { draft.model = v; setDirty(true); } });

  root.append(
    h('div', { class: 'card' },
      h('h2', null, 'Synthesizer'),
      h('p', { class: 'small muted' }, 'After every enabled actor has decided, the synthesizer writes the next state in several short calls, '
        + 'one after another: a digest of the period (本季要事: the main events, how the decisions interact, which forces dominate), '
        + 'the indicators in chunks, the predefined state questions in chunks, and finally the ~100-字 narrative. Every call sees the '
        + 'world description, these instructions, the previous state, every actor\'s answers and actions and everything written '
        + 'earlier in the iteration, and answers only its own keys. A step that fails twice carries its items forward (flagged) '
        + 'and the iteration is marked partial.'),
      h('div', { class: 'form' },
        ui.field('Instructions (world-specific rules)', instr),
        h('label', { class: 'field' }, h('span', null, 'Items per call (x): at most this many indicators or questions in one call (1–30)'),
          h('div', { class: 'row' }, maxItems), h('span', { class: 'hint' }, 'Smaller chunks give each item more attention but cost more calls. '
            + 'Groups stay together when they fit; a larger group is split; small neighbouring groups are merged.'), callsHint),
        h('div', { class: 'field' }, h('span', null, 'Model override'), model.el))),
    errors,
    h('div', { class: 'savebar' }, saveBtn, status));
  setDirty(false);

  async function save() {
    ui.clear(errors);
    try {
      const res = await api.put(`worlds/${api.enc(wid)}/synthesizer`, draft);
      world.synthesizer = res.world.synthesizer;
      await ctx.reloadWorld();
      drawCalls(ctx.meta && ctx.meta.calls_per_iteration);
      setDirty(false);
      ui.toast('Synthesizer saved', 'ok');
    } catch (e) { errors.append(ui.errorBox(e, 'Not saved.')); }
  }
}
