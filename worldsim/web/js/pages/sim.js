// Simulation: the world description, the current (or a past) state (narrative, indicator values, the predefined state
// questions' statuses), one-click iterations with live progress per actor and per synthesizer step, the step history
// with the iteration that produced each step (actors' answers and actions, every synthesizer call), rewind and fork.
const POLL_MS = 600;

export async function render(root, ctx) {
  const { api, ui, wid } = ctx;
  const h = ui.h;
  let world = ctx.world;
  let steps = [];
  let latest = 0;
  let selected = null;
  let showChangedOnly = false;
  let pollTimer = null;
  let running = false;
  ctx.signal.addEventListener('abort', () => clearTimeout(pollTimer));

  const fields = () => world.schema.fields;
  const questions = () => world.schema.questions || [];
  const fieldMap = () => new Map(fields().map((f) => [f.id, f]));
  const questionMap = () => new Map(questions().map((q) => [q.id, q]));
  const W = `worlds/${api.enc(wid)}`;

  const headCard = h('div', { class: 'card' });
  const historyCard = h('div', { class: 'card' });
  const stepBox = h('div');
  root.append(headCard, historyCard, stepBox);

  // ---------------------------------------------------------------- head: run button, estimate, progress, failure
  const runBtn = h('button', { class: 'btn primary big', type: 'button', onclick: () => startRun('run') }, 'Run one iteration');
  const callsLine = h('div', { class: 'cost-line calls-line' });
  const costLine = h('div', { class: 'cost-line' }, 'Estimating cost…');
  const lastCost = h('div', { class: 'cost-line' });
  const banners = h('div');
  const progressBox = h('div');

  function drawHead() {
    ui.clear(headCard);
    const s = ctx.settings || {};
    const enabled = world.actors.filter((a) => a.enabled).length;
    const mock = s.effective_backend === 'mock';
    const top = steps.length ? steps[steps.length - 1] : null;
    const c = (ctx.meta && ctx.meta.calls_per_iteration) || null;
    callsLine.textContent = c
      ? `${c.total} calls per iteration: ${c.actors} actors + ${c.synthesizer} synthesizer steps (1 digest, ${c.synth_values} indicator, `
        + `${c.synth_statuses} question, 1 narrative; at most ${world.synthesizer.max_items_per_call} items each)`
      : '';
    headCard.append(
      h('div', { class: 'sim-head' },
        h('div', { class: 'stack' },
          h('h2', { class: 'sim-title' }, world.name),
          h('div', { class: 'sim-meta' },
            h('span', { class: 'badge accent' }, `Step ${latest}`), h('span', { class: 'badge' }, top ? top.period : '—'),
            h('span', null, `${enabled}/${world.actors.length} actors enabled · ${fields().length} indicators · `
              + `${questions().length} state questions · ${world.language}`),
            h('span', { class: `badge ${mock ? 'warn' : 'ok'}` }, mock ? 'Mock (no key)' : 'OpenRouter')),
          top && top.summary ? h('div', { class: 'small muted' }, top.summary) : null),
        h('div', { class: 'run-box' }, runBtn, callsLine, costLine, lastCost)),
      world.description ? h('details', { class: 'world-desc' }, h('summary', null, 'World description'),
        h('p', { class: 'pre' }, world.description)) : null,
      banners, progressBox);
    runBtn.disabled = running || !enabled;
    drawBanners();
  }

  function drawBanners() {
    ui.clear(banners);
    const s = ctx.settings || {};
    if (s.effective_backend === 'openrouter' && !s.has_key) {
      banners.append(h('div', { class: 'banner warn' }, 'No OpenRouter key is set. Add one in ',
        h('a', { href: '#settings' }, 'Settings'), ', or choose "Mock (no key)" there to try the app for free.'));
    }
    if (!world.actors.some((a) => a.enabled)) {
      banners.append(h('div', { class: 'banner warn' }, 'No actor is enabled. ', h('a', { href: '#actors' }, 'Enable one'), '.'));
    }
    const p = ctx.meta && ctx.meta.pending;
    if (p && !running) {
      banners.append(h('div', { class: 'banner bad', role: 'alert' },
        h('strong', null, 'The synthesizer failed'), ` (${ui.when(p.failed_at)}): ${p.error || 'unknown error'}. `,
        'The state is unchanged; the actors\' outputs are kept, so a retry only calls the synthesizer again.',
        h('div', { class: 'row', role: 'group' },
          h('button', { class: 'btn primary', type: 'button', onclick: () => startRun('retry') }, 'Retry synthesizer'),
          h('button', { class: 'btn', type: 'button', onclick: discardPending }, 'Discard'),
          h('button', { class: 'btn ghost', type: 'button', onclick: () => showCalls(`${W}/pending/calls`, 'Calls of the failed attempt') },
            'Show prompts & responses'))));
    }
  }

  async function discardPending() {
    if (!(await ui.confirmBox({ title: 'Discard the failed attempt?', body: 'The actors\' outputs of the failed attempt are dropped. The next run calls every actor again.', ok: 'Discard', danger: true }))) return;
    try { await api.post(`${W}/discard`, {}); await ctx.reloadWorld(); drawHead(); } catch (e) { ui.toast(e.message, 'bad'); }
  }

  async function loadEstimate() {
    try {
      const e = await api.get(`${W}/estimate`);
      if (!ctx.alive()) return;
      const models = [...new Set(e.rows.map((r) => r.model))];
      const price = e.priced ? ui.money(e.cost) : `${ui.money(e.cost)} + unpriced models`;
      const what = `${e.calls} calls · ~${ui.tokens(e.prompt_tokens)} in / ~${ui.tokens(e.completion_tokens)} out tokens · ${models.join(', ')}`;
      costLine.textContent = e.backend === 'mock'
        ? `Mock backend: free. With OpenRouter ≈ ${price} (${what})`
        : `Estimated next iteration ≈ ${price} (${what})`;
      costLine.title = e.assumptions;
    } catch (err) { costLine.textContent = `Estimate unavailable: ${err.message}`; }
  }

  function drawLastCost() {
    const top = steps.length ? steps[steps.length - 1] : null;
    lastCost.textContent = top && top.step > 0
      ? `Last iteration: ${ui.money(top.cost)}${top.cost_complete === false ? ' (some calls unpriced)' : ''}`
      : '';
  }

  // ---------------------------------------------------------------- live progress
  const STATUS = { queued: ['queued', ''], running: ['running', 'accent'], retrying: ['retrying', 'warn'], done: ['done', 'ok'],
    failed: ['no response', 'bad'], waiting: ['waiting', ''], repairing: ['repairing', 'warn'], partial: ['partial', 'warn'] };

  function chip(status, synth = false) {
    const [label, cls] = synth && status === 'failed' ? ['failed', 'bad'] : (STATUS[status] || [status, '']);
    return h('span', { class: `badge ${cls}` }, status === 'running' || status === 'retrying' || status === 'repairing'
      ? h('span', { class: 'spin', 'aria-hidden': 'true' }) : null, label);
  }

  function drawProgress(run) {
    ui.clear(progressBox);
    if (!run) return;
    const isLive = run.status === 'running';
    const head = isLive
      ? `${run.mode === 'retry' ? 'Retrying the synthesizer' : 'Running'}: ${run.phase === 'actors' ? 'actors deciding' : 'synthesizer writing'} ${run.period} …`
      : run.status === 'done' ? `Finished: step ${run.step} (${run.period}) saved` : `Failed: ${run.error || ''}`;
    const syn = run.synthesizer || {};
    const sSteps = syn.steps || [];
    const nDone = sSteps.filter((x) => x.status === 'done' || x.status === 'failed').length;
    progressBox.append(h('div', { class: 'progress' },
      h('div', { class: 'row spread' }, h('strong', null, head),
        h('span', { class: 'small muted' }, `cost so far ${ui.money(run.cost)}`)),
      h('h3', { class: 'small muted' }, 'Actors'),
      h('div', { class: 'progress-grid' },
        run.actors.map((a) => h('div', { class: 'prog-item', title: a.error || a.summary || '' },
          h('div', { class: 'stack' }, h('span', { class: 'nm' }, a.name),
            a.summary ? h('span', { class: 'act' }, a.summary) : a.error ? h('span', { class: 'act' }, a.error) : null),
          chip(a.status)))),
      h('h3', { class: 'small muted' }, `Synthesizer · ${nDone}/${sSteps.length} steps`, ' ', chip(syn.status, true)),
      h('div', { class: 'synth-steps' }, sSteps.map((x) => h('span', {
        class: `synth-step ${x.status}`, title: x.error || `${x.items ? x.items + ' items' : ''}` },
      x.status === 'running' || x.status === 'retrying' || x.status === 'repairing' ? h('span', { class: 'spin', 'aria-hidden': 'true' }) : null,
      x.label, x.status === 'failed' ? ' ✕' : x.status === 'done' ? ' ✓' : '')))));
  }

  async function startRun(mode) {
    if (running) return;
    try {
      const res = await api.post(`${W}/${mode === 'retry' ? 'retry' : 'run'}`, {});
      running = true;
      drawHead();
      drawProgress(res.run);
      poll();
    } catch (e) {
      ui.toast(e.message, 'bad');
    }
  }

  async function poll() {
    clearTimeout(pollTimer);
    if (!ctx.alive()) return;
    let res;
    try { res = await api.get(`${W}/run`); } catch (e) { pollTimer = setTimeout(poll, POLL_MS * 3); return; }
    if (!ctx.alive()) return;
    const run = res.run;
    drawProgress(run);
    if (run && run.status === 'running') { pollTimer = setTimeout(poll, POLL_MS); return; }
    running = false;
    await ctx.reloadWorld();
    world = ctx.world;
    await loadSteps();
    if (run && run.status === 'done') {
      ui.toast(`Step ${run.step} (${run.period}) saved${run.partial ? ' — partial: some synthesizer steps failed' : ''}`, run.partial ? 'bad' : 'ok');
      selected = latest;
    } else if (run) {
      ui.toast('The iteration failed: the state is unchanged.', 'bad');
    }
    drawHead();
    drawProgress(run);
    drawHistory();
    drawLastCost();
    loadEstimate();
    await showStep(selected);
  }

  // ---------------------------------------------------------------- history
  async function loadSteps() {
    steps = (await api.get(`${W}/steps`)).steps;
    latest = steps.length ? steps[steps.length - 1].step : 0;
    if (selected === null || selected > latest) selected = latest;
  }

  function drawHistory() {
    ui.clear(historyCard);
    historyCard.append(h('div', { class: 'card-head' }, h('h2', null, 'History'),
      h('span', { class: 'small muted' }, `${steps.length} state${steps.length === 1 ? '' : 's'} · click a step to view it`)),
    h('div', { class: 'steps', role: 'tablist', 'aria-label': 'Steps' },
      steps.map((s) => h('button', { class: `step-chip${s.step === selected ? ' active' : ''}`, type: 'button', role: 'tab',
        'aria-selected': s.step === selected ? 'true' : 'false', title: s.summary || '',
        onclick: () => { selected = s.step; drawHistory(); showStep(s.step); } },
      h('span', { class: 'n' }, String(s.step)), s.period, s.partial ? h('span', { class: 'partial-dot', title: 'partial' }, ' ⚑') : null))));
  }

  // ---------------------------------------------------------------- one step
  async function showStep(n) {
    ui.clear(stepBox).append(h('div', { class: 'loading' }, 'Loading step…'));
    let data;
    try { data = await api.get(`${W}/steps/${n}`); } catch (e) { ui.clear(stepBox).append(ui.errorBox(e)); return; }
    if (!ctx.alive() || n !== selected) return;
    ui.clear(stepBox);
    const { state, previous, iteration } = data;
    stepBox.append(stepHeader(state), narrativeCard(state), valuesCard(state, previous), questionsCard(state, previous));
    if (iteration) stepBox.append(iterationCard(iteration, n));
  }

  function stepHeader(state) {
    const isLatest = state.step === latest;
    const tools = h('div', { class: 'row' });
    if (!isLatest) {
      tools.append(h('button', { class: 'btn danger', type: 'button', onclick: () => rewind(state.step) }, 'Rewind to this step'));
    }
    tools.append(h('button', { class: 'btn', type: 'button', onclick: () => fork(state.step) }, 'Fork from this step'));
    return h('div', { class: 'card' },
      h('div', { class: 'card-head' },
        h('div', { class: 'row' }, h('h2', null, `Step ${state.step} · ${state.period}`),
          h('span', { class: `badge ${isLatest ? 'ok' : 'warn'}` }, isLatest ? 'current state' : `past step (latest is ${latest})`),
          state.partial ? h('span', { class: 'badge bad', title: 'some synthesizer steps failed: their items were carried forward' }, 'partial') : null),
        tools),
      state.summary ? h('div', { class: 'summary-line' }, state.summary) : null,
      h('div', { class: 'small muted' },
        state.step === 0 ? 'Initial state (edit it on the Schema page while the world has no later steps).'
          : `Saved ${ui.when(state.created)} · cost ${ui.money(state.cost)} · ${state.usage ? `${state.usage.calls} calls, ${ui.tokens((state.usage.prompt_tokens || 0) + (state.usage.completion_tokens || 0))} tokens` : ''}`));
  }

  function legacyText(state) { return state.text || null; }

  function narrativeCard(state) {
    const narrative = state.narrative ?? legacyText(state)?.narrative ?? '';
    return h('div', { class: 'card narrative-card' },
      h('div', { class: 'card-head' }, h('h2', null, 'Narrative'),
        h('span', { class: 'small muted' }, `${[...narrative].length} characters · a summary of all values and statuses`)),
      h('p', { class: 'narrative' }, narrative || '(none)'));
  }

  function questionsCard(state, previous) {
    const card = h('div', { class: 'card' });
    const qs = questions();
    const sts = state.statuses || {};
    const prev = previous ? (previous.statuses || {}) : null;
    const reasons = state.status_reasons || {};
    const changes = new Map((state.status_changes || []).map((c) => [c.question, c]));
    const flagsBy = new Map();
    for (const f of state.flags || []) {
      if (!f.question) continue;
      if (!flagsBy.has(f.question)) flagsBy.set(f.question, []);
      flagsBy.get(f.question).push(f);
    }
    const isChanged = (q) => changes.has(q.id) || (prev && !state.status_changes && (prev[q.id] || '') !== (sts[q.id] || ''));
    const nChanged = prev ? qs.filter(isChanged).length : 0;
    card.append(h('div', { class: 'card-head' }, h('h2', null, `State questions (${qs.length})`),
      h('span', { class: 'small muted' }, prev ? `${nChanged} of ${qs.length} statuses changed this step` : 'status at the start')));
    if (!qs.length) {
      const legacy = legacyText(state);
      card.append(h('div', { class: 'empty' }, legacy ? 'This step was saved in the old format.' : 'No state questions: add them on the Schema page.'));
      return card;
    }
    let group = null;
    const list = h('div', { class: 'sq-list' });
    for (const q of qs) {
      if (q.group !== group) { group = q.group; list.append(h('h3', { class: 'small muted' }, group)); }
      const ch = prev ? isChanged(q) : false;
      const c = changes.get(q.id);
      const was = c ? c.from : (prev ? prev[q.id] : null);
      const fl = flagsBy.get(q.id) || [];
      list.append(h('div', { class: `sq-item${ch ? ' changed' : ''}` },
        h('div', { class: 'row' }, h('span', { class: 'q' }, q.text),
          ch ? h('span', { class: 'badge accent' }, 'changed') : (prev ? h('span', { class: 'badge' }, 'unchanged') : null)),
        h('div', { class: 'st' }, sts[q.id] || h('span', { class: 'muted' }, '(no status yet)')),
        ch && reasons[q.id] ? h('div', { class: 'reason' }, `Why: ${reasons[q.id]}`) : null,
        ch && was ? h('details', { class: 'was-box' }, h('summary', null, 'Previous status'), h('div', { class: 'was-st' }, was)) : null,
        fl.map((x) => h('span', { class: `flag ${x.level}` }, `⚑ ${x.message}`))));
    }
    card.append(list);
    return card;
  }

  function valuesCard(state, previous) {
    const card = h('div', { class: 'card' });
    const vals = state.values || {};
    const prev = previous ? previous.values || {} : null;
    const reasons = state.reasons || {};
    const flagsBy = new Map();
    for (const f of state.flags || []) {
      if (!f.field) continue;
      if (!flagsBy.has(f.field)) flagsBy.set(f.field, []);
      flagsBy.get(f.field).push(f);
    }
    const nChanged = prev ? fields().filter((f) => ui.deltaOf(f, prev[f.id], vals[f.id])).length : 0;
    const toggle = h('label', { class: 'check small' },
      h('input', { type: 'checkbox', checked: showChangedOnly, disabled: !prev,
        onchange: (e) => { showChangedOnly = e.target.checked; draw(); } }), 'Changed only');
    const body = h('div');
    card.append(h('div', { class: 'card-head' }, h('h2', null, 'State values'),
      h('div', { class: 'row small muted' }, prev ? `${nChanged} of ${fields().length} fields changed` : `${fields().length} fields`, toggle,
        h('button', { class: 'btn tiny', type: 'button', onclick: () => body.querySelectorAll('details').forEach((d) => { d.open = true; }) }, 'Expand all'),
        h('button', { class: 'btn tiny', type: 'button', onclick: () => body.querySelectorAll('details').forEach((d) => { d.open = false; }) }, 'Collapse all'))),
    body);

    function draw() {
      ui.clear(body);
      const groups = [];
      const byGroup = new Map();
      for (const f of fields()) {
        if (!byGroup.has(f.group)) { byGroup.set(f.group, []); groups.push(f.group); }
        byGroup.get(f.group).push(f);
      }
      for (const g of groups) {
        const rows = [];
        let changedHere = 0;
        for (const f of byGroup.get(g)) {
          const d = prev ? ui.deltaOf(f, prev[f.id], vals[f.id]) : null;
          if (d) changedHere += 1;
          if (showChangedOnly && !d) continue;
          const fl = flagsBy.get(f.id) || [];
          rows.push(h('tr', { class: d ? 'changed' : '' },
            h('td', { class: 'c-field' }, h('div', { class: 'fl-label' }, f.label, ' ',
              f.tag ? h('span', { class: 'badge tag', title: f.tag }, f.tag) : null),
            h('div', { class: 'fl-id', title: f.description || '' }, f.id)),
            h('td', { class: 'c-val' }, h('span', { class: 'val' }, ui.valueText(f, vals[f.id])),
              ui.NUMERIC.has(f.type) ? h('span', { class: 'unit' }, ui.unitText(f)) : null,
              d && d.cls === 'txt' && prev ? h('span', { class: 'was-val' }, `was: ${ui.valueText(f, prev[f.id])}`) : null),
            h('td', { class: 'c-delta' }, d ? h('span', { class: `delta ${d.cls}` }, d.text) : h('span', { class: 'muted small' }, prev ? '·' : '')),
            h('td', { class: 'c-reason' }, h('span', { class: 'reason' }, reasons[f.id] || ''),
              fl.map((x) => h('span', { class: `flag ${x.level}` }, `⚑ ${x.message}`)))));
        }
        if (!rows.length) continue;
        const det = h('details', { class: 'group', open: true },
          h('summary', null, g, h('span', { class: 'badge' }, String(byGroup.get(g).length)),
            changedHere ? h('span', { class: 'badge accent' }, `${changedHere} changed`) : null),
          h('div', { class: 'group-body' }, h('table', { class: 'vt' },
            h('colgroup', null, h('col', { class: 'c-field' }), h('col', { class: 'c-val' }), h('col', { class: 'c-delta' }), h('col')),
            h('thead', null, h('tr', null, h('th', null, 'Field'), h('th', null, 'Value'), h('th', null, 'Change'), h('th', null, 'Reason'))),
            h('tbody', null, rows))));
        body.append(det);
      }
      if (!body.children.length) body.append(h('div', { class: 'empty' }, 'No field changed in this step.'));
      const general = (state.flags || []).filter((f) => !f.field && !f.question);
      if (general.length) {
        body.append(h('div', { class: 'banner warn' }, h('strong', null, 'Validation notes'),
          h('ul', null, general.map((f) => h('li', null, `${f.level}: ${f.message}`)))));
      }
    }
    draw();
    return card;
  }

  function answerList(o) {
    return h('div', { class: 'answers' }, (o.answers || []).map((a) => {
      const bad = a.status === 'unanswered';
      return h('div', { class: `answer${bad ? ' bad' : ''}` },
        h('div', { class: 'aq' }, a.question || a.id, bad ? h('span', { class: 'badge bad' }, 'unanswered') : null,
          a.status === 'skipped' ? h('span', { class: 'badge' }, 'optional · skipped') : null),
        bad ? h('div', { class: 'av muted' }, 'Required, still no answer after one repair: the synthesizer was told not to invent one.')
          : a.value ? h('div', { class: 'av' }, a.value) : null,
        a.rationale ? h('div', { class: 'ar' }, a.rationale) : null);
    }));
  }

  function actionCards(o) {
    const acts = o.actions || [];
    if (!acts.length) return h('div', { class: 'small muted' }, 'No autonomous actions.');
    return h('div', { class: 'auto-acts' }, acts.map((x) => h('div', { class: 'auto-act' },
      h('div', { class: 'row' }, h('strong', null, x.title), x.target ? h('span', { class: 'badge accent', title: 'target' }, `→ ${x.target}`) : null),
      x.detail ? h('div', null, x.detail) : null,
      x.intended_effect ? h('div', { class: 'small muted' }, `Intended effect: ${x.intended_effect}`) : null)));
  }

  function actorCard(r) {
    const o = r.output;
    if (r.status !== 'ok' || !o) {
      return h('div', { class: 'actor-out' }, h('h4', null, r.name, h('span', { class: 'badge bad' }, 'no response')),
        h('div', { class: 'small muted' }, r.error || ''), h('div', { class: 'small muted' }, `${r.attempts} attempt(s) · ${r.model || ''}`));
    }
    if (!o.answers && o.action_id) {                // an iteration saved before decision questions existed
      return h('div', { class: 'actor-out' },
        h('h4', null, r.name, h('span', { class: `badge ${o.action_id === 'free' ? 'warn' : 'accent'}` }, o.action_label)),
        h('dl', null,
          o.action_detail ? [h('dt', null, 'Action'), h('dd', null, o.action_detail)] : null,
          o.rationale ? [h('dt', null, 'Rationale'), h('dd', null, o.rationale)] : null,
          o.public_statement ? [h('dt', null, 'Statement'), h('dd', null, `“${o.public_statement}”`)] : null));
    }
    const unanswered = (o.answers || []).filter((a) => a.status === 'unanswered').length;
    return h('div', { class: 'actor-out' },
      h('h4', null, r.name, h('span', { class: 'badge' }, `${(o.answers || []).length} answers`),
        h('span', { class: 'badge' }, `${(o.actions || []).length} actions`),
        unanswered ? h('span', { class: 'badge bad' }, `${unanswered} unanswered`) : null),
      answerList(o),
      h('div', { class: 'sub-h' }, 'Autonomous actions'), actionCards(o),
      h('dl', null,
        o.public_statement ? [h('dt', null, 'Statement'), h('dd', null, `“${o.public_statement}”`)] : null,
        o.rationale ? [h('dt', null, 'Rationale'), h('dd', null, o.rationale)] : null,
        (o.expected_consequences || []).length ? [h('dt', null, 'Expects'), h('dd', null, o.expected_consequences.join(' · '))] : null),
      (r.notes || []).length ? h('div', { class: 'flag warn' }, r.notes.join('; ')) : null,
      h('div', { class: 'small muted' }, `${r.attempts > 1 ? `${r.attempts} attempts · ` : ''}${r.model || ''}`));
  }

  function synthSteps(it, n) {
    const syn = it.synthesizer || {};
    const stepsList = syn.steps || [];
    if (!stepsList.length) return null;
    let callsCache = null;
    const loadCalls = async () => {
      if (!callsCache) callsCache = (await api.get(`${W}/iterations/${n}/calls`)).calls || [];
      return callsCache;
    };
    const fmap = fieldMap();
    const qmap = questionMap();
    const box = h('div', { class: 'synth-list' });
    for (const st of stepsList) {
      const body = h('div', { class: 'synth-body' });
      const out = st.output;
      if (st.status === 'failed') body.append(h('div', { class: 'flag error' }, `Failed: ${st.error || ''} — its items were carried forward.`));
      if (out && st.kind === 'digest') {
        body.append(h('p', { class: 'pre' }, out.digest || ''),
          (out.threads || []).length ? h('ul', { class: 'threads' }, out.threads.map((t) => h('li', null, t))) : null);
      } else if (out && st.kind === 'values') {
        body.append(h('ul', { class: 'changes' }, Object.entries(out.values || {}).map(([fid, e]) => {
          const f = fmap.get(fid) || { label: fid, type: 'text' };
          const v = e && typeof e === 'object' && 'value' in e ? e.value : e;
          return h('li', null, h('strong', null, f.label), ': ', ui.valueText(f, v), ui.NUMERIC.has(f.type) ? ` ${ui.unitText(f)}` : '',
            e && e.reason ? h('div', { class: 'reason' }, e.reason) : null);
        })));
      } else if (out && st.kind === 'statuses') {
        body.append(h('ul', { class: 'changes' }, Object.entries(out.statuses || {}).map(([qid, e]) => {
          const q = qmap.get(qid) || { text: qid };
          const o = typeof e === 'object' && e ? e : { status: e };
          return h('li', null, h('strong', null, q.text), o.changed ? h('span', { class: 'badge accent' }, 'changed') : h('span', { class: 'badge' }, 'unchanged'),
            h('div', null, o.status || ''), o.reason ? h('div', { class: 'reason' }, o.reason) : null);
        })));
      } else if (out && st.kind === 'narrative') {
        body.append(h('p', null, out.narrative || ''));
      }
      if ((st.dropped || []).length) body.append(h('div', { class: 'flag warn' }, `Dropped keys outside this step: ${st.dropped.join(', ')}`));
      const io = h('div');
      body.append(h('button', { class: 'btn tiny', type: 'button', onclick: async (e) => {
        e.target.disabled = true;
        try {
          const calls = (await loadCalls()).filter((c) => c.who === `synthesizer:${st.key}`);
          ui.clear(io).append(...(calls.length ? calls.map((c) => h('div', { class: 'calls' },
            h('div', { class: 'small muted' }, `attempt ${c.attempt}${c.repair ? ' · repair' : ''} · ${c.served_model || c.model} · `
              + `${ui.tokens((c.usage || {}).prompt_tokens)}/${ui.tokens((c.usage || {}).completion_tokens)} tok`),
            c.error ? h('pre', null, `Error: ${c.error}`) : null,
            h('details', null, h('summary', null, 'Input (prompt)'),
              h('pre', null, (c.messages || []).map((m) => [h('span', { class: 'msg-role' }, `[${m.role}]`), '\n', m.content, '\n\n']))),
            h('details', null, h('summary', null, 'Output (raw response)'), h('pre', null, c.response_text ?? '(no response)'))))
            : [h('div', { class: 'small muted' }, 'No call recorded for this step.')]));
        } catch (err) { ui.toast(err.message, 'bad'); e.target.disabled = false; }
      } }, 'Show input & raw output'), io);
      const items = st.items || [];
      box.append(h('details', { class: `synth-item ${st.status}` },
        h('summary', null, h('strong', null, st.label), h('span', { class: `badge ${st.status === 'ok' ? 'ok' : 'bad'}` }, st.status),
          items.length ? h('span', { class: 'small muted' }, `${items.length} item${items.length === 1 ? '' : 's'}${(st.groups || []).length ? ' · ' + st.groups.join(' · ') : ''}`) : null,
          st.attempts > 1 ? h('span', { class: 'badge warn' }, `${st.attempts} attempts`) : null),
        body));
    }
    return box;
  }

  function iterationCard(it, n) {
    const fmap = fieldMap();
    const card = h('div', { class: 'card' });
    const actorCards = (it.actors || []).map(actorCard);
    const changes = (it.changes || []).map((c) => {
      const f = fmap.get(c.field) || { type: typeof c.to === 'number' ? 'number' : 'text', unit: '' };
      return h('li', null, h('strong', null, c.label || c.field), ': ', ui.valueText(f, c.from), ' → ', h('strong', null, ui.valueText(f, c.to)),
        ui.NUMERIC.has(f.type) ? ` ${ui.unitText(f)}` : '', c.reason ? h('div', { class: 'reason' }, c.reason) : null);
    });
    const sChanges = (it.status_changes || []).map((c) => h('li', null, h('strong', null, c.text || c.question),
      h('div', { class: 'st-from' }, c.from || '(none)'), h('div', { class: 'st-to' }, '→ ', c.to), c.reason ? h('div', { class: 'reason' }, c.reason) : null));
    const tc = it.text_changes || {};
    const textBits = [];
    if ((tc.new_questions || []).length) textBits.push(h('li', null, h('strong', null, 'New questions: '), tc.new_questions.join(' · ')));
    if ((tc.answered_questions || []).length) textBits.push(h('li', null, h('strong', null, 'Answered: '), tc.answered_questions.join(' · ')));
    const u = it.usage || {};
    const syn = it.synthesizer || {};
    const nSyn = (syn.steps || []).length;
    ui.add(card,
      h('div', { class: 'card-head' }, h('h2', null, `Iteration ${it.period_from} → ${it.period}`),
        h('div', { class: 'row small muted' },
          `${u.calls || 0} calls · ${ui.tokens(u.prompt_tokens)} in / ${ui.tokens(u.completion_tokens)} out tokens`
          + (u.reasoning_tokens ? ` (${ui.tokens(u.reasoning_tokens)} reasoning)` : '')
          + ` · ${ui.money(it.cost)}${it.cost_complete === false ? ' (some calls unpriced)' : ''} · ${it.backend}`,
          h('button', { class: 'btn tiny', type: 'button', onclick: () => showCalls(`${W}/iterations/${n}/calls`, `Calls of step ${n}`) },
            'Show all prompts & raw responses'))),
      it.partial ? h('div', { class: 'banner warn' }, 'Partial iteration: some synthesizer steps failed after a retry, so their items '
        + 'were carried forward from the previous step (「合成失败，沿用上期」) and flagged.') : null,
      h('h3', { class: 'small muted' }, `Actors (${(it.actors || []).length})`),
      h('div', { class: 'actor-grid' }, actorCards),
      h('hr'),
      nSyn ? [h('h3', { class: 'small muted' }, `Synthesizer: ${nSyn} steps${syn.model ? ' · ' + syn.model : ''}`
        + `${syn.max_items_per_call ? ` · at most ${syn.max_items_per_call} items per call` : ''} · expand a step to see its input and output`),
      synthSteps(it, n), h('hr')] : null,
      h('h3', { class: 'small muted' }, `Indicator changes (${changes.length})`),
      changes.length ? h('ul', { class: 'changes' }, changes) : h('div', { class: 'empty' }, 'No value changed.'),
      h('h3', { class: 'small muted' }, `Status changes (${sChanges.length})`),
      sChanges.length ? h('ul', { class: 'changes' }, sChanges) : h('div', { class: 'empty' }, 'No status changed.'),
      textBits.length ? [h('h3', { class: 'small muted' }, 'Text changes'), h('ul', { class: 'changes' }, textBits)] : null);
    return card;
  }

  async function showCalls(path, title) {
    let calls;
    try { calls = (await api.get(path)).calls; } catch (e) { ui.toast(e.message, 'bad'); return; }
    const body = h('div', { class: 'calls' }, calls.length ? null : h('div', { class: 'empty' }, 'No calls recorded.'),
      calls.map((c) => {
        const u = c.usage || {};
        return h('details', null,
          h('summary', null, h('strong', null, c.who), h('span', { class: 'badge' }, c.purpose),
            c.repair ? h('span', { class: 'badge warn' }, 'repair') : null, c.attempt > 1 ? h('span', { class: 'badge warn' }, `attempt ${c.attempt}`) : null,
            c.error ? h('span', { class: 'badge bad' }, 'error') : h('span', { class: 'badge ok' }, 'ok'),
            h('span', { class: 'mono' }, c.served_model || c.model),
            h('span', { class: 'muted' }, `${c.response_format || ''} · ${ui.tokens(u.prompt_tokens)}/${ui.tokens(u.completion_tokens)} tok · ${ui.money(c.cost)} · ${c.duration_s ?? '?'} s`)),
          c.error ? h('pre', null, `Error: ${c.error}`) : null,
          h('pre', null, (c.messages || []).map((m) => [h('span', { class: 'msg-role' }, `[${m.role}]`), '\n', m.content, '\n\n'])),
          h('pre', null, h('span', { class: 'msg-role' }, '[response]'), '\n', c.response_text ?? '(no response)'));
      }));
    ui.infoBox({ title, body, wide: true });
  }

  // ---------------------------------------------------------------- rewind and fork
  async function rewind(n) {
    const drop = latest - n;
    const ok = await ui.confirmBox({ title: `Rewind to step ${n}?`, danger: true, ok: `Drop ${drop} step${drop === 1 ? '' : 's'}`,
      body: `This permanently deletes steps ${n + 1}–${latest} and the iterations that produced them. Fork from step ${latest} first if you want to keep them.` });
    if (!ok) return;
    try {
      await api.post(`${W}/rewind`, { step: n, confirm: true });
      ui.toast(`Rewound to step ${n}`, 'ok');
      await ctx.reloadWorld(); world = ctx.world;
      await loadSteps(); selected = latest;
      drawHead(); drawHistory(); drawLastCost(); loadEstimate(); showStep(selected);
    } catch (e) { ui.toast(e.message, 'bad'); }
  }

  async function fork(n) {
    const name = await ui.promptBox({ title: `Fork a new world from step ${n}`, label: 'Name of the new world',
      value: `${world.name} (fork @ step ${n})`, ok: 'Fork' });
    if (name === null) return;
    try {
      const res = await api.post(`${W}/fork`, { step: n, name });
      ui.toast(`Created “${res.world.name}”`, 'ok');
      await ctx.setWorld(res.world.id);
    } catch (e) { ui.toast(e.message, 'bad'); }
  }

  // ---------------------------------------------------------------- start
  await loadSteps();
  drawHead();
  drawHistory();
  drawLastCost();
  loadEstimate();
  const status = await api.get(`${W}/run`).catch(() => null);
  if (status && status.run && status.run.status === 'running') { running = true; drawHead(); drawProgress(status.run); poll(); }
  await showStep(selected);
}
