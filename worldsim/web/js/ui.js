// DOM helpers. Every text goes in as textContent: nothing from the server or a model is ever parsed as HTML.
import * as api from './api.js';

// h('div', {class: 'x', onclick: fn, title: '…'}, child, 'text', [more], null)
export function h(tag, attrs, ...children) {
  const el = document.createElement(tag);
  if (attrs) {
    for (const [k, v] of Object.entries(attrs)) {
      if (v === undefined || v === null || v === false) continue;
      if (k === 'class') el.className = v;
      else if (k === 'text') el.textContent = String(v);
      else if (k.startsWith('on') && typeof v === 'function') el.addEventListener(k.slice(2), v);
      else if (k === 'value') el.value = v;
      else if (k === 'checked') el.checked = !!v;
      else if (k === 'disabled') el.disabled = !!v;
      else if (k === 'selected') el.selected = !!v;
      else if (k === 'dataset') Object.assign(el.dataset, v);
      else if (k === 'style' || k === 'html' || k === 'innerHTML') continue;   // never
      else el.setAttribute(k, v === true ? '' : String(v));
    }
  }
  append(el, children);
  return el;
}

function append(el, children) {
  for (const c of children) {
    if (c === null || c === undefined || c === false) continue;
    if (Array.isArray(c)) append(el, c);
    else if (c instanceof Node) el.append(c);
    else el.append(document.createTextNode(String(c)));
  }
}

export function clear(el) { el.replaceChildren(); return el; }

// el.append(...) that skips null / false and flattens arrays (the native append would print "null").
export function add(el, ...children) { append(el, children); return el; }

// ------------------------------------------------------------------ numbers and values
export function num(x, maxFrac = 4) {
  if (typeof x !== 'number' || !Number.isFinite(x)) return String(x ?? '—');
  const abs = Math.abs(x);
  const frac = abs >= 1000 ? Math.min(maxFrac, 1) : maxFrac;
  return x.toLocaleString('en-US', { maximumFractionDigits: frac });
}

export const NUMERIC = new Set(['number', 'percent', 'integer']);

export function valueText(field, v) {
  if (v === null || v === undefined) return '—';
  if (field.type === 'boolean') return v ? 'true' : 'false';
  if (NUMERIC.has(field.type) && typeof v === 'number') return num(v);
  return String(v);
}

export function unitText(field) {
  const u = field.unit || '';
  if (field.type === 'percent' && !u.includes('%')) return ('% ' + u).trim();
  return u;
}

export function deltaOf(field, prev, cur) {
  if (prev === undefined) return null;
  if (NUMERIC.has(field.type) && typeof prev === 'number' && typeof cur === 'number') {
    const d = cur - prev;
    if (Math.abs(d) < 1e-12) return null;
    const pp = field.type === 'percent' || (field.unit || '').startsWith('%');
    const s = (d > 0 ? '+' : '−') + num(Math.abs(d)) + (pp ? ' pp' : '');
    return { text: s, cls: d > 0 ? 'up' : 'down' };
  }
  if (prev !== cur && !(prev == null && cur == null)) return { text: 'changed', cls: 'txt' };
  return null;
}

export function money(x, digits) {
  if (typeof x !== 'number' || !Number.isFinite(x)) return '—';
  if (x === 0) return '$0';
  const d = digits ?? (x < 0.01 ? 5 : x < 1 ? 4 : 2);
  return '$' + x.toFixed(d);
}

export function tokens(x) { return typeof x === 'number' ? x.toLocaleString('en-US') : '—'; }

export function when(iso) {
  if (!iso) return '';
  const d = new Date(iso);
  return Number.isNaN(d.getTime()) ? String(iso) : d.toLocaleString();
}

// ------------------------------------------------------------------ feedback
export function toast(msg, kind = '') {
  const box = document.getElementById('toasts');
  const t = h('div', { class: `toast ${kind}`, role: 'status' }, msg);
  box.append(t);
  setTimeout(() => t.remove(), kind === 'bad' ? 7000 : 3500);
}

export function errorBox(err, title) {
  const problems = (err && err.problems) || [];
  return h('div', { class: 'banner bad', role: 'alert' },
    h('strong', null, title || 'Error'), ' ', err && err.message ? err.message : String(err),
    problems.length ? h('ul', null, problems.slice(0, 30).map((p) => h('li', null, p))) : null);
}

// ------------------------------------------------------------------ modal dialogs
const modal = () => document.getElementById('modal');

