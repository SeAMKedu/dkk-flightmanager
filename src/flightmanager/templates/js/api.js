// ── Thin fetch wrapper: parsed JSON + consistent errors ──────────────────────
//
// Every JSON API call goes through here so error handling is uniform and there
// is a single place to add an auth header later (hosting). Non-JSON responses
// (file downloads, SSE) intentionally keep their own raw fetch / EventSource.

export class ApiError extends Error {
  constructor(detail, status) {
    super(detail || ('HTTP ' + status));
    this.name = 'ApiError';
    this.status = status;
    this.detail = detail || ('HTTP ' + status);
  }
}

async function _request(method, url, body) {
  var opts = {method: method};
  if (body !== undefined) {
    opts.headers = {'Content-Type': 'application/json'};
    opts.body = JSON.stringify(body);
  }
  var r = await fetch(url, opts);
  var data = null;
  try { data = await r.json(); } catch (e) { /* empty / non-JSON body */ }
  if (!r.ok) {
    throw new ApiError((data && data.detail) || null, r.status);
  }
  return data;
}

export function apiGet(url)          { return _request('GET', url); }
export function apiPost(url, body)   { return _request('POST', url, body); }
export function apiPatch(url, body)  { return _request('PATCH', url, body); }
export function apiDelete(url)       { return _request('DELETE', url); }
