"""A phone's side of the remote's protocol, for tests and dev scripts (no browser needed)."""

from __future__ import annotations

import json
from typing import Literal, Optional

from services.remote.noise import CipherState, HandshakeState, Keypair
from services.remote.pairing import code_psk
from services.remote.protocol import b64d, b64e


class PhoneClient:
    """One connection's worth of phone: opening, Noise message 1, then sealed JSON both ways.

    mode "pair" takes the TV's code (as in the QR), "ticket" a hop ticket "<id>.<secret>",
    "kk" the phone's key and its key id from an earlier pairing."""

    def __init__(
        self,
        tv_pub: bytes,
        *,
        mode: Literal["pair", "kk", "ticket"] = "pair",
        code: Optional[bytes] = None,
        ticket: str = "",
        key: Optional[Keypair] = None,
        key_id: str = "",
    ) -> None:
        self.mode = mode
        self.key = key or Keypair.generate()
        self.key_id = key_id
        self.ticket_id = ticket.split(".")[0] if ticket else ""
        if mode == "pair":
            psk: Optional[bytes] = code_psk(code or b"")
        elif mode == "ticket":
            psk = b64d(ticket.split(".")[1])
        else:
            psk = None
        self._opening = json.dumps(self._opening_obj(), separators=(",", ":"))
        self._hs = HandshakeState(
            "KK" if mode == "kk" else "NKpsk0",
            initiator=True,
            prologue=self._opening.encode(),
            s=self.key if mode == "kk" else None,
            rs=tv_pub,
            psk=psk,
        )
        self._send: Optional[CipherState] = None
        self._recv: Optional[CipherState] = None

    def _opening_obj(self) -> dict:
        if self.mode == "kk":
            return {"v": 1, "mode": "kk", "key": self.key_id}
        if self.mode == "ticket":
            return {"v": 1, "mode": "ticket", "ticket": self.ticket_id}
        return {"v": 1, "mode": "pair"}

    def opening(self) -> str:
        return self._opening

    def first(self, payload: Optional[dict] = None) -> bytes:
        if payload is None:
            payload = {} if self.mode == "kk" else {"pub": b64e(self.key.pub), "name": "Test phone"}
        return self._hs.write_message(json.dumps(payload).encode())

    def finish(self, msg2: bytes) -> dict:
        payload = json.loads(self._hs.read_message(msg2) or b"{}")
        self._send, self._recv = self._hs.split()
        if "key" in payload:
            self.key_id = payload["key"]
        return payload

    def seal(self, obj: object) -> bytes:
        assert self._send is not None, "handshake not finished"
        return self._send.encrypt_with_ad(b"", json.dumps(obj).encode())

    def open(self, frame: bytes) -> dict:
        assert self._recv is not None, "handshake not finished"
        return json.loads(self._recv.decrypt_with_ad(b"", frame))
