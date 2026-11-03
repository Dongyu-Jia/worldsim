// Thin JSON wrapper around /api. Every change is a JSON request from this page (the server refuses anything else).
// An error carries .status, .message and .problems (the server's list of reasons, when it sent one).

async function handle(res) {
  let data = null;
  try { data = await res.json(); } catch (_) { data = null; }
  if (!res.ok) {
    const d = data && data.detail;
    let msg = `${res.status} ${res.statusText}`;
    let problems = [];
    if (typeof d === 'string') msg = d;
    else if (d && typeof d === 'object') { msg = d.message || msg; problems = Array.isArray(d.problems) ? d.problems : []; }
    const err = new Error(msg);
    err.status = res.status;
    err.problems = problems;
    throw err;
  }
  return data;
}

function send(method, path, body) {
  return fetch(`/api/${path}`, {
    method,
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(body ?? {}),
  }).then(handle);
}

export const get = (path) => fetch(`/api/${path}`, { headers: { Accept: 'application/json' } }).then(handle);
export const post = (path, body) => send('POST', path, body);
export const put = (path, body) => send('PUT', path, body);
export const del = (path, body) => send('DELETE', path, body);
export const enc = encodeURIComponent;
