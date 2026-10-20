// The second screen: what QiTV says is on, the guide, the search's results and the Library,
// drawn from QiTV's snapshots (ui/remote_snapshots.py). Taps send back the opaque handles QiTV
// gave; nothing here knows a stream's address. Also the d-pad's touchpad mode.
import app, { press, release, send, show, t, tap } from "./app.js";
import * as store from "./store.js";

const $ = (id) => document.getElementById(id);
const COLORS = ["#2f5d73", "#2f6b52", "#3d4f7a", "#7a5a2f", "#5a3d6b", "#6b4a3a"];
const FLICK_PX = 24;
const TAP_PX = 10;
const HOLD_MS = 250;
const QUERY_DEBOUNCE_MS = 150;

const words = {
  watching: "Watching",
  next: "Next",
  continue_watching: "Continue watching",
  my_list: "My list",
  channels: "Channels",
  movies: "Movies",
  series: "Series",
  no_results: "Nothing found",
  nothing_here: "Nothing here yet",
  loading: "Loading…",
  use_touchpad: "Use a touchpad",
  use_buttons: "Use buttons",
};
const w = (key) => (t(key) === key ? words[key] || key : t(key));

// ---- helpers ------------------------------------------------------------------------------

function initials(name) {
  return (name || "")
    .split(/\s+/)
    .filter(Boolean)
    .map((part) => part[0])
    .join("")
    .slice(0, 2)
    .toUpperCase();
}

function colour(name) {
  let hash = 0;
  for (const c of name || "") hash = (hash * 31 + c.charCodeAt(0)) >>> 0;
  return COLORS[hash % COLORS.length];
}

function el(tag, className, text) {
  const node = document.createElement(tag);
  if (className) node.className = className;
  if (text !== undefined) node.textContent = text;
  return node;
}

function clock(seconds) {
  const d = new Date(seconds * 1000);
  return d.toLocaleTimeString([], { hour: "2-digit", minute: "2-digit" });
}

function share(programme, nowS = Date.now() / 1000) {
  if (!programme || !programme.start || !programme.stop || programme.stop <= programme.start) return 0;
  return Math.min(1, Math.max(0, (nowS - programme.start) / (programme.stop - programme.start)));
}

function logoTile(name, className = "logo") {
  const tile = el("div", className, initials(name));
  tile.style.background = colour(name);
  return tile;
}

function tapRow(row, handle) {
  row.addEventListener("pointerdown", () => row.classList.add("pressed"));
  for (const type of ["pointerup", "pointercancel", "pointerleave"]) row.addEventListener(type, () => row.classList.remove("pressed"));
  row.addEventListener("click", () => {
    if (navigator.vibrate) navigator.vibrate(8);
    send({ t: "open", id: handle });
  });
}

// ---- what's on ----------------------------------------------------------------------------

let current = null; // the last state, with when it arrived
let film = null; // {pos, len, at, playing}

function onState(message) {
  current = message;
  const now = message.now;
  film = now && now.kind !== "live" && now.len_ms ? { pos: now.pos_ms || 0, len: now.len_ms, at: Date.now(), playing: !!now.playing } : null;
  drawNow();
  for (const button of document.querySelectorAll(".seg [data-section]")) {
    button.classList.toggle("on", button.dataset.section === message.section && message.context !== "guide");
  }
  const guide = document.querySelector(".seg [data-key=guide]");
  if (guide) guide.classList.toggle("on", message.context === "guide");
  const mute = document.querySelector("[data-key=mute]");
  if (mute) mute.classList.toggle("on", !!message.muted);
}

function drawNow() {
  const now = current && current.now;
  const name = $("nowName");
  const live = $("nowLive");
  if (!now) {
    name.textContent = t("nothing_playing") === "nothing_playing" ? "Nothing playing" : t("nothing_playing");
    $("nowNum").textContent = "";
    $("nowShow").textContent = "";
    $("nowProg").style.width = "0%";
    live.hidden = true;
    const logo = $("nowLogo");
    logo.textContent = "TV";
    logo.style.background = "";
    return;
  }
  name.textContent = now.name || "";
  const logo = $("nowLogo");
  logo.textContent = initials(now.name) || "TV";
  logo.style.background = colour(now.name);
  if (now.kind === "live") {
    $("nowNum").textContent = now.num || "";
    live.hidden = false;
    const behind = now.behind_s || 0;
    live.classList.toggle("behind", behind > 0);
    live.textContent = behind > 0 ? `−${Math.floor(behind / 60)}:${String(behind % 60).padStart(2, "0")}` : t("live") === "live" ? "LIVE" : t("live");
    $("nowShow").textContent = now.now ? now.now.title : "";
    $("nowProg").style.width = `${(share(now.now) * 100).toFixed(1)}%`;
  } else {
    $("nowNum").textContent = "";
    live.hidden = true;
    $("nowShow").textContent = now.sub || "";
    let pos = film ? film.pos + (film.playing ? Date.now() - film.at : 0) : 0;
    if (film) pos = Math.min(pos, film.len);
    $("nowProg").style.width = film ? `${((pos / film.len) * 100).toFixed(1)}%` : "0%";
  }
}

