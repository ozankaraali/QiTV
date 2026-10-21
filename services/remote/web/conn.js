// One connection to QiTV, through either door: the opening text (also the Noise prologue),
// Noise message 1, message 2 back, then sealed JSON both ways. The relay passes the same frames
// along unread, so this is the same over ws://<home>/ws and wss://<relay>/room/<id>.
import { HandshakeState, concat } from "./noise.js";
import { sha256 } from "./vendor/crypto.js";

const EMPTY = new Uint8Array(0);
const encoder = new TextEncoder();
const decoder = new TextDecoder();
export const utf8 = (text) => encoder.encode(text);

export function b64e(bytes) {
  let text = "";
  for (const b of bytes) text += String.fromCharCode(b);
  return btoa(text).replace(/\+/g, "-").replace(/\//g, "_").replace(/=+$/, "");
}

export function b64d(text) {
  if (!/^[A-Za-z0-9_-]*$/.test(text || "")) throw new Error("not base64url");
  const plain = text.replace(/-/g, "+").replace(/_/g, "/");
  const padded = plain + "===".slice((plain.length + 3) % 4);
  return Uint8Array.from(atob(padded), (c) => c.charCodeAt(0));
}

// The Noise pre-shared key of a pairing code (services/remote/pairing.py code_psk).
export const codePsk = (code) => sha256(concat(utf8("qitv pair psk"), code));

export class Connection {
  constructor({ url, opening, pattern, s = null, rs, psk = null, payload, onopen, onmessage, onclose }) {
    this.openingText = JSON.stringify(opening);
    this.hs = new HandshakeState(pattern, { initiator: true, prologue: utf8(this.openingText), s, rs, psk });
    this.out = null;
    this.in = null;
    this.ended = false;
    this.onclose = onclose;
    const ws = new WebSocket(url);
    this.ws = ws;
    ws.binaryType = "arraybuffer";
    ws.onopen = () => {
      ws.send(this.openingText);
      ws.send(this.hs.writeMessage(utf8(JSON.stringify(payload))));
    };
    ws.onmessage = (event) => {
      if (typeof event.data === "string") return;
      const data = new Uint8Array(event.data);
      try {
        if (!this.in) {
          const answer = JSON.parse(decoder.decode(this.hs.readMessage(data)) || "{}");
          [this.out, this.in] = this.hs.split();
          this.hs = null;
          onopen(answer);
        } else {
          onmessage(JSON.parse(decoder.decode(this.in.decrypt(EMPTY, data))));
        }
      } catch (error) {
        this.end(4400, `bad message: ${error.message}`);
      }
    };
    ws.onclose = (event) => this.end(event.code, event.reason, false);
    ws.onerror = () => {}; // a close follows
  }

  get open() {
    return !!this.out && this.ws.readyState === WebSocket.OPEN;
  }

  send(message) {
    if (!this.open) return false;
    this.ws.send(this.out.encrypt(EMPTY, utf8(JSON.stringify(message))));
    return true;
  }

  end(code = 1000, reason = "", closeSocket = true) {
    if (this.ended) return;
    this.ended = true;
    if (closeSocket) {
      try {
        this.ws.close(code >= 3000 || code === 1000 ? code : 1000);
      } catch {
        // already closing
      }
    }
    this.onclose(code, reason);
  }
}
