// The two doors. On the relay (https, a stable address you can keep on the home screen) the page
// talks to QiTV through the relay's room, and each time it opens it asks QiTV where it is: on the
// same network it hops to QiTV's own page with a one-use ticket in the address, so every press
// then goes straight over Wi-Fi and the relay is spared. On the home door a ticket becomes this
// address's own key; if QiTV goes out of reach for 5 s the page goes back to the relay, which
// finds QiTV's new address (an IP change) or carries on through the relay.
import app, { banner, connect, menuItem, openSession, send, show, showScan, t } from "./app.js";
import { b64d, b64e, utf8 } from "./conn.js";
import { Keypair } from "./noise.js";
import * as store from "./store.js";

const BOUNCE_MS = 5000;
const SCAN_EVERY_MS = 150;
const decoder = new TextDecoder();
const $ = (id) => document.getElementById(id);

export function install(door) {
  app.door = door;
  if (door === "relay") installRelay();
  else installHome();
}

// ---- the relay door -----------------------------------------------------------------------

function installRelay() {
  const sockets = location.origin.replace(/^http/, "ws");
  app.socketUrl = (tvId, room) => {
    const known = room || (store.getTv(tvId) || {}).room;
    return `${sockets}/room/${known}?role=phone`;
  };
  app.route = (hash) => {
    if (!hash.startsWith("b.")) return false; // "#1." pairs through the room in it (app.js)
    app.noHop = true; // back from a home address that went quiet: stay here this time
    const tvId = hash.slice(2) || store.lastTv();
    if (tvId) connect(tvId);
    else showScan();
    return true;
  };
  app.onhello = () => {
    if (!app.noHop) send({ t: "where" });
  };
  app.handlers.where = (message) => {
    app.where = message;
    const go = message.home && message.ticket && (message.same || app.forceHop);
    app.forceHop = false;
    if (go) hop(message);
  };
  app.onclose = (code, reason) => {
    if (code === 4404) banner(t("tv_offline"));
    else if (code === 4429 && reason === "full") banner(t("full"), "error");
    return false; // retry as usual
  };
  app.menuExtras.push(() =>
    app.conn && app.conn.open
      ? menuItem(t("connect_directly"), () => {
          app.noHop = false;
          app.forceHop = true;
          send({ t: "where" });
        })
      : null,
  );
  setupScanner();
}

function hop(where) {
  const record = store.getTv(app.tvId) || {};
  const relay = b64e(utf8(location.origin));
  location.replace(`${where.home}#h.${where.ticket}.${app.tvId}.${record.room || ""}.${relay}`);
}

// ---- the home door ------------------------------------------------------------------------

let bounceTimer = null;

function installHome() {
  app.route = (hash) => {
    if (!hash.startsWith("h.")) return false;
    const [, tid, secret, tvId, room, relayB64] = hash.split(".");
    let relay = null;
    try {
      relay = decoder.decode(b64d(relayB64 || ""));
    } catch {
      relay = null;
    }
    if (!tid || !secret || !tvId) return false;
    redeem({ tid, secret, tvId, room: room || null, relay });
    return true;
  };
  app.onhello = () => {
    clearTimeout(bounceTimer);
    bounceTimer = null;
  };
  app.onclose = (code, reason, mode) => {
    if (mode === "ticket" && code === 4401) {
      // The ticket was used or too old: the relay hands out a fresh one.
      if (app.pendingRelay) {
        location.replace(`${app.pendingRelay}/#b.${app.tvId}`);
        return true;
      }
      return false;
    }
    armBounce();
    return false; // keep retrying meanwhile
  };
}

function redeem({ tid, secret, tvId, room, relay }) {
  const key = Keypair.generate();
  app.pendingRelay = relay;
  show("connecting");
  openSession({
    tvId,
    room,
    opening: { v: 1, mode: "ticket", ticket: tid },
    pattern: "NKpsk0",
    psk: b64d(secret),
    payload: { pub: b64e(key.pub) },
    onanswer: (answer) => {
      app.pendingRelay = null;
      store.putTv(tvId, {
        name: answer.tv,
        device: answer.device,
        keyId: answer.key,
        priv: b64e(key.priv),
        room,
        relay,
      });
    },
  });
}

function armBounce() {
  if (bounceTimer) return;
  const record = store.getTv(app.tvId);
  if (!record || !record.relay) return;
  bounceTimer = setTimeout(() => {
    bounceTimer = null;
    if (!(app.conn && app.conn.open)) location.replace(`${record.relay}/#b.${app.tvId}`);
  }, BOUNCE_MS);
}

// ---- the camera scanner (secure pages only: the relay door, or a home-screen copy of it) ---

let scanStream = null;
let scanTimer = null;

function setupScanner() {
  const button = $("scanButton");
  const media = navigator.mediaDevices;
  if (!window.isSecureContext || !media || !media.getUserMedia) return;
  button.hidden = false;
  button.addEventListener("click", () => startScanner().catch(() => stopScanner()));
}

function loadScript(src) {
  return new Promise((resolve, reject) => {
    if (window.jsQR) return resolve();
    const script = document.createElement("script");
    script.src = src;
    script.onload = resolve;
    script.onerror = reject;
    document.head.append(script);
  });
}

async function startScanner() {
  await loadScript("vendor/jsqr.js");
  const video = $("scanVideo");
  scanStream = await navigator.mediaDevices.getUserMedia({ video: { facingMode: "environment" }, audio: false });
  video.srcObject = scanStream;
  await video.play();
  $("scanner").hidden = false;
  $("scanButton").hidden = true;
  const canvas = $("scanCanvas");
  const context = canvas.getContext("2d", { willReadFrequently: true });
  scanTimer = setInterval(() => {
    if (video.readyState < 2 || !video.videoWidth) return;
    const scale = Math.min(1, 640 / Math.max(video.videoWidth, video.videoHeight));
    canvas.width = Math.round(video.videoWidth * scale);
    canvas.height = Math.round(video.videoHeight * scale);
    context.drawImage(video, 0, 0, canvas.width, canvas.height);
    const image = context.getImageData(0, 0, canvas.width, canvas.height);
    const found = window.jsQR(image.data, image.width, image.height, { inversionAttempts: "dontInvert" });
    if (found && found.data.includes("#1.")) {
      stopScanner();
      scanned(found.data);
    }
  }, SCAN_EVERY_MS);
}

function stopScanner() {
  clearInterval(scanTimer);
  scanTimer = null;
  if (scanStream) for (const track of scanStream.getTracks()) track.stop();
  scanStream = null;
  $("scanner").hidden = true;
  $("scanButton").hidden = false;
}

function scanned(text) {
  let url;
  try {
    url = new URL(text);
  } catch {
    return showScan();
  }
  // Another relay or a home address: go there, it pairs itself from the code in the address.
  if (url.origin !== location.origin) {
    location.href = url.href;
    return;
  }
  location.hash = url.hash;
  location.reload();
}
