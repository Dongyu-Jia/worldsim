// Actors: the list and an editor (role, what they care about, decision questions, autonomous actions, private info,
// visible fields, model). Edits stay in a draft until "Save actors" (one PUT of the whole list, validated by the server).
const MAX_QUESTIONS = 8;
export async function render(root, ctx) {
  const { api, ui, wid } = ctx;
  const h = ui.h;
  const world = ctx.world;
  const zh = /中|chinese|^zh/i.test(world.language || '');
  let draft = structuredClone(world.actors);
  let current = draft.length ? 0 : -1;
  let dirty = false;

  const listBox = h('div', { class: 'item-list' });
  const editor = h('div');
  const status = h('span', { class: 'dirty' });
  const errors = h('div');
  const saveBtn = h('button', { class: 'btn primary', type: 'button', onclick: save }, 'Save actors');
  const revertBtn = h('button', { class: 'btn', type: 'button', onclick: revert }, 'Revert');

  root.append(
    h('div', { class: 'card' }, h('div', { class: 'card-head' }, h('h2', null, `Actors (${draft.length})`),
      h('div', { class: 'row' },
        h('button', { class: 'btn', type: 'button', onclick: addActor }, '+ Add actor'))),
    h('p', { class: 'small muted' }, 'Each actor is called once per iteration (in parallel) with the world description, its role, '
      + 'what it cares about, its private information and the current state. It answers every decision question in text, '
      + 'and may take autonomous actions of its own beyond them. Disabled actors sit out.')),
    errors,
    h('div', { class: 'split' }, h('div', { class: 'card' }, listBox), h('div', null, editor)),
    h('div', { class: 'savebar' }, saveBtn, revertBtn, status));

  function setDirty(d) {
    dirty = d;
    ctx.setDirty(d);
    status.textContent = d ? 'Unsaved changes' : '';
    saveBtn.disabled = !d;
    revertBtn.disabled = !d;
  }
  setDirty(false);

  function drawList() {
    ui.clear(listBox);
    if (!draft.length) listBox.append(h('div', { class: 'empty' }, 'No actors yet.'));
    draft.forEach((a, i) => {
      listBox.append(h('button', { type: 'button', class: `item${i === current ? ' active' : ''}${a.enabled ? '' : ' off'}`,
        onclick: () => { current = i; drawList(); drawEditor(); } },
      h('span', { class: 'nm' }, a.name || a.id || '(unnamed)'),
      h('span', { class: 'row' }, h('span', { class: 'badge' }, `${a.decision_questions.length} Q`),
        a.autonomy?.enabled ? h('span', { class: 'badge', title: 'autonomous actions per iteration' }, `≤${a.autonomy.max_actions} auto`) : null,
        a.model ? h('span', { class: 'badge accent', title: a.model }, 'model') : null)));
    });
  }

  function touch() { setDirty(true); drawList(); }

  function drawEditor() {
    ui.clear(editor);
    const a = draft[current];
    if (!a) { editor.append(h('div', { class: 'card' }, h('div', { class: 'empty' }, 'Select or add an actor.'))); return; }
    const idInput = h('input', { type: 'text', value: a.id, class: ui.ID_RE.test(a.id) ? '' : 'bad' });
    idInput.addEventListener('input', () => { a.id = idInput.value.trim(); idInput.classList.toggle('bad', !ui.ID_RE.test(a.id)); touch(); });
    const nameInput = h('input', { type: 'text', value: a.name });
    nameInput.addEventListener('input', () => { a.name = nameInput.value; touch(); });
    const enabled = h('input', { type: 'checkbox', checked: a.enabled, onchange: (e) => { a.enabled = e.target.checked; touch(); } });
    const role = h('textarea', { rows: 5 }); role.value = a.role;
    role.addEventListener('input', () => { a.role = role.value; touch(); });
    const priv = h('textarea', { rows: 10, class: 'mono' }); priv.value = a.private_info;
    priv.addEventListener('input', () => { a.private_info = priv.value; touch(); });
    const model = ui.modelInput(a.model, { placeholder: world.model?.model || ctx.settings?.default_model || 'the default model',
      onchange: (v) => { a.model = v; touch(); } });

    // decision questions: answered in text every iteration
    const qBox = h('div', { class: 'list-editor' });
    const drawQs = () => {
      ui.clear(qBox);
      const qs = a.decision_questions;
      qs.forEach((q, i) => {
        const id = h('input', { type: 'text', value: q.id, placeholder: 'id', 'aria-label': 'Question id',
          class: ui.ID_RE.test(q.id) ? 'qid' : 'qid bad' });
        id.addEventListener('input', () => { q.id = id.value.trim(); id.classList.toggle('bad', !ui.ID_RE.test(q.id)); touch(); });
        const text = h('textarea', { rows: 2, placeholder: 'the question, e.g. 本季政策利率怎么调、调多少？', 'aria-label': 'Question text',
          class: q.text.trim() ? '' : 'bad' });
        text.value = q.text;
        text.addEventListener('input', () => { q.text = text.value; text.classList.toggle('bad', !q.text.trim()); touch(); });
        const guide = h('textarea', { rows: 2, placeholder: 'guidance (optional): what a good answer covers, typical options or magnitudes, units',
          'aria-label': 'Question guidance' });
        guide.value = q.guidance || '';
        guide.addEventListener('input', () => { q.guidance = guide.value; touch(); });
        const req = h('label', { class: 'check small' }, h('input', { type: 'checkbox', checked: q.required !== false,
          onchange: (e) => { q.required = e.target.checked; touch(); } }), 'Required');
        const move = (d) => { const j = i + d; if (j < 0 || j >= qs.length) return;
          [qs[i], qs[j]] = [qs[j], qs[i]]; touch(); drawQs(); };
        qBox.append(h('div', { class: 'dq-row' },
          h('div', { class: 'dq-head' }, h('span', { class: 'dq-n' }, `Q${i + 1}`), id, req,
            h('div', { class: 'row-tools' },
              h('button', { class: 'btn tiny', type: 'button', title: 'Move up', 'aria-label': 'Move up', onclick: () => move(-1) }, '↑'),
              h('button', { class: 'btn tiny', type: 'button', title: 'Move down', 'aria-label': 'Move down', onclick: () => move(1) }, '↓'),
              h('button', { class: 'btn tiny danger', type: 'button', title: 'Remove', 'aria-label': 'Remove question',
                onclick: () => { qs.splice(i, 1); touch(); drawQs(); } }, '✕'))),
          text, guide));
      });
      if (!qs.length) qBox.append(h('div', { class: 'hint small muted' }, 'No questions: the actor only takes autonomous actions.'));
      qBox.append(h('div', null, h('button', { class: 'btn tiny', type: 'button', disabled: qs.length >= MAX_QUESTIONS, onclick: () => {
        const taken = new Set(qs.map((x) => x.id));
        qs.push({ id: ui.uniqueId(`q${qs.length + 1}`, taken), text: '', guidance: '', required: true });
        touch(); drawQs();
        qBox.querySelectorAll('textarea')[2 * (qs.length - 1)]?.focus();
      } }, '+ Add question')));
    };
    drawQs();

    // autonomous actions: beyond the questions, up to a maximum
    a.autonomy = a.autonomy || { enabled: true, max_actions: 3, guidance: '' };
    const auto = a.autonomy;
    const autoMax = h('input', { type: 'number', min: 1, max: 10, step: 1, value: auto.max_actions, disabled: !auto.enabled,
      'aria-label': 'Maximum autonomous actions per iteration' });
    autoMax.addEventListener('input', () => {
      const n = Number(autoMax.value);
      auto.max_actions = Number.isInteger(n) ? n : autoMax.value;
      autoMax.classList.toggle('bad', !(Number.isInteger(n) && n >= 1 && n <= 10)); touch();
    });
    const autoGuide = h('textarea', { rows: 3, disabled: !auto.enabled, placeholder: 'what kind of actions fit this actor (optional)',
      'aria-label': 'Autonomous action guidance' });
    autoGuide.value = auto.guidance || '';
    autoGuide.addEventListener('input', () => { auto.guidance = autoGuide.value; touch(); });
    const autoOn = h('input', { type: 'checkbox', checked: auto.enabled, onchange: (e) => {
      auto.enabled = e.target.checked; autoMax.disabled = !auto.enabled; autoGuide.disabled = !auto.enabled; touch();
    } });
    const autoBox = h('div', { class: 'auto-box' },
      h('div', { class: 'row' }, h('label', { class: 'check' }, autoOn, 'Allow autonomous actions'),
        h('label', { class: 'check small' }, 'at most', autoMax, 'per iteration')),
      autoGuide,
      h('span', { class: 'hint' }, 'Each action: a title, what exactly is done, its target (who or what it acts on) and the intended effect.'));

    // visible fields
    const visBox = h('div');
    const drawVis = () => {
      ui.clear(visBox);
      const all = a.visible_fields === null || a.visible_fields === undefined;
      visBox.append(h('div', { class: 'row' },
        h('label', { class: 'check' }, h('input', { type: 'radio', name: `vis-${current}`, checked: all,
          onchange: () => { a.visible_fields = null; touch(); drawVis(); } }), 'All fields'),
        h('label', { class: 'check' }, h('input', { type: 'radio', name: `vis-${current}`, checked: !all,
          onchange: () => { a.visible_fields = world.schema.fields.map((f) => f.id); touch(); drawVis(); } }), 'Only selected fields')));
      if (!all) {
        const set = new Set(a.visible_fields);
        const list = h('div', { class: 'vis-list' });
        let group = null;
        for (const f of world.schema.fields) {
          if (f.group !== group) { group = f.group; list.append(h('div', { class: 'grp' }, group)); }
          list.append(h('label', { class: 'check' }, h('input', { type: 'checkbox', checked: set.has(f.id), onchange: (e) => {
            if (e.target.checked) set.add(f.id); else set.delete(f.id);
            a.visible_fields = world.schema.fields.map((x) => x.id).filter((x) => set.has(x)); touch();
          } }), f.label));
        }
        visBox.append(list, h('div', { class: 'hint small muted' }, `${set.size} of ${world.schema.fields.length} fields visible to this actor`));
      }
    };
    drawVis();

    editor.append(h('div', { class: 'card' },
      h('div', { class: 'card-head' }, h('h2', null, a.name || '(unnamed actor)'),
        h('div', { class: 'row' },
          h('button', { class: 'btn', type: 'button', onclick: duplicate }, 'Duplicate'),
          h('button', { class: 'btn danger', type: 'button', onclick: remove }, 'Delete'))),
      h('div', { class: 'form' },
        h('div', { class: 'inline-fields' }, ui.field('Name', nameInput), ui.field('ID', idInput, 'lower-case letters, digits, _'),
          h('div', { class: 'field' }, h('span', null, 'Status'), h('label', { class: 'check' }, enabled, 'Enabled'))),
        ui.field('Role (who they are)', role),
        h('div', { class: 'field' }, h('span', null, 'Cares about (goals, interests, constraints)'),
          ui.textList(a.cares_about, { placeholder: 'something this actor cares about', addLabel: 'Add item',
            onchange: (list) => { a.cares_about = list; touch(); } })),
        h('div', { class: 'field' }, h('span', null, `Decision questions (${a.decision_questions.length})`),
          h('span', { class: 'hint' }, 'Answered in text every iteration. Use the guidance for the constraints: units, '
            + 'typical options, magnitudes. A required question left unanswered after one repair is marked “unanswered”.'), qBox),
        h('div', { class: 'field' }, h('span', null, 'Autonomous actions'), autoBox),
        ui.field('Private information (only this actor sees it)', priv),
        h('div', { class: 'field' }, h('span', null, 'Visible state fields'), visBox),
        h('div', { class: 'field' }, h('span', null, 'Model override'), model.el))));
  }

  function addActor() {
    const taken = new Set(draft.map((a) => a.id));
    draft.push({ id: ui.uniqueId('new_actor', taken), name: 'New actor', enabled: true, role: '', cares_about: [],
      decision_questions: [{ id: 'q1', text: zh ? '本期你会怎么做？' : 'What do you do this period?', guidance: '', required: true }],
      autonomy: { enabled: true, max_actions: 3, guidance: '' }, private_info: '', visible_fields: null, model: '' });
    current = draft.length - 1;
    touch(); drawEditor();
  }

  function duplicate() {
    const a = structuredClone(draft[current]);
    const taken = new Set(draft.map((x) => x.id));
    a.id = ui.uniqueId(`${a.id}_copy`.slice(0, 48), taken);
    a.name = `${a.name} (copy)`;
    draft.splice(current + 1, 0, a);
    current += 1;
    touch(); drawEditor();
  }

  async function remove() {
    const a = draft[current];
    if (!(await ui.confirmBox({ title: `Delete ${a.name}?`, body: 'The actor is removed when you save. Past iterations keep its outputs.', ok: 'Delete', danger: true }))) return;
    draft.splice(current, 1);
    current = Math.min(current, draft.length - 1);
    touch(); drawEditor();
  }

  async function save() {
    ui.clear(errors);
    try {
      const res = await api.put(`worlds/${api.enc(wid)}/actors`, { actors: draft });
      world.actors = res.world.actors;
      draft = structuredClone(world.actors);
      current = Math.min(Math.max(current, 0), draft.length - 1);
      setDirty(false);
      drawList(); drawEditor();
      ui.toast('Actors saved', 'ok');
    } catch (e) {
      errors.append(ui.errorBox(e, 'Not saved.'));
      errors.scrollIntoView({ block: 'nearest' });
    }
  }

  function revert() {
    draft = structuredClone(world.actors);
    current = Math.min(Math.max(current, 0), draft.length - 1);
    setDirty(false);
    ui.clear(errors);
    drawList(); drawEditor();
  }

  drawList();
  drawEditor();
}