// ---- the guide ----------------------------------------------------------------------------

const guide = { list: "all", page: 0, pages: 1, rows: [], asking: false };

function askGuide(list, page) {
  guide.asking = true;
  send({ t: "get", what: "guide", list, page });
}

function openGuide() {
  guide.rows = [];
  guide.page = 0;
  $("guideRows").replaceChildren(el("div", "empty", w("loading")));
  askGuide(guide.list, 0);
}

function onGuide(message) {
  if (message.list !== guide.list) return;
  guide.asking = false;
  guide.pages = message.pages || 1;
  guide.page = message.page;
  guide.rows = message.page === 0 ? message.rows : guide.rows.concat(message.rows);
  drawGuide();
}

function guideRow(row) {
  const item = el("button", `row${row.playing ? " playing" : ""}`);
  item.type = "button";
  item.append(logoTile(row.name));
  const main = el("div", "row-main");
  const top = el("div", "row-top");
  if (row.num) top.append(el("span", "num", row.num));
  top.append(el("span", "title", row.name));
  if (row.playing) top.append(el("em", "", w("watching")));
  main.append(top);
  if (row.now) {
    main.append(el("div", "row-now", row.now.title));
    const bar = el("div", "prog");
    const fill = el("i");
    fill.style.width = `${(share(row.now) * 100).toFixed(1)}%`;
    bar.append(fill);
    main.append(bar);
  }
  if (row.next) main.append(el("div", "row-next", `${clock(row.next.start)} ${row.next.title}`));
  item.append(main);
  tapRow(item, row.id);
  return item;
}

function drawGuide() {
  const find = $("guideFind").value.trim().toLowerCase();
  const rows = find
    ? guide.rows.filter((r) => `${r.num} ${r.name} ${r.now ? r.now.title : ""}`.toLowerCase().includes(find))
    : guide.rows;
  const box = $("guideRows");
  box.replaceChildren(...(rows.length ? rows.map(guideRow) : [el("div", "empty", w("nothing_here"))]));
}

function bindGuide() {
  for (const chip of document.querySelectorAll("#guideChips [data-list]")) {
    chip.addEventListener("click", () => {
      for (const other of document.querySelectorAll("#guideChips [data-list]")) other.classList.toggle("on", other === chip);
      guide.list = chip.dataset.list;
      openGuide();
    });
  }
  $("guideFind").addEventListener("input", drawGuide);
  $("guideRows").addEventListener("scroll", () => {
    const box = $("guideRows");
    const near = box.scrollTop + box.clientHeight > box.scrollHeight - 400;
    if (near && !guide.asking && guide.page + 1 < guide.pages) askGuide(guide.list, guide.page + 1);
  });
}

// ---- search -------------------------------------------------------------------------------

let queryTimer = null;

function openSearch() {
  show("search");
  const input = $("searchInput");
  input.value = "";
  $("searchRows").replaceChildren();
  input.focus();
}

function onResults(message) {
  const box = $("searchRows");
  const titles = { live: w("channels"), movies: w("movies"), series: w("series") };
  const nodes = [];
  for (const group of message.groups || []) {
    nodes.push(el("div", "group", titles[group.kind] || group.kind));
    for (const item of group.items) {
      const row = el("button", "row");
      row.type = "button";
      row.append(group.kind === "live" ? logoTile(item.title) : logoTile(item.title, "poster"));
      const main = el("div", "row-main");
      const top = el("div", "row-top");
      top.append(el("span", "title", item.title));
      main.append(top);
      if (item.sub) main.append(el("div", "row-next", item.sub));
      row.append(main);
      tapRow(row, item.id);
      nodes.push(row);
    }
  }
  if (!nodes.length && message.q) nodes.push(el("div", "empty", w("no_results")));
  box.replaceChildren(...nodes);
}

