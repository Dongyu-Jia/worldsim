// Schema: the world description, the indicator fields (add, edit, delete, reorder, groups) with their initial values,
// the predefined state questions (add, edit, delete, reorder, group, guidance) with their initial status, and the
// initial period and narrative. The initial state is editable only while the world has no later steps.
const TYPES = ['number', 'percent', 'integer', 'text', 'enum', 'boolean'];
const NUMERIC = new Set(['number', 'percent', 'integer']);

export async function render(root, ctx) {
  const { api, ui, wid } = ctx;
  const h = ui.h;
  const world = ctx.world;
  const steps = (ctx.meta && ctx.meta.steps) || [0];
  const hasHistory = steps.length && steps[steps.length - 1] > 0;
  const cjk = /中|chinese|^zh|日本|japanese|^ja|한국|korean|^ko/i.test(world.language || '');
  const NARRATIVE_MAX = cjk ? 120 : 600;
  let fields = structuredClone(world.schema.fields);
  let questions = structuredClone(world.schema.questions || []);
  let vals = structuredClone(world.initial_state.values || {});
  let statuses = structuredClone(world.initial_state.statuses || {});
  let narrative = world.initial_state.narrative || '';
  let period = world.initial_state.period || '';
  let description = world.description || '';
  let editing = null;              // index of the field whose editor is open
  let editingQ = null;             // index of the question whose editor is open

  const status = h('span', { class: 'dirty' });
  const errors = h('div');
  const saveBtn = h('button', { class: 'btn primary', type: 'button', onclick: save }, hasHistory ? 'Save schema' : 'Save schema & initial state');
  const revertBtn = h('button', { class: 'btn', type: 'button', onclick: revert }, 'Revert');
  const descBox = h('div');
  const fieldsBox = h('div');
  const questionsBox = h('div');
  const initBox = h('div');
  const groupsList = h('datalist', { id: 'schema-groups' });
  const qGroupsList = h('datalist', { id: 'question-groups' });

  root.append(
    h('div', { class: 'card' }, h('h2', null, 'World description'),
      h('p', { class: 'small muted' }, 'A fixed text shown to every actor and to the synthesizer in every iteration: what this world is, '
        + 'its scope, the setting and what one step means. The synthesizer never rewrites it.'), descBox),
    h('div', { class: 'card' },
      h('div', { class: 'card-head' }, h('h2', null, 'Indicators'),
        h('button', { class: 'btn', type: 'button', onclick: addField }, '+ Add field')),
      h('p', { class: 'small muted' }, 'Every state has a value for each field. The synthesizer writes them in chunks; '
        + 'a value outside the plausible min/max is kept but flagged. The tag (e.g. 未确认, 估算) is shown next to the field.'),
      hasHistory ? h('div', { class: 'banner info' }, `This world has ${steps[steps.length - 1]} step(s) after the initial state: `
        + 'fields and questions can still be edited (a new one starts empty and the synthesizer fills it), but the initial state is '
        + 'read-only. Rewind to step 0 or fork the world to change it.') : null,
      groupsList, fieldsBox),
    h('div', { class: 'card' },
      h('div', { class: 'card-head' }, h('h2', null, 'Predefined state questions'),
        h('button', { class: 'btn', type: 'button', onclick: addQuestion }, '+ Add question')),
      h('p', { class: 'small muted' }, 'Every state holds a natural-language status (现状描述) for each question. The synthesizer '
        + 'rewrites every status in every iteration (restating it when nothing changed); the guidance says what a status should cover.'),
      qGroupsList, questionsBox),
    h('div', { class: 'card' }, h('h2', null, 'Initial state (step 0)'), initBox),
    errors,
    h('div', { class: 'savebar' }, saveBtn, revertBtn, status));

  function setDirty(d) {
    ctx.setDirty(d);
    status.textContent = d ? 'Unsaved changes' : '';
    saveBtn.disabled = !d;
    revertBtn.disabled = !d;
  }
  setDirty(false);
  const touch = () => setDirty(true);

  function drawDescription() {
    ui.clear(descBox);
    const ta = h('textarea', { class: 'tall', rows: 10, 'aria-label': 'World description' });
    ta.value = description;
    ta.addEventListener('input', () => { description = ta.value; touch(); });
    descBox.append(ta);
  }

  function valueInput(f) {
    const v = vals[f.id];
    const dis = !!hasHistory;
    if (f.type === 'boolean') {
      const s = h('select', { disabled: dis, 'aria-label': `Initial value of ${f.label}` },
        h('option', { value: '' }, '—'), h('option', { value: 'true', selected: v === true }, 'true'),
        h('option', { value: 'false', selected: v === false }, 'false'));
      s.addEventListener('change', () => { vals[f.id] = s.value === '' ? null : s.value === 'true'; touch(); });
      return s;
    }
    if (f.type === 'enum') {
      const s = h('select', { disabled: dis, 'aria-label': `Initial value of ${f.label}` }, h('option', { value: '' }, '—'),
        (f.options || []).map((o) => h('option', { value: o, selected: v === o }, o)));
      s.addEventListener('change', () => { vals[f.id] = s.value === '' ? null : s.value; touch(); });
      return s;
    }
    const inp = h('input', { type: 'text', value: v === null || v === undefined ? '' : String(v), disabled: dis,
      inputmode: NUMERIC.has(f.type) ? 'decimal' : null, 'aria-label': `Initial value of ${f.label}`,
      placeholder: NUMERIC.has(f.type) ? (f.unit || 'number') : 'text' });
    inp.addEventListener('input', () => {
      const raw = inp.value.trim();
      if (NUMERIC.has(f.type)) {
        const n = Number(raw);
        vals[f.id] = raw === '' ? null : (Number.isFinite(n) ? n : raw);
        inp.classList.toggle('bad', raw !== '' && !Number.isFinite(n));
      } else vals[f.id] = inp.value;
      touch();
    });
    return inp;
  }

  function drawFields() {
    ui.clear(fieldsBox);
    ui.clear(groupsList);
    const groups = [...new Set(fields.map((f) => f.group))];
    groups.forEach((g) => groupsList.append(h('option', { value: g })));
    if (!fields.length) fieldsBox.append(h('div', { class: 'empty' }, 'No fields yet.'));
    let group = null;
    fields.forEach((f, i) => {
      if (f.group !== group) {
        group = f.group;
        fieldsBox.append(h('h3', { class: 'small muted' }, `${group} · ${fields.filter((x) => x.group === group).length}`));
      }
      const move = (d) => { const j = i + d; if (j < 0 || j >= fields.length) return;
        [fields[i], fields[j]] = [fields[j], fields[i]]; if (editing === i) editing = j; else if (editing === j) editing = i; touch(); drawFields(); };
      fieldsBox.append(h('div', { class: 'schema-row' },
        h('div', { class: 'ord' },
          h('button', { class: 'btn tiny', type: 'button', title: 'Move up', 'aria-label': 'Move up', onclick: () => move(-1) }, '↑'),
          h('button', { class: 'btn tiny', type: 'button', title: 'Move down', 'aria-label': 'Move down', onclick: () => move(1) }, '↓')),
        h('div', null, h('div', { class: 'fl-label' }, f.label || '(no label)', ' ', f.tag ? h('span', { class: 'badge tag' }, f.tag) : null),
          h('div', { class: 'fl-id' }, f.id)),
        h('div', { class: 's-type small' }, h('span', { class: 'badge' }, f.type), ' ', h('span', { class: 'muted' }, f.unit || '')),
        h('div', { class: 's-init' }, valueInput(f)),
        h('div', { class: 'tools' },
          h('button', { class: 'btn tiny', type: 'button', onclick: () => { editing = editing === i ? null : i; drawFields(); } },
            editing === i ? 'Close' : 'Edit'),
          h('button', { class: 'btn tiny danger', type: 'button', title: 'Delete field', 'aria-label': 'Delete field', onclick: () => removeField(i) }, '✕'))));
      if (editing === i) fieldsBox.append(fieldEditor(f, i));
    });
  }

  function fieldEditor(f, i) {
    const inp = (key, attrs = {}) => {
      const el = h('input', { type: 'text', value: f[key] ?? '', ...attrs });
      el.addEventListener('input', () => { f[key] = el.value; touch(); });
      return el;
    };
    const idInput = h('input', { type: 'text', value: f.id, class: ui.ID_RE.test(f.id) ? '' : 'bad' });
    idInput.addEventListener('change', () => {
      const old = f.id;
      const nid = idInput.value.trim();
      idInput.classList.toggle('bad', !ui.ID_RE.test(nid));
      if (nid !== old) {
        if (fields.some((x, j) => j !== i && x.id === nid) || questions.some((q) => q.id === nid)) {
          ui.toast(`The id ${nid} is already used`, 'bad'); idInput.value = old; return;
        }
        vals[nid] = vals[old]; delete vals[old];
        f.id = nid; touch(); drawFields();
      }
    });
    const label = inp('label');
    label.addEventListener('change', () => drawFields());
    const groupIn = h('input', { type: 'text', value: f.group, list: 'schema-groups' });
    groupIn.addEventListener('change', () => { f.group = groupIn.value.trim() || 'General'; touch(); drawFields(); });
    const type = h('select', null, TYPES.map((t) => h('option', { value: t, selected: f.type === t }, t)));
    type.addEventListener('change', () => {
      f.type = type.value;
      if (f.type === 'enum' && !(f.options || []).length) f.options = ['option a', 'option b'];
      vals[f.id] = null;
      touch(); drawFields();
    });
    const desc = h('textarea', { rows: 3 }); desc.value = f.description || '';
    desc.addEventListener('input', () => { f.description = desc.value; touch(); });
    const opts = h('textarea', { rows: 4, placeholder: 'one option per line' }); opts.value = (f.options || []).join('\n');
    opts.addEventListener('change', () => {
      f.options = opts.value.split('\n').map((s) => s.trim()).filter(Boolean);
      if (vals[f.id] !== null && !f.options.includes(vals[f.id])) vals[f.id] = null;
      touch(); drawFields();
    });
    const numIn = (key) => {
      const el = h('input', { type: 'text', inputmode: 'decimal', value: f[key] === null || f[key] === undefined ? '' : String(f[key]), placeholder: 'none' });
      el.addEventListener('input', () => {
        const raw = el.value.trim(); const n = Number(raw);
        f[key] = raw === '' ? null : (Number.isFinite(n) ? n : raw);
        el.classList.toggle('bad', raw !== '' && !Number.isFinite(n)); touch();
      });
      return el;
    };
    return h('div', { class: 'schema-edit form' },
      h('div', { class: 'inline-fields' }, ui.field('ID', idInput, 'lower-case letters, digits, _'), ui.field('Label', label),
        ui.field('Group', groupIn), ui.field('Type', type), ui.field('Unit', inp('unit', { placeholder: 'e.g. %, 十亿美元' })),
        ui.field('Tag', inp('tag', { placeholder: 'e.g. 未确认' }))),
      ui.field('Description (what it measures; shown to the synthesizer)', desc),
      f.type === 'enum' ? ui.field('Options', opts) : null,
      NUMERIC.has(f.type) ? h('div', { class: 'inline-fields' }, ui.field('Plausible min', numIn('min')), ui.field('Plausible max', numIn('max'))) : null,
      ui.field('Historical range note', inp('hist', { placeholder: 'e.g. 1954–2026: 0.05 … 19.1, median 4.3' })));
  }

  function addField() {
    const taken = new Set([...fields.map((f) => f.id), ...questions.map((q) => q.id)]);
    const group = fields.length ? fields[fields.length - 1].group : 'General';
    fields.push({ id: ui.uniqueId('new_field', taken), label: 'New field', group, type: 'number', unit: '', description: '',
      options: [], min: null, max: null, hist: '', tag: '' });
    vals[fields[fields.length - 1].id] = null;
    editing = fields.length - 1;
    touch(); drawFields();
    fieldsBox.lastElementChild?.scrollIntoView({ block: 'nearest' });
  }

  async function removeField(i) {
    const f = fields[i];
    if (!(await ui.confirmBox({ title: `Delete the field ${f.label}?`, body: 'Saved states keep their old values, but the field is no longer shown or simulated.', ok: 'Delete', danger: true }))) return;
    fields.splice(i, 1);
    delete vals[f.id];
    editing = null;
    touch(); drawFields();
  }

  // ---------------------------------------------------------------- predefined state questions
  function drawQuestions() {
    ui.clear(questionsBox);
    ui.clear(qGroupsList);
    [...new Set(questions.map((q) => q.group))].forEach((g) => qGroupsList.append(h('option', { value: g })));
    if (!questions.length) questionsBox.append(h('div', { class: 'empty' }, 'No state questions yet.'));
    let group = null;
    questions.forEach((q, i) => {
      if (q.group !== group) {
        group = q.group;
        questionsBox.append(h('h3', { class: 'small muted' }, `${group} · ${questions.filter((x) => x.group === group).length}`));
      }
      const move = (d) => { const j = i + d; if (j < 0 || j >= questions.length) return;
        [questions[i], questions[j]] = [questions[j], questions[i]];
        if (editingQ === i) editingQ = j; else if (editingQ === j) editingQ = i; touch(); drawQuestions(); };
      const st = h('textarea', { rows: 3, disabled: !!hasHistory, placeholder: 'initial status (现状描述)',
        'aria-label': `Initial status of ${q.text}` });
      st.value = statuses[q.id] || '';
      st.addEventListener('input', () => { statuses[q.id] = st.value; touch(); });
      questionsBox.append(h('div', { class: 'sq-row' },
        h('div', { class: 'ord' },
          h('button', { class: 'btn tiny', type: 'button', title: 'Move up', 'aria-label': 'Move up', onclick: () => move(-1) }, '↑'),
          h('button', { class: 'btn tiny', type: 'button', title: 'Move down', 'aria-label': 'Move down', onclick: () => move(1) }, '↓')),
        h('div', { class: 'sq-main' },
          h('div', { class: 'fl-label' }, q.text || '(no question text)'),
          h('div', { class: 'fl-id' }, q.id, q.guidance ? h('span', { class: 'muted' }, ` · ${q.guidance}`) : null),
          st),
        h('div', { class: 'tools' },
          h('button', { class: 'btn tiny', type: 'button', onclick: () => { editingQ = editingQ === i ? null : i; drawQuestions(); } },
            editingQ === i ? 'Close' : 'Edit'),
          h('button', { class: 'btn tiny danger', type: 'button', title: 'Delete question', 'aria-label': 'Delete question',
            onclick: () => removeQuestion(i) }, '✕'))));
      if (editingQ === i) questionsBox.append(questionEditor(q, i));
    });
  }

  function questionEditor(q, i) {
    const idInput = h('input', { type: 'text', value: q.id, class: ui.ID_RE.test(q.id) ? '' : 'bad' });
    idInput.addEventListener('change', () => {
      const old = q.id;
      const nid = idInput.value.trim();
      idInput.classList.toggle('bad', !ui.ID_RE.test(nid));
      if (nid !== old) {
        if (questions.some((x, j) => j !== i && x.id === nid) || fields.some((f) => f.id === nid)) {
          ui.toast(`The id ${nid} is already used`, 'bad'); idInput.value = old; return;
        }
        statuses[nid] = statuses[old]; delete statuses[old];
        q.id = nid; touch(); drawQuestions();
      }
    });
    const text = h('textarea', { rows: 2, 'aria-label': 'Question text' }); text.value = q.text || '';
    text.addEventListener('input', () => { q.text = text.value; touch(); });
    text.addEventListener('change', () => drawQuestions());
    const groupIn = h('input', { type: 'text', value: q.group, list: 'question-groups' });
    groupIn.addEventListener('change', () => { q.group = groupIn.value.trim() || 'General'; touch(); drawQuestions(); });
    const guide = h('textarea', { rows: 2, placeholder: 'what the status should cover (optional)', 'aria-label': 'Question guidance' });
    guide.value = q.guidance || '';
    guide.addEventListener('input', () => { q.guidance = guide.value; touch(); });
    guide.addEventListener('change', () => drawQuestions());
    return h('div', { class: 'schema-edit form' },
      h('div', { class: 'inline-fields' }, ui.field('ID', idInput, 'lower-case letters, digits, _'), ui.field('Group', groupIn)),
      ui.field('Question', text), ui.field('Guidance (what the status should cover; shown to the synthesizer)', guide));
  }

  function addQuestion() {
    const taken = new Set([...fields.map((f) => f.id), ...questions.map((q) => q.id)]);
    const group = questions.length ? questions[questions.length - 1].group : 'General';
    const q = { id: ui.uniqueId(`sq${questions.length + 1}`, taken), text: '', group, guidance: '' };
    questions.push(q);
    statuses[q.id] = '';
    editingQ = questions.length - 1;
    touch(); drawQuestions();
    questionsBox.lastElementChild?.scrollIntoView({ block: 'nearest' });
  }

  async function removeQuestion(i) {
    const q = questions[i];
    if (!(await ui.confirmBox({ title: 'Delete this state question?', body: `${q.text || q.id}: saved states keep their old statuses, but the question is no longer shown or simulated.`, ok: 'Delete', danger: true }))) return;
    questions.splice(i, 1);
    delete statuses[q.id];
    editingQ = null;
    touch(); drawQuestions();
  }

  // ---------------------------------------------------------------- initial period and narrative
  function drawInit() {
    ui.clear(initBox);
    const dis = !!hasHistory;
    const periodIn = h('input', { type: 'text', value: period, disabled: dis, placeholder: 'e.g. 2026Q4' });
    periodIn.addEventListener('input', () => { period = periodIn.value; touch(); });
    const count = h('span', { class: 'hint' });
    const narr = h('textarea', { rows: 3, disabled: dis, 'aria-label': 'Initial narrative' }); narr.value = narrative;
    const recount = () => {
      const n = [...narr.value.trim()].length;
      count.textContent = `${n} / ${NARRATIVE_MAX} characters${n > NARRATIVE_MAX ? ' — over the limit the synthesizer must keep' : ''}`;
      count.classList.toggle('over', n > NARRATIVE_MAX);
    };
    narr.addEventListener('input', () => { narrative = narr.value; recount(); touch(); });
    recount();
    initBox.append(h('div', { class: 'form' },
      h('div', { class: 'inline-fields' }, ui.field('Period of step 0', periodIn, 'e.g. 2026Q4, 2026-11, 2026 or Turn 1')),
      h('p', { class: 'small muted' }, 'The initial values and statuses are edited in the lists above.'),
      h('label', { class: 'field' }, h('span', null, `Narrative: a summary of all values and statuses (${cjk ? 'about 100 字' : 'about 80 words'})`),
        narr, count)));
  }

  async function save() {
    ui.clear(errors);
    try {
      const body = { fields, questions };
      if (!hasHistory) { body.initial_values = vals; body.initial_statuses = statuses; }
      let res = await api.put(`worlds/${api.enc(wid)}/schema`, body);
      if (!hasHistory) {
        res = await api.put(`worlds/${api.enc(wid)}/initial`, { period, values: vals, statuses, narrative });
      }
      if (description !== (world.description || '')) {
        res = await api.put(`worlds/${api.enc(wid)}`, { description });
      }
      Object.assign(world, res.world);
      revert();
      ui.toast('Schema saved', 'ok');
    } catch (e) {
      errors.append(ui.errorBox(e, 'Not saved.'));
      errors.scrollIntoView({ block: 'nearest' });
    }
  }

  function revert() {
    fields = structuredClone(world.schema.fields);
    questions = structuredClone(world.schema.questions || []);
    vals = structuredClone(world.initial_state.values || {});
    statuses = structuredClone(world.initial_state.statuses || {});
    narrative = world.initial_state.narrative || '';
    period = world.initial_state.period || '';
    description = world.description || '';
    editing = null;
    editingQ = null;
    setDirty(false);
    drawDescription(); drawFields(); drawQuestions(); drawInit();
  }

  drawDescription();
  drawFields();
  drawQuestions();
  drawInit();
}
