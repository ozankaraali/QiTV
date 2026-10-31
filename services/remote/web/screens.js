// The second screen: what QiTV says is on, the guide, the search's results and the Library,
// drawn from QiTV's snapshots (ui/remote_snapshots.py). Taps send back the opaque handles QiTV
// gave; nothing here knows a stream's address. Also the d-pad's touchpad mode.
import app, { banner, closeSheets, menuItem, nameField, openSheet, press, release, send, show, t, tap } from "./app.js";
import * as store from "./store.js";

const $ = (id) => document.getElementById(id);
const COLORS = ["#2f5d73", "#2f6b52", "#3d4f7a", "#7a5a2f", "#5a3d6b", "#6b4a3a"];
const FLICK_PX = 24;
const TAP_PX = 10;
const HOLD_MS = 250;
const QUERY_DEBOUNCE_MS = 150;

const words = {
  nothing_playing: "Nothing playing",
  watching: "Watching",
  watch: "Watch",
  on_now: "Now",
  from_start: "Watch from the start",
  no_programmes: "No programme guide for this channel",
  guide: "Guide",
  library: "Library",
  pip: "Picture in picture",
  close_pip: "Close picture in picture",
  groups: "Groups",
  genres: "Genres",
  guide_find: "Find a channel",
  find_group: "Find a group or channel",
  providers: "Providers",
  find_genre: "Find a genre or title",
  find_title: "Find a title",
  load_failed: "Couldn't load this list",
  try_again: "Try again",
  channels: "Channels",
  movies: "Movies",
  series: "Series",
  no_results: "Nothing found",
  nothing_here: "Nothing here yet",
  loading: "Loading…",
  use_touchpad: "Use a touchpad",
  use_buttons: "Use buttons",
  watch_here: "Watch on this phone",
  opening_here: "Opening on this phone…",
  show_on_tv: "Show on TV",
  phone: "Phone",
  pick_here: "Pick a channel below to watch it here",
  now_on_tv: "Now playing on the TV",
  open_player: "Open in the phone's player",
  film_format: "This film doesn't play on a phone. Tap TV to watch it there.",
  film_sound: "This film's sound doesn't play on a phone. Tap TV to watch it there.",
  film_relay: "The relay can't carry films. To watch this one on the phone, be at home on QiTV's network (not a guest network or a VPN).",
  film_provider: "The film didn't open. Tap TV to watch it there.",
  rec_format: "This recording doesn't play on a phone. Tap TV to watch it there.",
  rec_sound: "This recording's sound doesn't play on a phone. Tap TV to watch it there.",
  rec_relay: "The relay can't carry recordings. To watch this one on the phone, be at home on QiTV's network (not a guest network or a VPN).",
  rec_open: "The recording didn't open. Tap TV to watch it there.",
  asked_elsewhere: "QiTV asked about the recording on another screen: nothing opened on this phone",
  back_to: "Resume · {time}",
  rec: "REC",
  go_back: "Go back to {channel}",
  stop_recording: "Stop recording",
  recording_kept: "The recording goes on: nothing opened on this phone",
  record: "Record",
  go_live: "Go live",
  cast_to: "Cast to a TV",
  stop_casting: "Stop casting",
  looking_devices: "Looking for devices…",
  refresh: "Refresh",
  refreshing: "Refreshing films and series…",
  no_casting: "Casting isn't available on this computer",
  record_series: "Series",
  cancel_recording: "Cancel recording",
  next_day: "Ends the next day.",
  passed: "That time has passed.",
  no_day: "Choose at least one day.",
  no_length: "From and To are the same.",
  timer_failed: "Couldn't schedule that recording",
  tv_clock: "Times are QiTV's clock: {time} there now.",
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

function pressable(node) {
  node.addEventListener("pointerdown", () => node.classList.add("pressed"));
  for (const type of ["pointerup", "pointercancel", "pointerleave"]) node.addEventListener(type, () => node.classList.remove("pressed"));
}

// A tap opens `handle` on the TV; watching on this phone, one that `plays` (a channel, a film,
// an episode) plays here instead, `name` (and `num`) above it meanwhile.
function tapRow(row, handle, { plays = false, num = "", name = "" } = {}) {
  pressable(row);
  row.addEventListener("click", () => {
    if (navigator.vibrate) navigator.vibrate(8);
    if (here && plays) watchHandle(handle, num, name);
    else send({ t: "open", id: handle });
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
  for (const [key, on] of [["record", message.recording], ["multiview", message.multiview]]) {
    for (const button of document.querySelectorAll(`[data-key=${key}]`)) button.classList.toggle("on", !!on); // lit while the TV does it
  }
  for (const [key, word] of [["record", "record"], ["live", "go_live"]]) {
    for (const button of document.querySelectorAll(`.cluster [data-key=${key}]`)) button.setAttribute("aria-label", w(word));
  }
  const pip = document.querySelector("#moreSheet [data-key=pip]");
  if (pip) {
    pip.classList.toggle("on", !!message.pip); // a small picture shows: the button closes it
    pip.querySelector("span").textContent = w(message.pip ? "close_pip" : "pip");
  }
  const back = $("backTo"); // 2.6: the TV offers where the channel come back to was left
  if (back) {
    back.hidden = !message.back;
    if (message.back) $("backToText").textContent = w("back_to").replace("{time}", message.back);
  }
  recordings = Array.isArray(message.rec) ? message.rec : [];
  drawRec();
  followTv(now && now.kind === "live" ? liveKey(now.num, now.name) : "");
}

// ---- 2.6: what records in the background, and QiTV's question --------------------------

let recordings = []; // [{id, name, since}] from the TV's state
let askId = null; // the number of the question shown, which the answer carries back

function recClock(seconds) {
  const s = Math.max(0, Math.floor(seconds));
  const h = Math.floor(s / 3600);
  const m = String(Math.floor((s % 3600) / 60)).padStart(2, "0");
  const rest = String(s % 60).padStart(2, "0");
  return h ? `${h}:${m}:${rest}` : `${m}:${rest}`;
}

function drawRec() {
  const line = $("recLine");
  if (!line) return;
  line.hidden = recordings.length === 0;
  if (!recordings.length) return;
  $("recName").textContent = recordings.map((r) => r.name).join(", ");
  const one = recordings.length === 1 && Number(recordings[0].since) > 0;
  $("recTime").textContent = one ? recClock(Date.now() / 1000 - Number(recordings[0].since)) : "";
}

function openRecMenu() {
  const list = $("menuList");
  list.replaceChildren();
  $("menuTitle").textContent = w("rec");
  for (const r of recordings) {
    list.append(menuItem(w("go_back").replace("{channel}", r.name), () => send({ t: "rec", id: r.id, do: "back" })));
    list.append(menuItem(w("stop_recording"), () => send({ t: "rec", id: r.id, do: "stop" }), "danger"));
  }
  openSheet("menuSheet");
}

function onAsk(message) {
  if (typeof message.id !== "number") return;
  closeSheets();
  askId = message.id;
  $("askText").textContent = message.text || "";
  $("askYes").textContent = message.yes || "";
  $("askNo").textContent = message.no || "";
  openSheet("askSheet");
  if (navigator.vibrate) navigator.vibrate(12);
}

function answer(yes) {
  if (askId === null) return;
  send({ t: "answer", id: askId, yes });
  askId = null;
  closeSheets();
}

function onUnask(message) {
  if (message.id !== askId) return;
  askId = null;
  closeSheets();
}

function bindRec() {
  $("recLine").addEventListener("click", openRecMenu);
  $("askYes").addEventListener("click", () => answer(true));
  $("askNo").addEventListener("click", () => answer(false));
  $("veil").addEventListener("click", () => answer(false)); // tapped away: nothing stops
}

function drawNow() {
  const now = current && current.now;
  const name = $("nowName");
  const live = $("nowLive");
  const toLive = document.querySelector(".cluster [data-key=live]");
  toLive.classList.remove("behind"); // lit only while a live channel plays behind its live point
  if (!now) {
    name.textContent = w("nothing_playing");
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
    toLive.classList.toggle("behind", behind > 0);
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

// ---- channel lists: the Guide's and the Remote tab's ---------------------------------------
// The channel list as the TV has it: its groups (★ Favorites, ⟲ Recent, the provider's), then a
// group's channels a page at a time. It opens where the TV's list is, on the channel playing. The
// find box searches the level shown, the whole of it, on QiTV. In the Guide a row opens the
// channel's card (what's on, what follows, Watch) and the ▶ at its end tunes at once; the Remote
// tab's list shows no programmes, and a row tunes.

// `watching`: the channel the TV shows, as "num|name".
let watching = "";

function liveKey(num, name) {
  return `${num || ""}|${name || ""}`;
}

// The Watching mark on the row of the channel the TV shows, on every page loaded.
function markWatching(row, on) {
  row.classList.toggle("playing", on);
  const badge = row.querySelector(".row-top em");
  if (on && !badge) row.querySelector(".row-top").append(el("em", "", w("watching")));
  else if (!on && badge) badge.remove();
}

function guideRow(row) {
  const item = el("div", `row split${row.playing ? " playing" : ""}`);
  item.dataset.live = liveKey(row.num, row.name);
  const hit = el("button", "row-tap");
  hit.type = "button";
  hit.append(logoTile(row.name));
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
  hit.append(main);
  pressable(hit);
  hit.addEventListener("click", () => openTimetable(row.id, row.num, row.name));
  const side = el("button", "side");
  side.type = "button";
  side.setAttribute("aria-label", w("watch"));
  side.innerHTML = '<svg><use href="#i-play"/></svg>';
  tapRow(side, row.id, { plays: true, num: row.num, name: row.name }); // tunes the TV, or plays here
  item.append(hit, side);
  return item;
}

function zapRow(row) {
  const item = el("button", `row${row.playing ? " playing" : ""}`);
  item.type = "button";
  item.dataset.live = liveKey(row.num, row.name);
  item.append(logoTile(row.name));
  const main = el("div", "row-main");
  const top = el("div", "row-top");
  if (row.num) top.append(el("span", "num", row.num));
  top.append(el("span", "title", row.name));
  if (row.playing) top.append(el("em", "", w("watching")));
  main.append(top);
  item.append(main);
  tapRow(item, row.id, { plays: true, num: row.num, name: row.name }); // the TV, or here
  return item;
}

// `ids`: its elements; `asks`: what it asks QiTV for, the groups and a group's channels (QiTV's
// answers are of the same names); `following`: opened where the TV is, it follows the TV's
// channel until the phone picks a group or finds.
// With two providers or more, the providers are a level above the groups (`pick`): All
// providers, then each; the one chosen is the phone's lists' (`source`), never the TV's.
function channelList({ ids, asks, top, row, shown }) {
  const list = newList({ level: "channels", key: "", title: "", total: null, following: false, pick: false, source: "", switchTo: "" });
  const box = () => $(ids.rows);
  let findTimer = null;
  const showLevel = () => {
    const level = list.level;
    const up = $(ids.up);
    up.hidden = level === "sources" || (level === "groups" && !list.pick);
    up.querySelector("span").textContent = w(level === "channels" ? "groups" : "providers");
    let title = list.title || w(top);
    if (level === "sources") title = w("providers");
    else if (level === "groups") title = (list.pick && list.source) || w(top);
    setTitle($(ids.title), title, level === "channels" ? list.total : null);
    $(ids.find).hidden = level === "sources";
    $(ids.find).placeholder = level === "groups" ? w("find_group") : w("guide_find");
    nameField($(ids.find));
  };
  const askPage = (page) => {
    if (list.level === "sources") {
      ask(list, { what: asks[2] });
    } else if (list.level === "groups") {
      const message = { what: asks[0], page, find: list.find };
      if (list.switchTo) message.source = list.switchTo; // the provider chosen, once
      list.switchTo = "";
      ask(list, message);
    } else {
      ask(list, { what: asks[1], list: list.key, page, find: list.find });
    }
  };
  const openAt = (level, key = "", title = "", source = "") => {
    const following = level === "channels" && !key; // where the TV is
    Object.assign(list, { level, key, title, total: null, find: "", following, switchTo: source });
    $(ids.find).value = "";
    showLevel();
    startList(box(), list);
    askPage(0);
  };
  const onSources = (message) => {
    if (list.level !== "sources" || !answers(list, message)) return;
    const rows = message.rows.map((source) => {
      const item = branchRow(source, () => openAt("groups", "", "", source.id));
      item.classList.toggle("on", !!source.on);
      return item;
    });
    placePage(box(), list, { ...message, page: 0, pages: 1, find: "" }, rows, () => openAt("sources"));
  };
  const onGroups = (message) => {
    if (list.level !== "groups" || message.find !== list.find || !answers(list, message)) return;
    Object.assign(list, { pick: !!message.pick, source: message.source || "" });
    showLevel();
    const rows = message.rows.map((group) => branchRow(group, () => openAt("channels", group.id, group.title)));
    const found = message.channels || []; // what the find found among every channel, below its groups
    if (found.length && (message.page === 0 || rows.length)) rows.push(el("div", "group", w("channels"))); // where they begin
    rows.push(...found.map(row));
    placePage(box(), list, message, rows, () => openAt("groups"));
  };
  const onChannels = (message) => {
    if (list.level !== "channels" || message.find !== list.find || !answers(list, message)) return;
    if (!("n" in message) && message.list !== list.key) return; // QiTV's own resend: of this group
    const opening = list.opening;
    Object.assign(list, { key: message.list, title: message.title, total: message.count });
    showLevel();
    const retry = () => openAt("channels", list.key, list.title);
    placePage(box(), list, message, message.rows.map(row), retry);
    const on = opening && box().querySelector(".playing");
    if (on) on.scrollIntoView({ block: "center" }); // the TV's channel, in view
  };
  // The TV changed channel (on the computer, or from here): Watching moves; opened where the TV
  // is, the list follows it, to its row when loaded, else to where the TV's list is now.
  const follow = (live) => {
    let found = null;
    for (const item of box().querySelectorAll(".row[data-live]")) {
      const on = !!live && item.dataset.live === live;
      markWatching(item, on);
      if (on) found = item;
    }
    if (!live || !list.following || !shown() || list.opening) return;
    if (found) found.scrollIntoView({ block: "nearest" });
    else openAt("channels");
  };
  const bind = () => {
    $(ids.up).addEventListener("click", () => openAt(list.level === "channels" ? "groups" : "sources"));
    const find = $(ids.find);
    find.addEventListener("input", () => {
      clearTimeout(findTimer);
      findTimer = setTimeout(() => {
        list.find = find.value.trim();
        list.following = false; // the phone looks for something: the TV's zaps don't move it
        startList(box(), list);
        askPage(0);
      }, QUERY_DEBOUNCE_MS);
    });
    pageOnScroll(box(), list, askPage);
  };
  // A reconnect is a new session: what shows comes again (its groups' keys hold).
  const reopen = () => openAt(list.level, list.level === "channels" ? list.key : "", list.title);
  app.handlers[asks[0]] = onGroups;
  app.handlers[asks[1]] = onChannels;
  app.handlers[asks[2]] = onSources;
  return { list, openAt, reopen, follow, bind };
}

const guide = channelList({
  ids: { rows: "guideRows", up: "guideUp", title: "guideTitle", find: "guideFind" },
  asks: ["groups", "guide", "sources"],
  top: "guide",
  row: guideRow,
  shown: () => !document.querySelector("[data-screen=guide]").hidden,
});
const zap = channelList({
  ids: { rows: "zapRows", up: "zapUp", title: "zapTitle", find: "zapFind" },
  asks: ["chgroups", "channels", "chsources"],
  top: "channels",
  row: zapRow,
  shown: () => zapping() && !document.querySelector("[data-screen=remote]").hidden,
});

function followTv(live) {
  if (live === watching) return;
  watching = live;
  guide.follow(live);
  zap.follow(live);
}

function openGuide() {
  guide.openAt("channels"); // where the TV's channel list is
}

let timetableFor = "";

function bindGuide() {
  guide.bind();
  zap.bind();
  $("timetableBack").addEventListener("click", () => show("guide"));
  $("timetableWatch").addEventListener("click", () => {
    if (navigator.vibrate) navigator.vibrate(8);
    if (!timetableFor) return;
    if (here) watchHandle(timetableFor, "", $("timetableName").textContent);
    else send({ t: "open", id: timetableFor });
  });
}

// ---- a channel's card: its timetable ------------------------------------------------------

function timetableTitle(num, name) {
  const title = $("timetableName");
  title.replaceChildren();
  if (num) title.append(el("span", "num", num));
  title.append(name || "");
}

let timerOffered = false; // the timetable shown says QiTV records (with its time-shift helper)
let tvOffset = null; // QiTV's computer's clock, minutes from UTC (its times are that clock's)

function openTimetable(id, num, name) {
  timetableFor = id;
  timerOffered = false;
  closeTimer();
  $("timerNote").textContent = "";
  timetableTitle(num, name);
  $("timetableRows").replaceChildren(el("div", "empty", w("loading")));
  show("timetable");
  send({ t: "get", what: "timetable", id });
}

function slotRow(slot, nowS) {
  const past = slot.stop <= nowS;
  const row = el(slot.id ? "button" : "div", `row slot${slot.on ? " playing" : ""}${past && !slot.id ? " still" : ""}`);
  if (slot.id) row.type = "button";
  row.append(el("div", "slot-time", clock(slot.start)));
  const main = el("div", "row-main");
  const top = el("div", "row-top");
  top.append(el("span", "title", slot.title));
  if (slot.on) top.append(el("em", "", w("on_now")));
  main.append(top);
  if (slot.on) {
    const bar = el("div", "prog");
    const fill = el("i");
    fill.style.width = `${(share(slot, nowS) * 100).toFixed(1)}%`;
    bar.append(fill);
    main.append(bar);
  } else if (past && slot.id) {
    main.append(el("div", "row-next", w("from_start")));
  }
  if (slot.about) main.append(el("div", "about", slot.about));
  if (slot.sched) main.append(schedChips(slot)); // 2.8: record it at its time, or cancel
  row.append(main);
  if (slot.id) tapRow(row, slot.id);
  return row;
}

// A programme still to come (or on now): QiTV records it at its time (once, or every airing:
// series), or cancels a scheduled one. The chips' taps are their own, not the row's.
function schedChips(slot) {
  const chips = el("div", "sched");
  const chip = (text, what, className) => {
    const button = el("button", `chip ${className}`, text);
    button.type = "button";
    for (const type of ["pointerdown", "pointerup", "click"]) {
      button.addEventListener(type, (event) => event.stopPropagation());
    }
    button.addEventListener("click", () => send({ t: "sched", id: slot.sched, do: what }));
    return button;
  };
  if (slot.rec) {
    chips.append(chip(`● ${w("cancel_recording")}`, "cancel", "on"));
  } else {
    chips.append(chip(`● ${w("record")}`, "once", "rec"), chip(w("record_series"), "series", ""));
  }
  return chips;
}

// ---- Record at a time (2.8): the timetable's channel from one time to another, once on a day
// or on chosen weekdays, guide or not. Times are the TV's clock's; an end before the start is
// the next day's. QiTV says what it scheduled (or why not) on the line by the button.

const pad2 = (n) => String(n).padStart(2, "0");
const isoDay = (date) => `${date.getFullYear()}-${pad2(date.getMonth() + 1)}-${pad2(date.getDate())}`;
const hhmm = (date) => `${pad2(date.getHours())}:${pad2(date.getMinutes())}`;

// Now on QiTV's computer's clock, as a Date whose fields read that clock (the phone's own
// when the timetable said nothing).
function tvNow() {
  const now = new Date();
  if (tvOffset === null) return now;
  return new Date(now.getTime() + (tvOffset + now.getTimezoneOffset()) * 60000);
}

function minutesOf(value) {
  const [hours, minutes] = (value || "").split(":").map(Number);
  return hours * 60 + minutes; // NaN while empty
}

function bindTimer() {
  const days = $("timerDays");
  for (let day = 0; day < 7; day += 1) {
    // 2024-01-01 was a Monday: the phone's own short day names, Monday first
    const name = new Date(2024, 0, 1 + day).toLocaleDateString([], { weekday: "short" });
    const button = el("button", "chip", name);
    button.type = "button";
    button.dataset.day = String(day);
    button.addEventListener("click", () => {
      button.classList.toggle("on");
      checkTimer();
    });
    days.append(button);
  }
  $("timerOpen").addEventListener("click", openTimer);
  $("timerCancel").addEventListener("click", closeTimer);
  for (const id of ["timerRepeat", "timerDay", "timerFrom", "timerTo"]) {
    $(id).addEventListener("input", checkTimer);
    $(id).addEventListener("change", checkTimer);
  }
  $("timerForm").addEventListener("submit", (event) => {
    event.preventDefault();
    sendTimer();
  });
}

function openTimer() {
  const now = tvNow();
  const start = new Date(now.getTime());
  start.setSeconds(0, 0);
  start.setMinutes(start.getMinutes() + ((5 - (start.getMinutes() % 5)) % 5)); // the next five
  const stop = new Date(start.getTime() + 3600 * 1000);
  $("timerName").value = "";
  $("timerRepeat").value = "once";
  $("timerDay").min = isoDay(now);
  $("timerDay").value = isoDay(start);
  $("timerFrom").value = hhmm(start);
  $("timerTo").value = hhmm(stop);
  for (const button of $("timerDays").children) button.classList.remove("on");
  $("timerNote").textContent = "";
  const clock = $("timerClock");
  clock.hidden = tvOffset === null || tvOffset === -new Date().getTimezoneOffset();
  clock.textContent = clock.hidden ? "" : w("tv_clock").replace("{time}", hhmm(now));
  $("timerBar").hidden = true; // the form in its place
  $("timerForm").hidden = false;
  checkTimer();
}

function closeTimer() {
  $("timerForm").hidden = true;
  $("timerBar").hidden = !timerOffered;
}

function timerDays() {
  const kind = $("timerRepeat").value;
  if (kind === "daily") return [0, 1, 2, 3, 4, 5, 6];
  if (kind === "weekdays") return [0, 1, 2, 3, 4];
  if (kind !== "days") return [];
  return [...$("timerDays").children].filter((b) => b.classList.contains("on")).map((b) => Number(b.dataset.day));
}

// The message the form asks for, or none; `why`: the hint's word.
function timerChoice() {
  const begin = minutesOf($("timerFrom").value);
  const end = minutesOf($("timerTo").value);
  if (Number.isNaN(begin) || Number.isNaN(end)) return { why: "" };
  const minutes = (((end - begin) % 1440) + 1440) % 1440;
  if (!minutes) return { why: "no_length" };
  const once = $("timerRepeat").value === "once";
  const days = timerDays();
  if (!once && !days.length) return { why: "no_day" };
  const day = once ? $("timerDay").value : "";
  if (once) {
    const [year, month, date] = day.split("-").map(Number);
    const start = new Date(year, month - 1, date, Math.floor(begin / 60), begin % 60);
    if (Number.isNaN(start.getTime())) return { why: "" };
    if (start.getTime() + minutes * 60000 <= tvNow().getTime()) return { why: "passed" };
  }
  const title = $("timerName").value.trim();
  const message = { t: "timer", id: timetableFor, title, begin, minutes, days, day };
  return { message, why: end <= begin ? "next_day" : "" };
}

function checkTimer() {
  const once = $("timerRepeat").value === "once";
  $("timerDays").hidden = $("timerRepeat").value !== "days";
  $("timerDayLine").hidden = !once;
  const choice = timerChoice();
  $("timerHint").textContent = choice.why ? w(choice.why) : "";
  $("timerSchedule").disabled = !choice.message;
}

function sendTimer() {
  const choice = timerChoice();
  if (!choice.message || !timetableFor) return;
  if (navigator.vibrate) navigator.vibrate(8);
  send(choice.message);
  closeTimer();
}

function onTimer(message) {
  if (message.id !== timetableFor) return;
  $("timerNote").textContent = message.text || w(message.failed === "passed" ? "passed" : "timer_failed");
}

// ---- casting (2.8): QiTV sends what plays to a TV it finds (a Chromecast) ------------------

// The menu sheet shows other menus too: a list that arrives redraws only the cast list in view.
function onCast(message) {
  if ($("menuSheet").hidden || $("menuTitle").textContent !== w("cast_to")) return;
  drawCast(message);
}

function drawCast(message) {
  const list = $("menuList");
  list.replaceChildren();
  $("menuTitle").textContent = w("cast_to");
  for (const device of message.devices || []) {
    list.append(menuItem(device.name, () => send({ t: "cast", to: device.name }), device.on ? "current" : ""));
  }
  if (!(message.devices || []).length) {
    const note = el("p", "step", w(message.off ? "no_casting" : "looking_devices"));
    list.append(note);
  }
  if (message.on) list.append(menuItem(w("stop_casting"), () => send({ t: "cast", to: "" }), "danger"));
}

function bindCast() {
  const button = $("castTo");
  pressable(button);
  button.addEventListener("click", () => {
    closeSheets();
    drawCast({ devices: [], on: "" });
    openSheet("menuSheet");
    send({ t: "get", what: "cast" });
  });
}

function onTimetable(message) {
  if (message.id !== timetableFor) return;
  timetableTitle(message.num, message.name);
  timerOffered = Boolean(message.timer);
  tvOffset = Number.isInteger(message.tz) ? message.tz : null;
  if ($("timerForm").hidden) $("timerBar").hidden = !timerOffered;
  const nowS = Date.now() / 1000;
  const rows = [];
  let day = new Date().toDateString();
  for (const slot of message.rows) {
    const date = new Date(slot.start * 1000);
    if (date.toDateString() !== day) {
      day = date.toDateString(); // past midnight (or before it): the day it is
      rows.push(el("div", "group", date.toLocaleDateString([], { weekday: "long", day: "numeric", month: "short" })));
    }
    rows.push(slotRow(slot, nowS));
  }
  const box = $("timetableRows");
  const empty = el("div", "empty", w(message.loading ? "loading" : "no_programmes"));
  box.replaceChildren(...(rows.length ? rows : [empty]));
  const on = box.querySelector(".slot.playing");
  box.scrollTop = on ? on.offsetTop - box.offsetTop : 0; // what's on at the top, earlier above
}

// ---- lists a page at a time ---------------------------------------------------------------

function newList(extra) {
  return { find: "", lo: 0, hi: 0, pages: 1, asking: false, askedAt: 0, ask: 0, opening: false, ...extra };
}

// Each ask is numbered and its answer carries the number back: a list takes only the answer to
// its latest ask (an earlier tap's comes too late to show), and QiTV's own resends (a list or a
// programme guide that arrived; no number) only while it isn't waiting for one.
let asks = 0;

function ask(list, message) {
  asks += 1;
  Object.assign(list, { ask: asks, asking: true, askedAt: Date.now() });
  send({ t: "get", ...message, n: asks });
}

function answers(list, message) {
  return "n" in message ? message.n === list.ask : !list.opening;
}

function setTitle(h1, text, total) {
  h1.replaceChildren(text || "");
  if (typeof total === "number") h1.append(el("small", "", total.toLocaleString()));
}

function startList(box, list) {
  Object.assign(list, { opening: true, lo: 0, hi: 0, pages: 1 });
  box.replaceChildren(el("div", "empty", w("loading")));
}

// A group or a genre: a tap goes into it. A provider's heading only names whose follow.
function branchRow(entry, open) {
  if (entry.header) return el("div", "group", entry.title);
  const row = el("button", "row branch");
  row.type = "button";
  row.append(el("span", "title", entry.title));
  if (typeof entry.count === "number") row.append(el("span", "count", entry.count.toLocaleString()));
  row.insertAdjacentHTML("beforeend", '<svg><use href="#i-right"/></svg>');
  pressable(row);
  row.addEventListener("click", open);
  return row;
}

function failedNotice(retry) {
  const box = el("div", "empty", w("load_failed"));
  const again = el("button", "again", w("try_again"));
  again.type = "button";
  again.addEventListener("click", retry);
  box.append(el("br"), again);
  return box;
}

// A page into `box`. The view's first replaces what showed; one QiTV sends again (a list that
// arrived, a programme guide that loaded) changes its rows in place; the next go below, the
// earlier above without moving what shows.
function placePage(box, list, message, rows, retry) {
  list.asking = false;
  list.pages = message.pages || 1;
  const page = message.page;
  for (const row of rows) row.dataset.page = page;
  const old = list.opening ? [] : [...box.querySelectorAll(`[data-page="${page}"]`)];
  if (list.opening || (!old.length && page >= list.lo && page <= list.hi)) {
    list.opening = false;
    list.lo = list.hi = page;
    if (rows.length) box.replaceChildren(...rows);
    else if (message.failed) box.replaceChildren(failedNotice(retry));
    else box.replaceChildren(el("div", "empty", w(message.loading ? "loading" : message.find ? "no_results" : "nothing_here")));
    box.scrollTop = 0;
  } else if (old.length) {
    old[0].before(...rows);
    for (const node of old) node.remove();
  } else if (page === list.hi + 1) {
    box.append(...rows);
    list.hi = page;
  } else if (page === list.lo - 1) {
    const before = box.scrollHeight;
    box.prepend(...rows);
    box.scrollTop += box.scrollHeight - before;
    list.lo = page;
  }
}

const UNANSWERED_MS = 5000; // an ask never answered (QiTV went away) no longer holds the list

function pageOnScroll(box, list, more) {
  box.addEventListener("scroll", () => {
    if (list.opening || (list.asking && Date.now() - list.askedAt < UNANSWERED_MS)) return;
    if (box.scrollTop + box.clientHeight > box.scrollHeight - 400 && list.hi + 1 < list.pages) more(list.hi + 1);
    else if (box.scrollTop < 400 && list.lo > 0) more(list.lo - 1);
  });
}

// ---- search -------------------------------------------------------------------------------

let queryTimer = null;

// The search is the phone's own (the desktop's search, the TV's picture as it is) unless Show
// on TV is on: then the TV's search shows it too, as typing on its keyboard would.
let searchOnTv = store.setting("search_tv", "") === "on";

function openSearch() {
  show("search");
  const input = $("searchInput");
  input.value = "";
  $("searchRows").replaceChildren();
  input.focus();
  if (searchOnTv) tap("search"); // the TV's search opens
}

function sendQuery() {
  send({ t: "query", s: $("searchInput").value, tv: searchOnTv });
}

function useSearchOnTv(on) {
  searchOnTv = on;
  const toggle = $("searchTv");
  toggle.classList.toggle("on", on);
  toggle.setAttribute("aria-pressed", String(on));
  store.setSetting("search_tv", on ? "on" : "");
}

function onResults(message) {
  if (typeof message.q === "string" && message.q !== $("searchInput").value) return; // older text
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
      tapRow(row, item.id, { plays: group.kind !== "series", name: item.title });
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
    queryTimer = setTimeout(sendQuery, QUERY_DEBOUNCE_MS);
  });
  input.addEventListener("keydown", (event) => {
    if (event.key === "Enter") {
      event.preventDefault();
      clearTimeout(queryTimer);
      sendQuery();
      if (searchOnTv) tap("ok"); // the TV's highlighted result
      else input.blur(); // the phone's: its results, the keyboard out of the way
    }
  });
  const toggle = $("searchTv");
  pressable(toggle);
  toggle.addEventListener("click", () => {
    useSearchOnTv(!searchOnTv);
    if (searchOnTv) tap("search"); // shown on the TV from now on: its search opens
    if (input.value) sendQuery();
  });
  useSearchOnTv(searchOnTv);
  $("searchDone").addEventListener("click", () => {
    input.blur();
    show("remote");
  });
}

// ---- the Library --------------------------------------------------------------------------
// A section's genres as the TV lists them (Continue, New and My list first), then a genre's
// titles a page at a time; the find box searches the level shown. QiTV browses with the phone's
// own copy of its Library: the TV stays where it is until a title is tapped.

const library = newList({ section: "movies", level: "genres", genre: "", title: "", total: null });
let libraryTimer = null;

function showLibraryLevel() {
  const genres = library.level === "genres";
  $("libraryUp").hidden = genres;
  $("libraryChips").hidden = !genres;
  setTitle($("libraryTitle"), genres ? w("library") : library.title, genres ? null : library.total);
  $("libraryFind").placeholder = genres ? w("find_genre") : w("find_title");
  nameField($("libraryFind"));
}

function openLibraryAt(level, genre = "", title = "") {
  Object.assign(library, { level, genre, title, total: null, find: "" });
  $("libraryFind").value = "";
  showLibraryLevel();
  startList($("libraryRows"), library);
  askLibrary(0);
}

function openLibrary() {
  // the tab: where the phone was
  if (library.level === "titles") openLibraryAt("titles", library.genre, library.title);
  else openLibraryAt("genres");
}

function askLibrary(page) {
  const { section, find } = library;
  if (library.level === "genres") ask(library, { what: "genres", section, page, find });
  else ask(library, { what: "library", section, genre: library.genre, page, find });
}

function libraryRow(item) {
  const row = el("button", "row");
  row.type = "button";
  row.append(logoTile(item.title, "poster"));
  const main = el("div", "row-main");
  const top = el("div", "row-top");
  top.append(el("span", "title", item.title));
  main.append(top);
  if (item.sub) main.append(el("div", "row-next", item.sub));
  if (typeof item.progress === "number") {
    const bar = el("div", "prog");
    const fill = el("i");
    fill.style.width = `${(item.progress * 100).toFixed(1)}%`;
    bar.append(fill);
    main.append(bar);
  }
  row.append(main);
  tapRow(row, item.id, { plays: !!item.plays, name: item.title });
  return row;
}

function onGenres(message) {
  if (library.level !== "genres" || message.section !== library.section || message.find !== library.find) return;
  if (!answers(library, message)) return;
  const rows = message.rows.map((genre) => branchRow(genre, () => openLibraryAt("titles", genre.id, genre.title)));
  const titles = message.titles || []; // what the find found in the whole section, below its genres
  if (titles.length && (message.page === 0 || rows.length)) rows.push(el("div", "group", w(library.section))); // where they begin
  rows.push(...titles.map(libraryRow));
  placePage($("libraryRows"), library, message, rows, () => openLibraryAt("genres"));
}

function onLibrary(message) {
  if (library.level !== "titles" || message.section !== library.section || message.find !== library.find) return;
  if (!answers(library, message)) return;
  if (!("n" in message) && message.genre !== library.genre) return; // QiTV's own resend: of this genre
  Object.assign(library, { genre: message.genre, title: message.title, total: message.count });
  showLibraryLevel();
  const retry = () => openLibraryAt("titles", library.genre, library.title);
  placePage($("libraryRows"), library, message, message.items.map(libraryRow), retry);
}

function bindLibrary() {
  for (const chip of document.querySelectorAll("#libraryChips [data-lib]")) {
    chip.addEventListener("click", () => {
      for (const other of document.querySelectorAll("#libraryChips [data-lib]")) other.classList.toggle("on", other === chip);
      library.section = chip.dataset.lib;
      openLibraryAt("genres");
    });
  }
  $("libraryUp").addEventListener("click", () => openLibraryAt("genres"));
  $("libraryRefresh").addEventListener("click", () => {  // 2.8: as the TV Library's ↻
    send({ t: "refresh" });
    banner(w("refreshing"));
  });
  const find = $("libraryFind");
  find.addEventListener("input", () => {
    clearTimeout(libraryTimer);
    libraryTimer = setTimeout(() => {
      library.find = find.value.trim();
      startList($("libraryRows"), library);
      askLibrary(0);
    }, QUERY_DEBOUNCE_MS);
  });
  pageOnScroll($("libraryRows"), library, askLibrary);
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

// ---- the Remote tab's channel list --------------------------------------------------------
// Channels turns the remote's middle (sections, arrows, Back/Search/More) into the channel list,
// with what's on above it and the volume, play and channel buttons below; tapped again, back.

function zapping() {
  return !$("zap").hidden;
}

function useChannelList(on, open = true) {
  $("zap").hidden = !on;
  document.querySelector("[data-screen=remote]").classList.toggle("zapping", on);
  const button = $("listButton");
  button.classList.toggle("on", on);
  button.setAttribute("aria-pressed", String(on));
  store.setSetting("remote", on ? "channels" : "buttons");
  if (on && open) zap.openAt("channels"); // where the TV's channel list is
}

function bindChannelList() {
  const button = $("listButton");
  pressable(button);
  button.addEventListener("click", () => useChannelList(!zapping()));
  useChannelList(store.setting("remote", "buttons") === "channels", false); // opens on the hello
}

// ---- watching on this phone --------------------------------------------------------------
// Phone, in the channel list's TV | Phone: a player docks above the list and a tap on a channel
// plays it here, QiTV letting go of its own stream (one screen at a time); TV hands it back.
// More's Watch on this phone brings what the TV plays. A secure page (the relay's) can't play a
// plain-http stream inside it: that one opens in the phone's own player instead.

const NOTE_MS = 4000;
let noteTimer = null;
let here = false; // watching on this phone
let hereLink = "";

function note(text) {
  banner(text);
  clearTimeout(noteTimer);
  noteTimer = setTimeout(() => {
    if ($("banner").textContent === text) banner("");
  }, NOTE_MS);
}

function caption(num, name, why = false) {
  $("playerNum").textContent = num || "";
  $("playerName").textContent = name || "";
  $("playerName").parentElement.classList.toggle("why", why); // a reason wraps
}

function stopVideo() {
  const video = $("video");
  video.pause();
  video.removeAttribute("src");
  video.load();
  hereLink = "";
  $("playerOut").hidden = true;
}

// In a tap: a phone plays sound only from one, and once its player started from a tap it may
// start again later (the next channel, its link arriving after the tap).
function unlock() {
  const video = $("video");
  if (hereLink || video.dataset.unlocked) return;
  video.dataset.unlocked = "1";
  video.play().catch(() => {});
}

function useHere(on) {
  here = on;
  if (on && !zapping()) useChannelList(true);
  document.querySelector("[data-screen=remote]").classList.toggle("on-phone", on);
  $("player").hidden = !on;
  for (const button of document.querySelectorAll(".where [data-where]")) {
    button.classList.toggle("on", (button.dataset.where === "phone") === on);
  }
  if (!on) stopVideo();
}

// How far this phone got in a film or an episode (a live stream has no end): QiTV keeps it as
// the TV keeps its own, so the TV, or the Library's Continue, goes on from there.
const POSITION_MS = 15000;
let positionAt = 0;
let startAt = 0; // where a title goes on (s), until its length is known and it got there
let playing = -1; // the number of QiTV's "play" this one came with: each report says whose it is

function reportPosition(now = false) {
  const video = $("video");
  if (!hereLink || startAt > 0 || !Number.isFinite(video.duration) || video.duration <= 0) return;
  if (!now && Date.now() - positionAt < POSITION_MS) return;
  positionAt = Date.now();
  const report = { t: "pos", ms: Math.round(video.currentTime * 1000), len: Math.round(video.duration * 1000) };
  send(playing >= 0 ? { ...report, p: playing } : report);
}

function watchHandle(handle, num, name) {
  reportPosition(true); // what played here before, kept before it changes
  unlock();
  caption(num, name);
  send({ t: "watch", id: handle }); // plays here
}

function startHere() {
  if (navigator.vibrate) navigator.vibrate(8);
  useHere(true);
  unlock();
  caption("", w("opening_here"));
  send({ t: "watch" }); // what the TV plays, if anything
}

// Why a film (or a recording) doesn't play here (QiTV's films.py, recorded.py and
// remote_controller.py say).
const FILM_WHY = {
  format: "film_format",
  sound: "film_sound",
  relay: "film_relay",
  provider: "film_provider",
  recording: "recording_kept",
  rec_format: "rec_format",
  rec_sound: "rec_sound",
  rec_relay: "rec_relay",
  rec_open: "rec_open",
  elsewhere: "asked_elsewhere", // its question gave way to a newer one: not "Opening…" for ever
};

function onPlay(message) {
  const url = typeof message.url === "string" ? message.url : "";
  if (!/^https?:\/\//i.test(url)) {
    const why = FILM_WHY[message.why];
    if (here && why) {
      stopVideo();
      caption("", w(why), true);
    } else if (here && !hereLink) caption("", w("pick_here"));
    return;
  }
  if (!here) useHere(true);
  caption(message.num, message.name);
  if (location.protocol === "https:" && url.startsWith("http:")) {
    location.assign(url); // the phone's own player
    return;
  }
  hereLink = url;
  playing = Number.isInteger(message.p) ? message.p : -1;
  $("playerOut").hidden = true;
  startAt = Number(message.start) > 0 ? Number(message.start) : 0; // where it stopped
  positionAt = Date.now(); // the first report in a while, not at 0:00
  const video = $("video");
  video.src = url;
  video.play().catch(() => {}); // the first may wait for a tap on its ▶
}

function onStop() {
  if (!here) return;
  useHere(false);
  note(w("now_on_tv"));
}

function bindHere() {
  for (const button of document.querySelectorAll(".where [data-where]")) {
    button.addEventListener("click", () => {
      const phone = button.dataset.where === "phone";
      if (phone === here) return;
      if (phone) {
        startHere();
      } else {
        reportPosition(true); // the TV goes on from here
        send({ t: "totv" });
        useHere(false);
      }
    });
  }
  const watch = $("watchHere");
  pressable(watch);
  watch.addEventListener("click", () => {
    closeSheets();
    if (!here) startHere();
  });
  const video = $("video");
  video.addEventListener("loadedmetadata", () => {
    if (startAt > 0 && Number.isFinite(video.duration) && startAt < video.duration) video.currentTime = startAt;
    startAt = 0;
  });
  video.addEventListener("timeupdate", () => reportPosition());
  for (const type of ["pause", "ended"]) video.addEventListener(type, () => reportPosition(true));
  document.addEventListener("visibilitychange", () => {
    if (document.visibilityState === "hidden") reportPosition(true);
  });
  window.addEventListener("pagehide", () => reportPosition(true));
  $("video").addEventListener("error", () => {
    if (hereLink) $("playerOut").hidden = false; // a stream this browser can't play
  });
  $("playerOut").addEventListener("click", () => {
    if (hereLink) location.assign(hereLink);
  });
}

// ---- wiring -------------------------------------------------------------------------------

// A reconnect (the phone slept, the network changed) is a new session: the rows shown carry the
// old one's handles, so what shows comes again (its groups' and genres' keys hold).
let hellos = 0;

function onHello() {
  hellos += 1;
  if (zapping()) zap.reopen(); // the Remote tab's list, on the first hello too
  if (hellos === 1) return; // the first: nothing else is open yet
  const shown = (document.querySelector("[data-screen]:not([hidden])") || {}).dataset;
  const screen = shown ? shown.screen : "";
  if (screen === "guide" || screen === "timetable") {
    show("guide");
    guide.reopen();
  } else if (screen === "library") {
    openLibraryAt(library.level, library.genre, library.title);
  }
}

app.handlers.hello = onHello;
app.handlers.state = onState;
app.handlers.timetable = onTimetable;
app.handlers.timer = onTimer;
app.handlers.results = onResults;
app.handlers.genres = onGenres;
app.handlers.library = onLibrary;
app.handlers.play = onPlay;
app.handlers.stop = onStop;
app.handlers.ask = onAsk;
app.handlers.unask = onUnask;
app.handlers.cast = onCast;
app.handlers["tab:guide"] = openGuide;
app.handlers["tab:library"] = openLibrary;
app.menuExtras.push(() => {
  const on = store.setting("dpad", "buttons") === "touchpad";
  const item = el("button", "", on ? w("use_buttons") : w("use_touchpad"));
  item.type = "button";
  item.addEventListener("click", () => {
    useTouchpad(!on);
    closeSheets();
  });
  return item;
});

export function install() {
  bindGuide();
  bindTimer();
  bindLibrary();
  bindSearch();
  bindTouchpad();
  useTouchpad(store.setting("dpad", "buttons") === "touchpad");
  bindChannelList();
  bindHere();
  bindRec();
  bindCast();
  setInterval(() => {
    if (current && !document.hidden) drawNow();
    if (recordings.length && !document.hidden) drawRec();
  }, 1000);
}
