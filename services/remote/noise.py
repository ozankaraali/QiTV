"""Noise handshakes for the phone remote: KK and NKpsk0 over 25519, ChaChaPoly and SHA256.

Follows the Noise Protocol Framework (revision 34) §5: CipherState, SymmetricState and
HandshakeState, with only the two patterns the remote uses. NKpsk0 pairs a phone that knows the
TV's key from the QR (the code is the pre-shared key) or redeems a hop ticket; KK opens every
later session between keys that already know each other. Checked against the Cacophony test
vectors (tests/fixtures/noise_vectors.json).
"""

from __future__ import annotations

from dataclasses import dataclass, field
import hashlib
import hmac
from typing import Literal, Optional

from Cryptodome.Cipher import ChaCha20_Poly1305
from Cryptodome.Protocol.DH import key_agreement
from Cryptodome.PublicKey import ECC

DHLEN = 32
HASHLEN = 32
TAGLEN = 16
_MAX_NONCE = 2**64 - 1

Pattern = Literal["KK", "NKpsk0"]
_NAMES = {
    "KK": b"Noise_KK_25519_ChaChaPoly_SHA256",
    "NKpsk0": b"Noise_NKpsk0_25519_ChaChaPoly_SHA256",
}
# (initiator's pre-message, responder's pre-message, message patterns in order)
_PATTERNS: dict[str, tuple[tuple[str, ...], tuple[str, ...], tuple[tuple[str, ...], ...]]] = {
    "KK": (("s",), ("s",), (("e", "es", "ss"), ("e", "ee", "se"))),
    "NKpsk0": ((), ("s",), (("psk", "e", "es"), ("e", "ee"))),
}


class NoiseError(Exception):
    """A handshake or transport message that does not open: wrong key, code or a changed byte."""


@dataclass(frozen=True)
class Keypair:
    priv: bytes
    pub: bytes = field(repr=False)

    @staticmethod
    def generate() -> Keypair:
        key = ECC.generate(curve="curve25519")
        return Keypair(key.seed, key.public_key().export_key(format="raw"))

    @staticmethod
    def from_private(priv: bytes) -> Keypair:
        # The stubs predate curve25519, whose keys are built from a 32-byte seed.
        key = ECC.construct(curve="curve25519", seed=priv)  # type: ignore[arg-type]
        return Keypair(priv, key.public_key().export_key(format="raw"))


def dh(priv: bytes, pub: bytes) -> bytes:
    """X25519 of our private key and their public key; a degenerate public key is a NoiseError."""
    if len(pub) != DHLEN:
        raise NoiseError("bad public key length")
    try:
        ours = ECC.construct(curve="curve25519", seed=priv)  # type: ignore[arg-type]
        theirs = ECC.construct(curve="curve25519", point_x=int.from_bytes(pub, "little"))
        return bytes(key_agreement(static_priv=ours, static_pub=theirs, kdf=lambda x: x))
    except ValueError as error:
        raise NoiseError(f"key agreement failed: {error}") from error


def _hmac(key: bytes, data: bytes) -> bytes:
    return hmac.new(key, data, hashlib.sha256).digest()


def _hkdf(chaining_key: bytes, ikm: bytes, outputs: int) -> tuple[bytes, ...]:
    temp = _hmac(chaining_key, ikm)
    out = [_hmac(temp, b"\x01")]
    for i in range(2, outputs + 1):
        out.append(_hmac(temp, out[-1] + bytes([i])))
    return tuple(out)


class CipherState:
    def __init__(self, key: Optional[bytes] = None) -> None:
        self._key = key
        self._n = 0

    def has_key(self) -> bool:
        return self._key is not None

    def _nonce(self) -> bytes:
        if self._n >= _MAX_NONCE:
            raise NoiseError("nonce exhausted")
        return b"\x00" * 4 + self._n.to_bytes(8, "little")

    def encrypt_with_ad(self, ad: bytes, plaintext: bytes) -> bytes:
        if self._key is None:
            return plaintext
        cipher = ChaCha20_Poly1305.new(key=self._key, nonce=self._nonce())
        cipher.update(ad)
        ciphertext, tag = cipher.encrypt_and_digest(plaintext)
        self._n += 1
        return ciphertext + tag

    def decrypt_with_ad(self, ad: bytes, ciphertext: bytes) -> bytes:
        if self._key is None:
            return ciphertext
        if len(ciphertext) < TAGLEN:
            raise NoiseError("message too short")
        cipher = ChaCha20_Poly1305.new(key=self._key, nonce=self._nonce())
        cipher.update(ad)
        try:
            plaintext = cipher.decrypt_and_verify(ciphertext[:-TAGLEN], ciphertext[-TAGLEN:])
        except ValueError as error:
            raise NoiseError("message does not open") from error
        self._n += 1
        return plaintext