function openModal(build, { wide = false } = {}) {
  const dlg = modal();
  return new Promise((resolve) => {
    let done = false;
    const finish = (v) => { if (done) return; done = true; dlg.close(); resolve(v); };
    dlg.className = `modal${wide ? ' wide' : ''}`;
    clear(dlg);
    dlg.append(...build(finish));
    dlg.onclose = () => finish(null);
    dlg.oncancel = (e) => { e.preventDefault(); finish(null); };
    dlg.showModal();
    const first = dlg.querySelector('[data-autofocus]') || dlg.querySelector('input, textarea, select, button');
    if (first) first.focus();
  });
}

export function confirmBox({ title = 'Are you sure?', body = '', ok = 'OK', danger = false } = {}) {
  return openModal((finish) => [
    h('div', { class: 'modal-head' }, title),
    h('div', { class: 'modal-body' }, typeof body === 'string' ? h('p', null, body) : body),
    h('div', { class: 'modal-foot' },
      h('button', { class: 'btn', type: 'button', onclick: () => finish(false) }, 'Cancel'),
      h('button', { class: `btn ${danger ? 'danger solid' : 'primary'}`, type: 'button', 'data-autofocus': true,
        onclick: () => finish(true) }, ok)),
  ]).then((v) => v === true);
}

export function promptBox({ title, label, value = '', ok = 'OK' }) {
  return openModal((finish) => {
    const input = h('input', { type: 'text', value, 'data-autofocus': true });
    input.addEventListener('keydown', (e) => { if (e.key === 'Enter') finish(input.value.trim()); });
    return [
      h('div', { class: 'modal-head' }, title),
      h('div', { class: 'modal-body' }, h('label', { class: 'field' }, h('span', null, label), input)),
      h('div', { class: 'modal-foot' },
        h('button', { class: 'btn', type: 'button', onclick: () => finish(null) }, 'Cancel'),
        h('button', { class: 'btn primary', type: 'button', onclick: () => finish(input.value.trim()) }, ok)),
    ];
  });
}

export function infoBox({ title, body, wide = false }) {
  return openModal((finish) => [
    h('div', { class: 'modal-head' }, title),
    h('div', { class: 'modal-body' }, body),
    h('div', { class: 'modal-foot' }, h('button', { class: 'btn primary', type: 'button', onclick: () => finish(true) }, 'Close')),
  ], { wide });
}

// ------------------------------------------------------------------ the model list and picker
let modelsPromise = null;
export function loadModels(refresh = false) {
  if (refresh || !modelsPromise) {
    modelsPromise = api.get(`models${refresh ? '?refresh=1' : ''}`).catch((e) => { modelsPromise = null; throw e; });
  }
  return modelsPromise;
}

export function perM(p) { return typeof p === 'number' ? '$' + num(p * 1e6, 2) : '?'; }

export function modelSummary(m) {
  if (!m) return 'not in the model list (free text: check the id on openrouter.ai)';
  const bits = [`${perM(m.prompt_price)}/M in`, `${perM(m.completion_price)}/M out`];
  if (m.context_length) bits.push(`${num(m.context_length)} context`);
  bits.push(m.structured_outputs ? 'structured outputs' : m.response_format ? 'JSON mode' : 'no JSON mode');
  if (m.reasoning) bits.push(`reasoning: ${m.reasoning_efforts.join('/')}${m.reasoning_mandatory ? ' (always on)' : ''}`);
  return bits.join(' · ');
}

