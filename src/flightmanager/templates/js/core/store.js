// ── Tiny reactive store (zero dependencies, no build step) ────────────────────
// createStore(obj) returns a Proxy over `obj`: reads and writes work exactly
// like a plain object — `st.foo`, `st.foo = 1` — so existing call sites need no
// changes. The difference is that a write fires any subscribers registered for
// that key, so derived UI (e.g. the Save button) can react instead of every
// caller remembering to refresh it by hand.
//
//   const st = createStore({ dirty: false });
//   st.subscribe('dirty', (val, old) => updateButton());
//   st.dirty = true;   // updateButton() runs automatically
//   st.subscribe(['a', 'b'], cb);   // multiple keys
//   st.subscribe('*', cb);          // any key
//
// subscribe() returns an unsubscribe function. Writes that don't change the
// value (old === new) are no-ops and fire nothing.
//
// Why hand-rolled and not nanostores/valtio:
//   - nanostores is atom-based (`$store.set(...)`), so adopting it would mean
//     rewriting every `st.foo = x` across the app — it can't wrap an existing
//     mutable object, which is the whole point here (zero call-site changes).
//   - valtio IS the close cousin (proxy + subscribe) and would be the natural
//     upgrade, but its payoff is React (`useSnapshot`) — we have no component
//     layer — and it adds deep-proxy semantics we don't need for a flat `st`.
//   This API (mutate normally + per-key `subscribe`) is intentionally kept tiny
//   and valtio-compatible: if we later add Preact/React, need derived state, or
//   want devtools, swapping in valtio (`subscribe`/`subscribeKey`) is mechanical.

export function createStore(initial) {
  const listeners = new Map(); // key (or '*') -> Set<callback>

  function notify(key, val, old) {
    const fire = (cb) => {
      try { cb(val, old, key); }
      catch (e) { console.error('[store] subscriber for "' + key + '" threw', e); }
    };
    const direct = listeners.get(key); if (direct) direct.forEach(fire);
    const any = listeners.get('*');    if (any)    any.forEach(fire);
  }

  function subscribe(keys, cb) {
    const arr = Array.isArray(keys) ? keys : [keys];
    arr.forEach((k) => {
      if (!listeners.has(k)) listeners.set(k, new Set());
      listeners.get(k).add(cb);
    });
    return function unsubscribe() {
      arr.forEach((k) => { const s = listeners.get(k); if (s) s.delete(cb); });
    };
  }

  return new Proxy(initial, {
    get(target, key) {
      if (key === 'subscribe') return subscribe; // not stored on the target
      return target[key];
    },
    set(target, key, val) {
      const old = target[key];
      if (old === val) return true; // no-op write: don't fire
      target[key] = val;
      notify(key, val, old);
      return true;
    },
  });
}