class _SymmetricState:
    def __init__(self, name: bytes) -> None:
        self.h = name.ljust(HASHLEN, b"\x00") if len(name) <= HASHLEN else _sha256(name)
        self.ck = self.h
        self.cipher = CipherState()

    def mix_key(self, ikm: bytes) -> None:
        self.ck, key = _hkdf(self.ck, ikm, 2)
        self.cipher = CipherState(key)

    def mix_hash(self, data: bytes) -> None:
        self.h = _sha256(self.h + data)

    def mix_key_and_hash(self, ikm: bytes) -> None:
        self.ck, temp_h, key = _hkdf(self.ck, ikm, 3)
        self.mix_hash(temp_h)
        self.cipher = CipherState(key)

    def encrypt_and_hash(self, plaintext: bytes) -> bytes:
        ciphertext = self.cipher.encrypt_with_ad(self.h, plaintext)
        self.mix_hash(ciphertext)
        return ciphertext

    def decrypt_and_hash(self, ciphertext: bytes) -> bytes:
        plaintext = self.cipher.decrypt_with_ad(self.h, ciphertext)
        self.mix_hash(ciphertext)
        return plaintext

    def split(self) -> tuple[CipherState, CipherState]:
        first, second = _hkdf(self.ck, b"", 2)
        return CipherState(first), CipherState(second)


def _sha256(data: bytes) -> bytes:
    return hashlib.sha256(data).digest()


class HandshakeState:
    def __init__(
        self,
        pattern: Pattern,
        *,
        initiator: bool,
        prologue: bytes,
        s: Optional[Keypair],
        rs: Optional[bytes],
        psk: Optional[bytes] = None,
        e: Optional[Keypair] = None,
    ) -> None:
        init_pre, resp_pre, self._messages = _PATTERNS[pattern]
        self._psk_mode = "psk" in pattern
        if self._psk_mode and (psk is None or len(psk) != 32):
            raise ValueError("a psk pattern needs a 32-byte psk")
        self._initiator = initiator
        self._s, self._rs, self._psk, self._e = s, rs, psk, e
        self._re: Optional[bytes] = None
        self._index = 0
        self._ss = _SymmetricState(_NAMES[pattern])
        self._ss.mix_hash(prologue)
        for owner_is_initiator, pre in ((True, init_pre), (False, resp_pre)):
            if "s" in pre:
                mine = owner_is_initiator == initiator
                key = (s.pub if s else None) if mine else rs
                if key is None:
                    raise ValueError("this pattern needs both static keys it names")
                self._ss.mix_hash(key)

    @property
    def finished(self) -> bool:
        return self._index >= len(self._messages)

    @property
    def handshake_hash(self) -> bytes:
        return self._ss.h

    def _my_turn(self) -> bool:
        return (self._index % 2 == 0) == self._initiator

    def write_message(self, payload: bytes) -> bytes:
        if self.finished or not self._my_turn():
            raise NoiseError("not this side's turn to write")
        out = bytearray()
        for token in self._messages[self._index]:
            if token == "e":
                if self._e is None:
                    self._e = Keypair.generate()
                out += self._e.pub
                self._mix_e(self._e.pub)
            elif token == "psk":
                self._ss.mix_key_and_hash(self._psk or b"")
            else:
                self._ss.mix_key(self._dh(token))
        out += self._ss.encrypt_and_hash(payload)
        self._index += 1
        return bytes(out)

    def read_message(self, message: bytes) -> bytes:
        if self.finished or self._my_turn():
            raise NoiseError("not this side's turn to read")
        rest = message
        for token in self._messages[self._index]:
            if token == "e":
                if len(rest) < DHLEN:
                    raise NoiseError("message too short")
                self._re, rest = rest[:DHLEN], rest[DHLEN:]
                self._mix_e(self._re)
            elif token == "psk":
                self._ss.mix_key_and_hash(self._psk or b"")
            else:
                self._ss.mix_key(self._dh(token))
        payload = self._ss.decrypt_and_hash(rest)
        self._index += 1
        return payload

    def split(self) -> tuple[CipherState, CipherState]:
        """(initiator→responder, responder→initiator), once the handshake is finished."""
        if not self.finished:
            raise NoiseError("handshake not finished")
        return self._ss.split()

    def _mix_e(self, pub: bytes) -> None:
        self._ss.mix_hash(pub)
        if self._psk_mode:
            self._ss.mix_key(pub)

    def _dh(self, token: str) -> bytes:
        # "es": the initiator's ephemeral with the responder's static, and so on.
        first, second = token[0], token[1]
        mine, theirs = (first, second) if self._initiator else (second, first)
        private = self._e if mine == "e" else self._s
        public = self._re if theirs == "e" else self._rs
        if private is None or public is None:
            raise NoiseError(f"missing key for {token}")
        return dh(private.priv, public)
