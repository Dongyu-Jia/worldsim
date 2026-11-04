// Audit: every model call of an iteration, participant by participant (each actor, then each synthesizer step), and
// attempt by attempt (first call, repair, retry): the exact input sent, the model's thinking as the provider returned
// it, and the raw and parsed output with the app's verdict. Deep link: #audit?step=3&who=fed (step=pending for the
// failed attempt awaiting retry). Everything is rendered as text.
export async function render(root, ctx) {
  const { api, ui, wid } = ctx;
  const h = ui.h;
  const W = `worlds/${api.enc(wid)}`;
  const query = new URLSearchParams(location.hash.split('?')[1] || '');

  let steps;
  try { steps = (await api.get(`${W}/steps`)).steps.filter((s) => s.step > 0); } catch (e) { root.append(ui.errorBox(e)); return; }
  if (!ctx.alive()) return;
  const pending = ctx.meta && ctx.meta.pending;
  const choices = [...(pending ? ['pending'] : []), ...steps.map((s) => s.step).reverse()];
  if (!choices.length) {
    root.append(h('div', { class: 'card' }, h('h2', null, 'Audit'),
      h('p', { class: 'muted' }, 'No iteration has run yet. Run one on the ', h('a', { href: '#sim' }, 'Simulation'), ' page; every call it makes is recorded here.')));
    return;
  }
  const wantStep = query.get('step');
  let step = wantStep === 'pending' && pending ? 'pending' : (choices.includes(Number(wantStep)) ? Number(wantStep) : choices[0]);
  let who = query.get('who');
  let participants = [];

  const head = h('div', { class: 'card' });
  const layout = h('div', { class: 'audit-layout' });
  const list = h('nav', { class: 'card audit-list', 'aria-label': 'Participants' });
  const detail = h('div', { class: 'audit-detail' });
  layout.append(list, detail);
  root.append(head, layout);

  const base = () => (step === 'pending' ? `${W}/pending/audit` : `${W}/iterations/${step}/audit`);
  const stepName = (s) => {
    if (s === 'pending') return `Failed attempt awaiting retry (${pending.period || ''})`;
    const st = steps.find((x) => x.step === s);
    return `Step ${s} · ${st ? st.period : ''}`;
  };
  function remember() {
    const q = new URLSearchParams({ step: String(step) });
    if (who) q.set('who', who);
    history.replaceState(null, '', `#audit?${q}`);
  }

  // ---------------------------------------------------------------- head: step picker and previous / next
  function drawHead() {
    const sel = h('select', { 'aria-label': 'Step', onchange: (e) => go(e.target.value === 'pending' ? 'pending' : Number(e.target.value)) },
      choices.map((c) => h('option', { value: String(c), selected: c === step }, stepName(c))));
    const numeric = choices.filter((c) => c !== 'pending');
    const i = numeric.indexOf(step);
    const older = i >= 0 && i < numeric.length - 1 ? numeric[i + 1] : null;
    const newer = i > 0 ? numeric[i - 1] : null;
    ui.clear(head).append(
      h('div', { class: 'card-head' }, h('h2', null, 'Model calls of a step'),
        h('div', { class: 'row audit-nav' },
          h('button', { class: 'btn tiny', type: 'button', disabled: older === null, title: 'The same participant one step earlier',
            onclick: () => go(older) }, '← Older'),
          sel,
          h('button', { class: 'btn tiny', type: 'button', disabled: newer === null, title: 'The same participant one step later',
            onclick: () => go(newer) }, 'Newer →'))),
      h('p', { class: 'small muted' }, 'Every model call of the step: what was sent (the exact messages and request), the model\'s thinking as '
        + 'the provider returned it, and what came back with the app\'s verdict. Thinking is only what the provider returns: some models '
        + 'return a summary of their reasoning (Gemini returns thought summaries), some return none, and encrypted reasoning cannot be read.'));
  }

  async function go(s) {
    step = s;
    drawHead();
    await loadList();
  }

  // ---------------------------------------------------------------- the participants
  async function loadList() {
    ui.clear(list).append(h('div', { class: 'loading' }, 'Loading…'));
    ui.clear(detail);
    let data;
    try { data = await api.get(base()); } catch (e) { ui.clear(list).append(ui.errorBox(e)); return; }
    if (!ctx.alive()) return;
    participants = data.participants || [];
    if (!participants.some((p) => p.who === who)) {
      if (who) ui.toast(`${labelOf(who)} has no calls in ${stepName(step)}: showing the first participant`);
      who = participants.length ? participants[0].who : null;
    }
    drawList();
    remember();
    if (who) await loadDetail();
  }

  const labelOf = (w) => (participants.find((p) => p.who === w) || {}).label || w;

  function drawList() {
    ui.clear(list);
    for (const [kind, name] of [['actor', 'Actors'], ['synth', 'Synthesizer']]) {
      const ps = participants.filter((p) => p.kind === kind);
      if (!ps.length) continue;
      list.append(h('div', { class: 'sub-h' }, `${name} (${ps.length})`),
        h('ul', { class: 'audit-who' }, ps.map((p) => h('li', null,
          h('button', { type: 'button', class: `audit-who-btn${p.who === who ? ' active' : ''}`, 'aria-current': p.who === who ? 'true' : null,
            onclick: () => { who = p.who; drawList(); remember(); loadDetail(); } },
          h('span', { class: 'audit-who-name' }, p.label),
          h('span', { class: 'audit-who-meta' },
            h('span', { class: `badge ${p.status === 'ok' ? 'ok' : (p.status === 'not run' ? '' : 'bad')}` }, p.status),
            p.attempts > 1 ? h('span', { class: 'badge warn' }, `${p.attempts} calls`) : null,
            p.has_thinking ? h('span', { class: 'badge accent', title: 'thinking recorded' }, 'thinking') : null,
            p.legacy ? h('span', { class: 'badge', title: 'recorded before the audit view' }, 'old record') : null))))));
    }
  }

  // ---------------------------------------------------------------- one participant
  async function loadDetail() {
    ui.clear(detail).append(h('div', { class: 'loading' }, 'Loading…'));
    const want = who;
    let d;
    try { d = await api.get(`${base()}?who=${api.enc(who)}`); } catch (e) { ui.clear(detail).append(ui.errorBox(e)); return; }
    if (!ctx.alive() || want !== who) return;
    const p = d.participant || {};
    const t = p.tokens || {};
    ui.clear(detail).append(
      h('div', { class: 'card' },
        h('div', { class: 'card-head' }, h('h2', null, p.label || who),
          h('div', { class: 'row small muted' },
            `${stepName(step)} · ${d.attempts.length} call${d.attempts.length === 1 ? '' : 's'} · `
            + `${ui.tokens(t.prompt)} in / ${ui.tokens(t.completion)} out` + (t.reasoning ? ` (${ui.tokens(t.reasoning)} reasoning)` : '')
            + ` · ${ui.money(p.cost)}${p.duration_s != null ? ` · ${ui.num(p.duration_s, 1)} s` : ''}`)),
        finalBlock(p, d.final)),
      ...(d.attempts.length ? d.attempts.map((a) => attemptCard(a, d.attempts.length))
        : [h('div', { class: 'card muted' }, 'No call was recorded for this participant.')]));
  }

  function finalBlock(p, f) {
    const box = h('details', { class: 'audit-final', open: true },
      h('summary', null, h('strong', null, p.kind === 'actor' ? 'Final decision (as used by the synthesizer)' : 'Final output (as merged into the state)'),
        f.status ? h('span', { class: `badge ${f.status === 'ok' ? 'ok' : 'bad'}` }, f.status) : null));
    if (f.error) box.append(h('div', { class: 'flag bad' }, f.error));
    const o = f.output;
    if (!o) { box.append(h('div', { class: 'small muted' }, 'No accepted output.')); return box; }
    if (p.kind === 'actor' && (o.answers || o.actions)) {
      box.append(h('ol', { class: 'answers' }, (o.answers || []).map((a) => h('li', null,
        h('div', { class: 'q' }, a.question || a.id, a.status === 'unanswered' ? h('span', { class: 'badge bad' }, 'unanswered') : null),
        h('div', null, a.value || ''), a.rationale ? h('div', { class: 'reason' }, a.rationale) : null))));
      if ((o.actions || []).length) {
        box.append(h('div', { class: 'sub-h' }, 'Autonomous actions'), h('ul', null, o.actions.map((x) => h('li', null,
          h('strong', null, x.title || ''), x.target ? ` → ${x.target}` : '', x.detail ? h('div', null, x.detail) : null,
          x.intended_effect ? h('div', { class: 'reason' }, `Intended effect: ${x.intended_effect}`) : null))));
      }
      box.append(h('dl', null,
        o.public_statement ? [h('dt', null, 'Statement'), h('dd', null, `“${o.public_statement}”`)] : null,
        o.rationale ? [h('dt', null, 'Rationale'), h('dd', null, o.rationale)] : null,
        (o.expected_consequences || []).length ? [h('dt', null, 'Expects'), h('dd', null, o.expected_consequences.join(' · '))] : null));
    } else {
      box.append(pre(JSON.stringify(o, null, 2)));
    }
    if ((f.notes || []).length) box.append(h('div', { class: 'flag warn' }, f.notes.join('; ')));
    if ((f.dropped || []).length) box.append(h('div', { class: 'flag warn' }, `Dropped keys outside this step: ${f.dropped.join(', ')}`));
    return box;
  }

  // ---------------------------------------------------------------- one attempt: input, thinking, output
  function attemptCard(a, total) {
    const o = a.output;
    const title = [`Call ${a.n} of ${total}`, a.round > 1 ? `retry round ${a.round}` : null, `attempt ${a.attempt}`, a.repair ? 'repair' : null]
      .filter(Boolean).join(' · ');
    const verdict = a.meta.error && !o.text ? h('span', { class: 'badge bad' }, 'call failed')
      : o.accepted ? h('span', { class: 'badge ok' }, 'accepted') : h('span', { class: 'badge bad' }, 'not accepted');
    return h('div', { class: 'card audit-attempt' },
      h('div', { class: 'card-head' }, h('h2', null, title, ' ', verdict), h('span', { class: 'small muted' }, ui.when(a.started))),
      a.legacy ? h('div', { class: 'banner' }, 'Recorded before the audit view existed: only the messages and the response text were saved.') : null,
      section('1 · Input', 'what was sent', inputBody(a)),
      section('2 · Thinking', 'as the provider returned it', thinkingBody(a)),
      section('3 · Output', 'what came back, and the app\'s verdict', outputBody(a)),
      metaLine(a));
  }

  function section(title, sub, body) {
    return h('details', { class: 'audit-sec', open: true }, h('summary', null, h('strong', null, title), h('span', { class: 'small muted' }, sub)), body);
  }

  function inputBody(a) {
    const i = a.input;
    const box = h('div');
    const pr = i.params;
    if (pr) {
      box.append(h('div', { class: 'kv' },
        kv('Model', pr.model), kv('Temperature', pr.temperature ?? 'provider default'), kv('Max tokens', pr.max_tokens ?? 'provider default'),
        kv('Reasoning', pr.reasoning ? JSON.stringify(pr.reasoning) : 'not sent'), kv('Response format', pr.response_format || 'none'),
        pr.schema_name ? kv('Schema', pr.schema_name) : null, pr.provider ? kv('Provider routing', JSON.stringify(pr.provider)) : null));
    } else if (a.input.request_truncated) {
      box.append(h('div', { class: 'flag warn' }, 'The request was larger than the audit cap: its start is kept below.'));
    }
    (i.messages || []).forEach((m, k) => box.append(h('details', { class: 'audit-msg', open: k === i.messages.length - 1 },
      h('summary', null, h('span', { class: 'msg-role' }, `[${m.role}]`), h('span', { class: 'small muted' }, `${(m.content || '').length.toLocaleString('en-US')} characters`),
        copyBtn(() => m.content || '')),
      pre(m.content || ''))));
    if (!(i.messages || []).length) box.append(h('div', { class: 'small muted' }, 'No messages were recorded.'));
    if (i.schema) box.append(h('details', { class: 'audit-msg' }, h('summary', null, 'JSON schema of the answer', copyBtn(() => JSON.stringify(i.schema, null, 2))),
      pre(JSON.stringify(i.schema, null, 2))));
    if (i.request) box.append(h('details', { class: 'audit-msg' }, h('summary', null, 'Exact request body (the key is in a header, never in the body)',
      copyBtn(() => JSON.stringify(i.request, null, 2))), pre(JSON.stringify(i.request, null, 2))));
    if (i.request_text) box.append(h('details', { class: 'audit-msg' }, h('summary', null, 'Request body (truncated)'), pre(i.request_text)));
    return box;
  }

  function thinkingBody(a) {
    const t = a.thinking;
    const box = h('div');
    if (!t) {
      box.append(h('div', { class: 'small muted' }, a.legacy ? 'Not recorded: this call was saved before thinking capture existed.'
        : 'No thinking was recorded for this call.'));
      return box;
    }
    const notes = [];
    if (t.mock) notes.push(h('div', { class: 'flag warn' }, 'Mock backend: this text is written by WorldSim\'s mock to exercise the view. It is not a model\'s reasoning.'));
    if (t.capture === 'off') notes.push(h('div', { class: 'small muted' }, 'Thinking capture is off in Settings, so the reasoning was not asked for.'));
    if (t.capture === 'unsupported') notes.push(h('div', { class: 'small muted' }, 'This model does not take the reasoning parameter, so it returns no thinking.'));
    if (t.capture === 'unknown') notes.push(h('div', { class: 'small muted' }, 'The model is not in the model list, so no reasoning was asked for.'));
    if (t.encrypted_blocks) notes.push(h('div', { class: 'small muted' }, `${t.encrypted_blocks} encrypted reasoning block${t.encrypted_blocks === 1 ? '' : 's'} returned: encrypted reasoning cannot be read.`));
    const text = t.text || '';
    if (text.trim()) {
      box.append(h('div', { class: 'small muted' }, [
        t.source === 'reasoning_details' ? `${(t.blocks || []).length} reasoning block(s) (${[...new Set((t.blocks || []).map((b) => b.type))].join(', ')})` : 'message.reasoning',
        t.reasoning_tokens != null ? ` · ${ui.tokens(t.reasoning_tokens)} reasoning tokens` : '',
        t.text_truncated ? ' · truncated at the audit cap' : ''].join('')), copyBtn(() => text, 'Copy thinking'));
      box.append(pre(text, 'audit-thinking'));
    } else if (t.capture === 'on' || !t.capture) {
      box.append(h('div', { class: 'small muted' }, t.reasoning_tokens
        ? `The model used ${ui.tokens(t.reasoning_tokens)} reasoning tokens but returned no readable thinking.`
        : 'The model returned no readable thinking.'));
    }
    box.append(...notes);
    return box;
  }

  function outputBody(a) {
    const o = a.output;
    const box = h('div');
    if (a.meta.error && !o.text) box.append(h('div', { class: 'flag bad' }, a.meta.error));
    else if (o.accepted) box.append(h('div', { class: 'flag ok' }, 'Accepted: this answer passed validation and was used.'));
    else if (o.validation_error) box.append(h('div', { class: 'flag bad' }, `Not accepted: ${o.validation_error}`));
    if ((o.notes || []).length) box.append(h('div', { class: 'flag warn' }, o.notes.join('; ')));
    if (o.finish_reason || o.native_finish_reason) {
      box.append(h('div', { class: 'small muted' }, `Finish reason: ${o.finish_reason || '—'}${o.native_finish_reason && o.native_finish_reason !== o.finish_reason ? ` (provider: ${o.native_finish_reason})` : ''}`));
    }
    if (o.text != null) {
      box.append(h('details', { class: 'audit-msg', open: true }, h('summary', null, 'Response text (raw)', copyBtn(() => o.text)), pre(o.text)));
    }
    if (o.parsed != null) box.append(h('details', { class: 'audit-msg' }, h('summary', null, 'JSON parsed from it'), pre(JSON.stringify(o.parsed, null, 2))));
    if (o.raw != null) {
      box.append(h('details', { class: 'audit-msg' }, h('summary', null, `Raw response JSON from the provider${o.raw_truncated ? ' (truncated)' : ''}`,
        copyBtn(() => (typeof o.raw === 'string' ? o.raw : JSON.stringify(o.raw, null, 2)))),
      pre(o.raw && o.raw.truncated ? o.raw.text : JSON.stringify(o.raw, null, 2))));
    }
    const hx = a.meta.http_attempts || [];
    if (hx.length > 1 || hx.some((x) => !x.ok)) {
      box.append(h('div', { class: 'sub-h' }, 'HTTP requests'), h('ol', { class: 'small' }, hx.map((x) =>
        h('li', null, `${x.format} → ${x.status ?? 'no status'}${x.ok ? '' : ` · ${x.error || 'failed'}`}${x.duration_s != null ? ` · ${ui.num(x.duration_s, 1)} s` : ''}`))));
    }
    return box;
  }

  function metaLine(a) {
    const m = a.meta;
    const u = m.usage || {};
    const served = m.served_model && m.served_model !== m.model ? ` → served by ${m.served_model}` : '';
    return h('div', { class: 'small muted audit-meta' }, [
      `${m.model || ''}${served}`, m.provider ? `provider ${m.provider}` : null,
      `${ui.tokens(u.prompt_tokens)} prompt / ${ui.tokens(u.completion_tokens)} completion`
        + (u.reasoning_tokens ? ` / ${ui.tokens(u.reasoning_tokens)} reasoning` : '') + (u.cached_tokens ? ` / ${ui.tokens(u.cached_tokens)} cached` : '') + ' tokens',
      ui.money(m.cost), a.duration_s != null ? `${ui.num(a.duration_s, 1)} s` : null, m.generation_id ? m.generation_id : null]
      .filter(Boolean).join(' · '));
  }

  // ---------------------------------------------------------------- bits
  function kv(k, v) { return h('div', null, h('span', { class: 'k' }, k), h('span', { class: 'v mono' }, String(v))); }
  function pre(text, cls = '') { return h('pre', { class: `audit-pre ${cls}` }, text); }
  function copyBtn(get, label = 'Copy') {
    return h('button', { class: 'btn tiny ghost', type: 'button', onclick: async (e) => {
      e.preventDefault(); e.stopPropagation();
      try { await navigator.clipboard.writeText(get()); ui.toast('Copied'); } catch (_) { ui.toast('Copy is not available here', 'bad'); }
    } }, label);
  }

  drawHead();
  await loadList();
}