export async function pickModel(current = '') {
  let data;
  try { data = await loadModels(); } catch (e) { toast(`Model list: ${e.message}`, 'bad'); data = { models: [] }; }
  const models = data.models || [];
  return openModal((finish) => {
    const search = h('input', { type: 'search', placeholder: 'Search models (id or name)…', value: '', 'data-autofocus': true,
      'aria-label': 'Search models' });
    const results = h('div', { class: 'model-results', role: 'listbox' });
    const custom = h('input', { type: 'text', value: current, placeholder: 'or type any model id, e.g. google/gemini-4-argon' });
    const draw = () => {
      const q = search.value.trim().toLowerCase();
      const hits = models.filter((m) => !q || m.id.toLowerCase().includes(q) || m.name.toLowerCase().includes(q)).slice(0, 150);
      clear(results);
      if (!hits.length) results.append(h('div', { class: 'empty' }, 'No model matches.'));
      for (const m of hits) {
        results.append(h('button', { type: 'button', class: 'model-opt', role: 'option', onclick: () => finish(m.id) },
          h('div', null, h('div', { class: 'mid' }, m.id), h('div', { class: 'mname' }, m.name)),
          h('div', { class: 'mprice' }, `${perM(m.prompt_price)} / ${perM(m.completion_price)} per M`,
            h('div', { class: 'mname' }, m.structured_outputs ? 'structured outputs' : (m.response_format ? 'JSON mode' : '')))));
      }
    };
    search.addEventListener('input', draw);
    draw();
    const note = data.source === 'openrouter' || data.source === 'cache'
      ? `${models.length} models from OpenRouter${data.fetched_at ? ' · fetched ' + new Date(data.fetched_at * 1000).toLocaleString() : ''}`
      : (data.note || 'Model list unavailable');
    return [
      h('div', { class: 'modal-head' }, 'Choose a model'),
      h('div', { class: 'modal-body' }, search, h('div', { class: 'small muted' }, note, data.note && data.source !== 'builtin' ? ` · ${data.note}` : ''),
        results, h('label', { class: 'field' }, h('span', null, 'Model id (free text)'), custom)),
      h('div', { class: 'modal-foot' },
        h('button', { class: 'btn', type: 'button', onclick: () => finish(null) }, 'Cancel'),
        h('button', { class: 'btn', type: 'button', onclick: () => finish('') }, 'Clear (use default)'),
        h('button', { class: 'btn primary', type: 'button', onclick: () => finish(custom.value.trim()) }, 'Use typed id')),
    ];
  }, { wide: true });
}

// A model input with a Browse button. onchange(value) is called on every change.
export function modelInput(value, { placeholder = 'default', onchange } = {}) {
  const input = h('input', { type: 'text', value: value || '', placeholder, 'aria-label': 'Model id' });
  const info = h('div', { class: 'hint' });
  const describe = async () => {
    const id = input.value.trim();
    if (!id) { info.textContent = placeholder ? `Uses ${placeholder}` : ''; return; }
    try {
      const data = await loadModels();
      info.textContent = modelSummary((data.models || []).find((m) => m.id === id));
    } catch (_) { info.textContent = ''; }
  };
  input.addEventListener('input', () => { onchange && onchange(input.value.trim()); describe(); });
  const browse = h('button', { class: 'btn', type: 'button', onclick: async () => {
    const id = await pickModel(input.value.trim());
    if (id !== null) { input.value = id; onchange && onchange(id); describe(); }
  } }, 'Browse…');
  describe();
  return { el: h('div', { class: 'stack' }, h('div', { class: 'list-row' }, input, browse), info), input };
}

// A list of short texts (one input per row) with add / remove.
export function textList(items, { placeholder = '', onchange, addLabel = 'Add' } = {}) {
  const box = h('div', { class: 'list-editor' });
  const list = items.slice();
  const draw = () => {
    clear(box);
    list.forEach((t, i) => {
      const inp = h('input', { type: 'text', value: t, placeholder });
      inp.addEventListener('input', () => { list[i] = inp.value; onchange && onchange(list.slice()); });
      box.append(h('div', { class: 'list-row' }, inp,
        h('button', { class: 'btn tiny ghost', type: 'button', title: 'Remove', 'aria-label': 'Remove',
          onclick: () => { list.splice(i, 1); onchange && onchange(list.slice()); draw(); } }, '✕')));
    });
    box.append(h('div', null, h('button', { class: 'btn tiny', type: 'button',
      onclick: () => { list.push(''); onchange && onchange(list.slice()); draw(); box.querySelectorAll('input')[list.length - 1]?.focus(); } }, `+ ${addLabel}`)));
  };
  draw();
  return box;
}

export function field(label, control, hint) {
  return h('label', { class: 'field' }, h('span', null, label), control, hint ? h('span', { class: 'hint' }, hint) : null);
}

export function slugify(text, fallback = 'item') {
  let s = String(text || '').toLowerCase().replace(/[^a-z0-9]+/g, '_').replace(/^_+|_+$/g, '');
  if (!s || !/^[a-z]/.test(s)) s = s ? `${fallback}_${s}` : fallback;
  return s.slice(0, 48).replace(/_+$/, '') || fallback;
}

export function uniqueId(base, taken) {
  if (!taken.has(base)) return base;
  for (let i = 2; i < 10000; i++) { const c = `${base.slice(0, 44)}_${i}`; if (!taken.has(c)) return c; }
  return base + Date.now();
}

export const ID_RE = /^[a-z][a-z0-9_]{0,47}$/;
