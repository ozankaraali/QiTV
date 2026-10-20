// The phone remote page. Pairs by the code in the address (the TV's QR), then drives QiTV:
// a press sends "down", a release "up" with the steps this phone counted (QiTV repeats a held
// button itself and stops where the finger did), "held" every 200 ms while a button is down.
// Anything that may hide a release (the page hidden, focus lost, a cancelled touch) releases
// everything at once.
import { Connection, b64d, b64e, codePsk } from "./conn.js";
import { Keypair } from "./noise.js";
import * as store from "./store.js";

const DELAY_MS = 400;
const INTERVAL_MS = 40;
const KEEPALIVE_MS = 200;
const RETRY_MS = [500, 1000, 2000, 5000];

// The same count as services/remote/protocol.py steps_for.
export const stepsFor = (ms) => (ms < DELAY_MS ? 1 : 2 + Math.floor((ms - DELAY_MS) / INTERVAL_MS));

const $ = (id) => document.getElementById(id);
const strings = {};
const FALLBACK = {
  code_expired: "The code expired. Scan the new code on the TV.",
  removed: "This phone was removed in QiTV. Scan the code on the TV to pair again.",
  reconnecting: "Reconnecting…",
  offline: "Can't reach QiTV. Is it open on the computer?",
  forget: "Forget this TV",
  pair_another: "Pair another TV",
};
export const t = (key) => strings[key] || FALLBACK[key] || key;

const app = {
  tvId: null, // the TV's public key, base64url
  conn: null,
  retry: 0,
  retryTimer: null,
  held: new Map(), // button → {t0, el}
  keepalive: null,
  handlers: {}, // message type → handler, filled by the other parts of the page
};
export default app;

export function phoneName() {
  const ua = navigator.userAgent;
  const device = /iPhone/.test(ua) ? "iPhone" : /iPad/.test(ua) ? "iPad" : /Android/.test(ua) ? "Android phone" : "Phone";
  const browser = /Edg\//.test(ua) ? "Edge" : /FxiOS|Firefox/.test(ua) ? "Firefox" : /CriOS|Chrome/.test(ua) ? "Chrome" : /Safari/.test(ua) ? "Safari" : "Browser";
  return `${device} · ${browser}`.slice(0, 40);
}

// ---- screens ------------------------------------------------------------------------------

export function show(name) {
  for (const section of document.querySelectorAll("[data-screen]")) section.hidden = section.dataset.screen !== name;
  const tabbed = ["remote", "guide", "library", "search"].includes(name);
  $("tabs").hidden = !tabbed;
  for (const tab of document.querySelectorAll("[data-tab]")) tab.classList.toggle("on", tab.dataset.tab === name);
}

export function banner(text, kind = "") {
  const el = $("banner");
  el.hidden = !text;
  el.textContent = text || "";
  el.className = `banner ${kind}`;
}

function showScan(message) {
  show("scan");
  $("scanMessage").textContent = message || strings.scan_hint || $("scanMessage").textContent;
  $("tvName").textContent = "QiTV";
  setDot("");
}

export function setDot(kind) {
  $("tvButton").classList.toggle("online", kind === "online");
  $("tvButton").classList.toggle("relay", kind === "relay");
}

function applyStrings(table) {
  Object.assign(strings, table || {});
  for (const el of document.querySelectorAll("[data-t]")) if (strings[el.dataset.t]) el.textContent = strings[el.dataset.t];
  for (const el of document.querySelectorAll("[data-tp]")) if (strings[el.dataset.tp]) el.placeholder = strings[el.dataset.tp];
  if (strings.lang) document.documentElement.lang = strings.lang;
}

// ---- the connection -----------------------------------------------------------------------

export function homeSocketUrl() {
  return `${location.protocol === "https:" ? "wss" : "ws"}://${location.host}/ws`;
}

// How a TV is reached from this page; the relay door replaces it (relay.js).
app.socketUrl = () => homeSocketUrl();

export function pair({ tvId, code, room }) {
  const key = Keypair.generate();
  show("connecting");
  open({
    tvId,
    opening: { v: 1, mode: "pair" },
    pattern: "NKpsk0",
    psk: codePsk(b64d(code)),
    payload: { pub: b64e(key.pub), name: phoneName() },
    onanswer: (answer) => {
      store.putTv(tvId, {
        name: answer.tv,
        device: answer.device,
        keyId: answer.key,
        priv: b64e(key.priv),
        relay: answer.relay || null,
        room: answer.room || room || null,
      });
    },
  });
}

export function connect(tvId) {
  const record = store.getTv(tvId);
  if (!record) {
    showScan();
    return;
  }
  open({
    tvId,
    opening: { v: 1, mode: "kk", key: record.keyId },
    pattern: "KK",
    s: Keypair.fromPrivate(b64d(record.priv)),
    payload: {},
    onanswer: () => store.setLastTv(tvId),
  });
}

function open({ tvId, opening, pattern, s = null, psk = null, payload, onanswer }) {
  clearTimeout(app.retryTimer);
  if (app.conn) app.conn.end(1000, "replaced");
  app.tvId = tvId;
  const conn = new Connection({
    url: app.socketUrl(tvId),
    opening,
    pattern,
    s,
    psk,
    rs: b64d(tvId),
    payload,
    onopen: (answer) => {
      app.retry = 0;
      onanswer(answer);
    },
    onmessage: (message) => onMessage(message),
    onclose: (code, reason) => {
      if (app.conn === conn) onClose(code, reason, opening.mode);
    },
  });
  app.conn = conn;
}

