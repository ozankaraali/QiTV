// What this page remembers, in this browser and for this address only: the TVs it paired with
// (keyed by the TV's public key) and the phone's own choices. Storage can be missing or full
// (a private window); then nothing is remembered and the page still works this time.
const KEY = "qitv-remote";

function load() {
  try {
    const data = JSON.parse(localStorage.getItem(KEY) || "{}");
    return data && typeof data === "object" ? data : {};
  } catch {
    return {};
  }
}

function save(data) {
  try {
    localStorage.setItem(KEY, JSON.stringify(data));
  } catch {
    // not remembered: the page keeps working for now
  }
}

export function tvs() {
  return load().tvs || {};
}

export function getTv(id) {
  return tvs()[id] || null;
}

export function putTv(id, record) {
  const data = load();
  data.tvs = { ...(data.tvs || {}), [id]: { ...(data.tvs || {})[id], ...record } };
  data.last = id;
  save(data);
}

export function forgetTv(id) {
  const data = load();
  if (data.tvs) delete data.tvs[id];
  if (data.last === id) delete data.last;
  save(data);
}

export function lastTv() {
  const data = load();
  const all = data.tvs || {};
  if (data.last && all[data.last]) return data.last;
  return Object.keys(all)[0] || null;
}

export function setLastTv(id) {
  const data = load();
  data.last = id;
  save(data);
}

export function setting(name, fallback) {
  const value = (load().settings || {})[name];
  return value === undefined ? fallback : value;
}

export function setSetting(name, value) {
  const data = load();
  data.settings = { ...(data.settings || {}), [name]: value };
  save(data);
}