function bindSearch() {
  $("searchButton").addEventListener("click", openSearch);
  const input = $("searchInput");
  input.addEventListener("input", () => {
    clearTimeout(queryTimer);
    queryTimer = setTimeout(() => send({ t: "query", s: input.value }), QUERY_DEBOUNCE_MS);
  });
  input.addEventListener("keydown", (event) => {
    if (event.key === "Enter") {
      event.preventDefault();
      clearTimeout(queryTimer);
      send({ t: "query", s: input.value });
      tap("ok");
    }
  });
  $("searchDone").addEventListener("click", () => {
    input.blur();
    show("remote");
  });
}

// ---- the Library --------------------------------------------------------------------------

function openLibrary() {
  $("libraryRows").replaceChildren(el("div", "empty", w("loading")));
  send({ t: "get", what: "library" });
}

function libraryRow(item, withProgress) {
  const row = el("button", "row");
  row.type = "button";
  row.append(logoTile(item.title, "poster"));
  const main = el("div", "row-main");
  const top = el("div", "row-top");
  top.append(el("span", "title", item.title));
  main.append(top);
  if (item.sub) main.append(el("div", "row-next", item.sub));
  if (withProgress) {
    const bar = el("div", "prog");
    const fill = el("i");
    fill.style.width = `${((item.progress || 0) * 100).toFixed(1)}%`;
    bar.append(fill);
    main.append(bar);
  }
  row.append(main);
  tapRow(row, item.id);
  return row;
}

function onLibrary(message) {
  const nodes = [];
  if (message.continue && message.continue.length) {
    nodes.push(el("div", "group", w("continue_watching")));
    for (const item of message.continue) nodes.push(libraryRow(item, true));
  }
  if (message.mylist && message.mylist.length) {
    nodes.push(el("div", "group", w("my_list")));
    for (const item of message.mylist) nodes.push(libraryRow(item, false));
  }
  if (!nodes.length) nodes.push(el("div", "empty", w("nothing_here")));
  $("libraryRows").replaceChildren(...nodes);
}

// ---- the touchpad -------------------------------------------------------------------------

function useTouchpad(on) {
  $("pad").classList.toggle("touch", on);
  $("touchpad").hidden = !on;
  store.setSetting("dpad", on ? "touchpad" : "buttons");
}

function bindTouchpad() {
  const pad = $("touchpad");
  let start = null; // {x, y, t, dir, holding, timer, id}
  const direction = (dx, dy) => (Math.abs(dx) > Math.abs(dy) ? (dx > 0 ? "right" : "left") : dy > 0 ? "down" : "up");
  pad.addEventListener("pointerdown", (event) => {
    event.preventDefault();
    if (pad.setPointerCapture) pad.setPointerCapture(event.pointerId);
    start = { x: event.clientX, y: event.clientY, t: performance.now(), dir: null, holding: false, timer: null, far: 0 };
    pad.classList.add("pressed");
  });
  pad.addEventListener("pointermove", (event) => {
    if (!start) return;
    const dx = event.clientX - start.x;
    const dy = event.clientY - start.y;
    start.far = Math.max(start.far, Math.hypot(dx, dy));
    if (start.dir || start.far < FLICK_PX) return;
    start.dir = direction(dx, dy);
    const begin = () => {
      if (start && !start.holding) {
        start.holding = true;
        press(start.dir, null);
      }
    };
    const waited = performance.now() - start.t;
    if (waited >= HOLD_MS) begin();
    else start.timer = setTimeout(begin, HOLD_MS - waited);
  });
  const end = () => {
    if (!start) return;
    const done = start;
    start = null;
    pad.classList.remove("pressed");
    clearTimeout(done.timer);
    if (done.holding) release(done.dir);
    else if (done.dir) tap(done.dir); // a flick: one step
    else if (done.far < TAP_PX) tap("ok");
  };
  for (const type of ["pointerup", "pointercancel", "lostpointercapture"]) pad.addEventListener(type, end);
}

// ---- wiring -------------------------------------------------------------------------------

app.handlers.state = onState;
app.handlers.guide = onGuide;
app.handlers.results = onResults;
app.handlers.library = onLibrary;
app.handlers["tab:guide"] = openGuide;
app.handlers["tab:library"] = openLibrary;
app.menuExtras.push(() => {
  const on = store.setting("dpad", "buttons") === "touchpad";
  const item = el("button", "", on ? w("use_buttons") : w("use_touchpad"));
  item.type = "button";
  item.addEventListener("click", () => {
    useTouchpad(!on);
    document.getElementById("veil").hidden = true;
    for (const sheet of document.querySelectorAll(".sheet")) sheet.hidden = true;
  });
  return item;
});

export function install() {
  bindGuide();
  bindSearch();
  bindTouchpad();
  useTouchpad(store.setting("dpad", "buttons") === "touchpad");
  setInterval(() => {
    if (current && !document.hidden) drawNow();
  }, 1000);
}
