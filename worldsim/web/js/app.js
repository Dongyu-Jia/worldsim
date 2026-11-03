// App shell: hash router, the current world, the backend badge. Each page module exports
//   async function render(root, ctx)
// ctx: {api, ui, wid, world, settings, worlds, signal, alive(), setDirty(bool), reloadWorld(), setWorld(id),
//       reloadWorlds(), go(route), topbar}
import * as api from './api.js';
import * as ui from './ui.js';

const PAGES = [
  { id: 'sim', name: 'Simulation', ico: '▶', needsWorld: true, load: () => import('./pages/sim.js') },
  { id: 'actors', name: 'Actors', ico: '◎', needsWorld: true, load: () => import('./pages/actors.js') },
  { id: 'schema', name: 'Schema', ico: '▦', needsWorld: true, load: () => import('./pages/schema.js') },
  { id: 'synth', name: 'Synthesizer', ico: '◇', needsWorld: true, load: () => import('./pages/synth.js') },
  { id: 'worlds', name: 'Worlds', ico: '◫', needsWorld: false, load: () => import('./pages/worlds.js') },
  { id: 'settings', name: 'Settings', ico: '⚙', needsWorld: false, load: () => import('./pages/settings.js') },
];
const STORE_KEY = 'worldsim.world';

const nav = document.getElementById('nav');
const view = document.getElementById('view');
const title = document.getElementById('page-title');
const topbar = document.getElementById('topbar-right');
const select = document.getElementById('world-select');
const backendInfo = document.getElementById('backend-info');

const state = { wid: null, world: null, worlds: [], settings: null, dirty: false, visit: null, route: 'sim' };

function remember(wid) { try { localStorage.setItem(STORE_KEY, wid || ''); } catch (_) { /* storage blocked */ } }
function recalled() { try { return localStorage.getItem(STORE_KEY) || null; } catch (_) { return null; } }

function routeOf(hash) {
  const id = String(hash || '').replace(/^#/, '').split('?')[0];
  return PAGES.some((p) => p.id === id) ? id : 'sim';
}

function drawNav() {
  ui.clear(nav);
  for (const p of PAGES) {
    nav.append(ui.h('a', { href: `#${p.id}`, class: p.id === state.route ? 'active' : '' },
      ui.h('span', { class: 'nav-ico', 'aria-hidden': 'true' }, p.ico), p.name));
  }
}

function drawSelect() {
  ui.clear(select);
  if (!state.worlds.length) select.append(ui.h('option', { value: '' }, '(no world)'));
  for (const w of state.worlds) {
    select.append(ui.h('option', { value: w.id, selected: w.id === state.wid }, w.name));
  }
}

function drawBackend() {
  ui.clear(backendInfo);
  const s = state.settings;
  if (!s) return;
  const mock = s.effective_backend === 'mock';
  ui.add(backendInfo, ui.h('div', null, 'LLM backend'),
    ui.h('div', null, ui.h('span', { class: `badge ${mock ? 'warn' : s.has_key ? 'ok' : 'bad'}` },
      mock ? 'Mock (no key)' : s.has_key ? 'OpenRouter' : 'OpenRouter · no key')),
    ui.h('div', { class: 'mono' }, mock ? '' : s.default_model),
    s.forced_backend ? ui.h('div', null, `forced by WORLDSIM_LLM=${s.forced_backend}`) : null);
}

async function reloadSettings() {
  state.settings = await api.get('settings');
  drawBackend();
  return state.settings;
}

async function reloadWorlds() {
  const data = await api.get('worlds');
  state.worlds = data.worlds || [];
  if (!state.worlds.some((w) => w.id === state.wid)) {
    const want = recalled();
    state.wid = state.worlds.some((w) => w.id === want) ? want : (state.worlds[0]?.id || null);
  }
  drawSelect();
  return state.worlds;
}

async function reloadWorld() {
  if (!state.wid) { state.world = null; return null; }
  const data = await api.get(`worlds/${api.enc(state.wid)}`);
  state.world = data.world;
  state.worldMeta = { steps: data.steps, pending: data.pending, busy: data.busy, calls_per_iteration: data.calls_per_iteration };
  return state.world;
}

async function setWorld(wid) {
  state.wid = wid;
  remember(wid);
  await reloadWorlds();
  await render();
}

async function confirmLeave() {
  if (!state.dirty) return true;
  const ok = await ui.confirmBox({ title: 'Discard unsaved changes?', body: 'This page has changes that are not saved.',
    ok: 'Discard', danger: true });
  if (ok) state.dirty = false;
  return ok;
}

async function render() {
  if (state.visit) state.visit.ctrl.abort();
  const ctrl = new AbortController();
  const visit = { ctrl };
  state.visit = visit;
  state.dirty = false;
  const page = PAGES.find((p) => p.id === state.route);
  title.textContent = page.name;
  document.title = `${page.name} · WorldSim`;
  drawNav();
  ui.clear(topbar);
  const root = ui.h('div', { class: 'page' });
  ui.clear(view).append(root);
  root.append(ui.h('div', { class: 'loading' }, 'Loading…'));
  try {
    if (page.needsWorld) await reloadWorld();
    if (visit !== state.visit) return;
    if (page.needsWorld && !state.world) {
      ui.clear(root).append(ui.h('div', { class: 'empty' }, 'There is no world yet. ',
        ui.h('a', { href: '#worlds' }, 'Create one on the Worlds page.')));
      return;
    }
    const mod = await page.load();
    if (visit !== state.visit) return;
    ui.clear(root);
    const ctx = {
      api, ui, wid: state.wid, world: state.world, meta: state.worldMeta, settings: state.settings, worlds: state.worlds,
      signal: ctrl.signal, topbar,
      alive: () => visit === state.visit,
      setDirty: (d) => { state.dirty = !!d; },
      reloadWorld: async () => { const w = await reloadWorld(); ctx.world = w; ctx.meta = state.worldMeta; return w; },
      reloadWorlds: async () => { const ws = await reloadWorlds(); ctx.worlds = ws; return ws; },
      reloadSettings: async () => { const s = await reloadSettings(); ctx.settings = s; return s; },
      setWorld, go: (r) => { location.hash = `#${r}`; },
      rerender: () => render(),
    };
    await mod.render(root, ctx);
  } catch (err) {
    if (visit !== state.visit) return;
    ui.clear(root).append(ui.errorBox(err, 'Could not load this page.'));
    console.warn(err);
  }
}

let lastHash = location.hash;
window.addEventListener('hashchange', async () => {
  const next = routeOf(location.hash);
  if (state.dirty && !(await confirmLeave())) { history.replaceState(null, '', lastHash || '#sim'); return; }
  lastHash = location.hash;
  state.route = next;
  render();
});

select.addEventListener('change', async () => {
  const wid = select.value;
  if (!(await confirmLeave())) { select.value = state.wid || ''; return; }
  await setWorld(wid);
});

window.addEventListener('beforeunload', (e) => { if (state.dirty) { e.preventDefault(); e.returnValue = ''; } });

(async function start() {
  state.route = routeOf(location.hash);
  try {
    await Promise.all([reloadSettings(), reloadWorlds()]);
  } catch (err) {
    ui.clear(view).append(ui.errorBox(err, 'Cannot reach the WorldSim server.'));
    return;
  }
  render();
})();