function onMessage(message) {
  if (message.t === "hello") {
    applyStrings(message.strings);
    $("tvName").textContent = message.tv || "QiTV";
    setDot(message.door === "relay" ? "relay" : "online");
    banner("");
    if ($("tabs").hidden) show("remote");
  } else if (message.t === "bye") {
    if (message.reason === "removed") forget(t("removed"));
  }
  const handler = app.handlers[message.t];
  if (handler) handler(message);
}

function onClose(code, reason, mode) {
  releaseAll();
  setDot("");
  if (code === 4401 && reason === "pair-expired") {
    app.conn = null;
    showScan(t("code_expired"));
    return;
  }
  if ((code === 4401 && mode !== "pair") || code === 4403) {
    forget(t("removed"));
    return;
  }
  if (app.onclose && app.onclose(code, reason)) return; // the relay door's own cases
  scheduleRetry();
}

function forget(message) {
  if (app.tvId) store.forgetTv(app.tvId);
  if (app.conn) app.conn.end(1000, "forgotten");
  app.conn = null;
  app.tvId = null;
  showScan(message);
}

function scheduleRetry() {
  const delay = RETRY_MS[Math.min(app.retry, RETRY_MS.length - 1)];
  app.retry += 1;
  banner(app.retry > RETRY_MS.length ? t("offline") : t("reconnecting"));
  clearTimeout(app.retryTimer);
  app.retryTimer = setTimeout(() => {
    if (app.tvId && document.visibilityState !== "hidden") connect(app.tvId);
  }, delay);
}

export function send(message) {
  return app.conn ? app.conn.send(message) : false;
}

// ---- presses and holds --------------------------------------------------------------------

export function press(button, el) {
  if (app.held.has(button)) return;
  app.held.set(button, { t0: performance.now(), el });
  if (el) el.classList.add("pressed");
  if (navigator.vibrate) navigator.vibrate(8);
  send({ t: "down", b: button });
  if (!app.keepalive) app.keepalive = setInterval(() => send({ t: "held" }), KEEPALIVE_MS);
}

export function release(button) {
  const hold = app.held.get(button);
  if (!hold) return;
  app.held.delete(button);
  if (hold.el) hold.el.classList.remove("pressed");
  send({ t: "up", b: button, n: stepsFor(performance.now() - hold.t0) });
  if (!app.held.size) {
    clearInterval(app.keepalive);
    app.keepalive = null;
  }
}

export function releaseAll() {
  for (const button of [...app.held.keys()]) release(button);
}

export function tap(button) {
  send({ t: "down", b: button });
  send({ t: "up", b: button, n: 1 });
  if (navigator.vibrate) navigator.vibrate(8);
}

function bindButtons() {
  for (const el of document.querySelectorAll("[data-key]")) {
    const button = el.dataset.key;
    el.addEventListener("pointerdown", (event) => {
      event.preventDefault();
      if (el.setPointerCapture) el.setPointerCapture(event.pointerId);
      press(button, el);
    });
    for (const type of ["pointerup", "pointercancel", "lostpointercapture"]) el.addEventListener(type, () => release(button));
    el.addEventListener("contextmenu", (event) => event.preventDefault());
  }
  document.addEventListener("visibilitychange", () => {
    if (document.visibilityState === "hidden") releaseAll();
    else if (app.tvId && !(app.conn && app.conn.open)) connect(app.tvId);
  });
  window.addEventListener("pagehide", releaseAll);
  window.addEventListener("blur", releaseAll);
}

// ---- sheets and tabs ----------------------------------------------------------------------

export function openSheet(id) {
  $("veil").hidden = false;
  $(id).hidden = false;
}

export function closeSheets() {
  $("veil").hidden = true;
  for (const sheet of document.querySelectorAll(".sheet")) sheet.hidden = true;
}

function bindChrome() {
  $("moreButton").addEventListener("click", () => openSheet("moreSheet"));
  $("veil").addEventListener("click", closeSheets);
  for (const el of document.querySelectorAll("[data-close]")) el.addEventListener("click", closeSheets);
  for (const tab of document.querySelectorAll("[data-tab]")) {
    tab.addEventListener("click", () => {
      show(tab.dataset.tab);
      const handler = app.handlers[`tab:${tab.dataset.tab}`];
      if (handler) handler();
    });
  }
  $("menuButton").addEventListener("click", openMenu);
}

export function menuItem(text, onClick, className = "") {
  const item = document.createElement("button");
  item.type = "button";
  item.className = className;
  item.textContent = text;
  item.addEventListener("click", () => {
    closeSheets();
    onClick();
  });
  return item;
}

function openMenu() {
  const list = $("menuList");
  list.replaceChildren();
  for (const extra of app.menuExtras || []) {
    const item = extra();
    if (item) list.append(item);
  }
  if (app.tvId) {
    list.append(
      menuItem(t("forget"), () => {
        send({ t: "forget" });
        forget();
      }, "danger"),
    );
  }
  openSheet("menuSheet");
}

// ---- start --------------------------------------------------------------------------------

export function start() {
  bindButtons();
  bindChrome();
  const hash = location.hash.slice(1);
  if (hash) history.replaceState(null, "", location.pathname + location.search);
  if (app.route && app.route(hash)) return; // the relay door's addresses (relay.js)
  if (hash.startsWith("1.")) {
    const [, room, tvId, code] = hash.split(".");
    if (tvId && code) {
      pair({ tvId, code, room: room === "-" ? null : room });
      return;
    }
  }
  const last = store.lastTv();
  if (last) connect(last);
  else showScan();
}

