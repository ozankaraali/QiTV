// Noise KK and NKpsk0 over 25519, ChaChaPoly and SHA256: the phone's side of
// services/remote/noise.py, checked against the same Cacophony vectors
// (scripts/remote_page_check.py --scenario vectors). Plain JS: the home door is not a secure
// context, so WebCrypto is not there; the primitives are noble's (vendor/crypto.js).
import { chacha20poly1305, hmac, randomBytes, sha256, x25519 } from "./vendor/crypto.js";

export class NoiseError extends Error {}

const NAMES = {
  KK: "Noise_KK_25519_ChaChaPoly_SHA256",
  NKpsk0: "Noise_NKpsk0_25519_ChaChaPoly_SHA256",
};
// [initiator's pre-message, responder's pre-message, message patterns]
const PATTERNS = {
  KK: [["s"], ["s"], [["e", "es", "ss"], ["e", "ee", "se"]]],
  NKpsk0: [[], ["s"], [["psk", "e", "es"], ["e", "ee"]]],
};
const EMPTY = new Uint8Array(0);

export function concat(...parts) {
  const out = new Uint8Array(parts.reduce((n, p) => n + p.length, 0));
  let at = 0;
  for (const p of parts) {
    out.set(p, at);
    at += p.length;
  }
  return out;
}

const mac = (key, data) => hmac(sha256, key, data);

function hkdf(ck, ikm, outputs) {
  const temp = mac(ck, ikm);
  const out = [mac(temp, Uint8Array.of(1))];
  for (let i = 2; i <= outputs; i++) out.push(mac(temp, concat(out[out.length - 1], Uint8Array.of(i))));
  return out;
}

export class Keypair {
  constructor(priv, pub) {
    this.priv = priv;
    this.pub = pub;
  }

  static generate() {
    return Keypair.fromPrivate(randomBytes(32));
  }

  static fromPrivate(priv) {
    return new Keypair(priv, x25519.getPublicKey(priv));
  }
}

export function dh(priv, pub) {
  if (!pub || pub.length !== 32) throw new NoiseError("bad public key");
  try {
    return x25519.getSharedSecret(priv, pub);
  } catch (error) {
    throw new NoiseError(`key agreement failed: ${error.message}`);
  }
}

export class CipherState {
  constructor(key = null) {
    this.key = key;
    this.n = 0;
  }

  nonce() {
    const nonce = new Uint8Array(12); // 4 zero bytes, then the counter little-endian
    const view = new DataView(nonce.buffer);
    view.setUint32(4, this.n >>> 0, true);
    view.setUint32(8, Math.floor(this.n / 2 ** 32), true);
    return nonce;
  }

  encrypt(ad, plaintext) {
    if (!this.key) return plaintext;
    const out = chacha20poly1305(this.key, this.nonce(), ad).encrypt(plaintext);
    this.n += 1;
    return out;
  }

  decrypt(ad, ciphertext) {
    if (!this.key) return ciphertext;
    if (ciphertext.length < 16) throw new NoiseError("message too short");
    let out;
    try {
      out = chacha20poly1305(this.key, this.nonce(), ad).decrypt(ciphertext);
    } catch {
      throw new NoiseError("message does not open");
    }
    this.n += 1;
    return out;
  }
}

class SymmetricState {
  constructor(name) {
    const bytes = new TextEncoder().encode(name);
    this.h = bytes.length <= 32 ? concat(bytes, new Uint8Array(32 - bytes.length)) : sha256(bytes);
    this.ck = this.h;
    this.cipher = new CipherState();
  }

  mixKey(ikm) {
    const [ck, key] = hkdf(this.ck, ikm, 2);
    this.ck = ck;
    this.cipher = new CipherState(key);
  }

  mixHash(data) {
    this.h = sha256(concat(this.h, data));
  }

  mixKeyAndHash(ikm) {
    const [ck, tempH, key] = hkdf(this.ck, ikm, 3);
    this.ck = ck;
    this.mixHash(tempH);
    this.cipher = new CipherState(key);
  }

  encryptAndHash(plaintext) {
    const out = this.cipher.encrypt(this.h, plaintext);
    this.mixHash(out);
    return out;
  }

  decryptAndHash(ciphertext) {
    const out = this.cipher.decrypt(this.h, ciphertext);
    this.mixHash(ciphertext);
    return out;
  }

  split() {
    const [first, second] = hkdf(this.ck, EMPTY, 2);
    return [new CipherState(first), new CipherState(second)];
  }
}

export class HandshakeState {
  constructor(pattern, { initiator, prologue, s = null, rs = null, psk = null, e = null }) {
    const [initPre, respPre, messages] = PATTERNS[pattern];
    this.messages = messages;
    this.pskMode = pattern.includes("psk");
    if (this.pskMode && (!psk || psk.length !== 32)) throw new Error("a psk pattern needs a 32-byte psk");
    Object.assign(this, { initiator, s, rs, psk, e, re: null, index: 0 });
    this.ss = new SymmetricState(NAMES[pattern]);
    this.ss.mixHash(prologue);
    for (const [ownerIsInitiator, pre] of [[true, initPre], [false, respPre]]) {
      if (!pre.includes("s")) continue;
      const key = ownerIsInitiator === initiator ? s && s.pub : rs;
      if (!key) throw new Error("this pattern needs both static keys it names");
      this.ss.mixHash(key);
    }
  }

  get finished() {
    return this.index >= this.messages.length;
  }

  get handshakeHash() {
    return this.ss.h;
  }

  myTurn() {
    return (this.index % 2 === 0) === this.initiator;
  }

  writeMessage(payload) {
    if (this.finished || !this.myTurn()) throw new NoiseError("not this side's turn to write");
    const parts = [];
    for (const token of this.messages[this.index]) {
      if (token === "e") {
        if (!this.e) this.e = Keypair.generate();
        parts.push(this.e.pub);
        this.mixE(this.e.pub);
      } else if (token === "psk") {
        this.ss.mixKeyAndHash(this.psk);
      } else {
        this.ss.mixKey(this.dh(token));
      }
    }
    parts.push(this.ss.encryptAndHash(payload));
    this.index += 1;
    return concat(...parts);
  }

  readMessage(message) {
    if (this.finished || this.myTurn()) throw new NoiseError("not this side's turn to read");
    let rest = message;
    for (const token of this.messages[this.index]) {
      if (token === "e") {
        if (rest.length < 32) throw new NoiseError("message too short");
        this.re = rest.slice(0, 32);
        rest = rest.slice(32);
        this.mixE(this.re);
      } else if (token === "psk") {
        this.ss.mixKeyAndHash(this.psk);
      } else {
        this.ss.mixKey(this.dh(token));
      }
    }
    const payload = this.ss.decryptAndHash(rest);
    this.index += 1;
    return payload;
  }

  split() {
    if (!this.finished) throw new NoiseError("handshake not finished");
    return this.ss.split();
  }

  mixE(pub) {
    this.ss.mixHash(pub);
    if (this.pskMode) this.ss.mixKey(pub);
  }

  dh(token) {
    // "es": the initiator's ephemeral with the responder's static, and so on.
    const [mine, theirs] = this.initiator ? [token[0], token[1]] : [token[1], token[0]];
    const priv = mine === "e" ? this.e : this.s;
    const pub = theirs === "e" ? this.re : this.rs;
    if (!priv || !pub) throw new NoiseError(`missing key for ${token}`);
    return dh(priv.priv, pub);
  }
}
