"""One phone connection, whichever door it came through: frames in, frames and events out.

The first frame is the opening JSON text (its bytes are the Noise prologue), then Noise message 1
from the phone and message 2 from the TV, then sealed JSON both ways. "pair" (NKpsk0 keyed by the
code on the TV) adds a phone; "ticket" (NKpsk0 keyed by a hop ticket) adds a home key to a paired
phone; "kk" opens a session for a key QiTV already knows. Anything off script closes the
connection with a 44xx code; nothing here raises on a phone's input. No network and no Qt: the
hub feeds frames in and writes the replies out.
"""

from __future__ import annotations

from collections import deque
from dataclasses import dataclass, field
import json
import logging
import time
from typing import Callable, Literal, Optional

from services.remote.noise import CipherState, HandshakeState, NoiseError
from services.remote.pairing import RemoteStore
from services.remote.protocol import (
    MAX_FRAME,
    MAX_MESSAGE,
    MAX_RATE,
    PROTO,
    Closed,
    Forgot,
    Get,
    Held,
    Opened,
    OpenItem,
    Paired,
    Position,
    Press,
    Query,
    StoreSaved,
    ToTv,
    WatchHere,
    b64d,
    parse_opening,
    parse_phone_message,
)

logger = logging.getLogger(__name__)

MAX_NAME = 40
TOUCH_EVERY_S = 60.0

Door = Literal["home", "relay"]


@dataclass
class Reply:
    frames: list[bytes | str] = field(default_factory=list)
    events: list[object] = field(default_factory=list)
    close: Optional[tuple[int, str]] = None


class Session:
    def __init__(
        self,
        store: RemoteStore,
        *,
        sid: str,
        door: Door,
        home_url: Callable[[], str],
        same_network: Optional[bool],
        clock: Callable[[], float] = time.time,
        tv_name: str = "",
        hello: Callable[[], dict] = dict,
    ) -> None:
        self.sid = sid
        self._store = store
        self._door = door
        self._home_url = home_url
        self._same = same_network
        self._clock = clock
        self._tv_name = tv_name
        self._hello = hello
        self._state: Literal["opening", "handshake", "established", "closed"] = "opening"
        self._mode = ""
        self._prologue = b""
        self._hs: Optional[HandshakeState] = None
        self._ticket_device = ""
        self._key_id = ""
        self._device_id: Optional[str] = None
        self._send: Optional[CipherState] = None
        self._recv: Optional[CipherState] = None
        self._recent: deque[float] = deque()

    @property
    def device_id(self) -> Optional[str]:
        return self._device_id

    @property
    def established(self) -> bool:
        return self._state == "established"

    def closed_event(self) -> Closed:
        return Closed(self.sid, self._device_id)

    # ---- frames in -------------------------------------------------------------------------

    def receive(self, frame: bytes | str) -> Reply:
        if self._state == "closed":
            return Reply()
        size = len(frame.encode("utf-8")) if isinstance(frame, str) else len(frame)
        if size > MAX_FRAME:
            return self._close(4413, "too big")
        if self._state == "opening":
            return self._opening(frame)
        if not isinstance(frame, bytes):
            return self._close(4400, "expected binary")
        if self._state == "handshake":
            return self._handshake(frame)
        return self._message(frame)

    def _opening(self, frame: bytes | str) -> Reply:
        opening = parse_opening(frame) if isinstance(frame, str) else None
        if opening is None:
            return self._close(4400, "bad opening")
        self._mode = opening.mode
        self._prologue = frame.encode("utf-8") if isinstance(frame, str) else b""
        identity = self._store.ensure_identity()
        if opening.mode == "pair":
            if not self._store.pairing_psks(self._clock()):
                return self._close(4401, "pair-expired")
        elif opening.mode == "kk":
            found = self._store.find_key(opening.key_id)
            if found is None:
                return self._close(4401, "unknown")
            self._key_id = opening.key_id
            self._device_id = found[0].id
            self._hs = HandshakeState(
                "KK", initiator=False, prologue=self._prologue, s=identity, rs=found[1].pub
            )
        else:
            redeemed = self._store.redeem_ticket(opening.ticket_id, self._clock())
            if redeemed is None:
                return self._close(4401, "unknown")
            self._ticket_device, psk = redeemed
            self._hs = self._nk(psk)
        self._state = "handshake"
        return Reply()

    def _nk(self, psk: bytes) -> HandshakeState:
        return HandshakeState(
            "NKpsk0",
            initiator=False,
            prologue=self._prologue,
            s=self._store.ensure_identity(),
            rs=None,
            psk=psk,
        )

    def _handshake(self, message: bytes) -> Reply:
        now = self._clock()
        events: list[object] = []
        if self._mode == "pair":
            payload, psk = None, b""
            for psk in self._store.pairing_psks(now):
                self._hs = self._nk(psk)
                try:
                    payload = self._hs.read_message(message)
                    break
                except NoiseError:
                    continue
            if payload is None or self._hs is None:
                return self._close(4401, "pair-expired")
            parsed = _pairing_payload(payload, named=True)
            if parsed is None:
                return self._close(4400, "bad pairing")
            pub, name = parsed
            self._store.burn(psk, now)
            device, key = self._store.add_device(name, pub, self._door, now)
            self._device_id = device.id
            relay = self._store.relay
            answer = {
                "device": device.id,
                "key": key.id,
                "tv": self._tv_name,
                "relay": self._store.relay_url if relay else None,
                "room": self._store.room if relay else None,
            }
            events += [Paired(self.sid, device.id, name), Opened(self.sid, device.id, self._door)]
            events.append(StoreSaved(self._store.to_config()))
        else:
            assert self._hs is not None
            try:
                payload = self._hs.read_message(message)
            except NoiseError:
                return self._close(4401, "unknown")
            if self._mode == "ticket":
                parsed = _pairing_payload(payload, named=False)
                if parsed is None:
                    return self._close(4400, "bad ticket")
                try:
                    key = self._store.add_key(self._ticket_device, parsed[0], "home")
                except KeyError:
                    return self._close(4401, "unknown")
                self._device_id = self._ticket_device
                answer = {"device": self._device_id, "key": key.id, "tv": self._tv_name}
                events += [Opened(self.sid, self._device_id, self._door)]
                events.append(StoreSaved(self._store.to_config()))
            else:
                if self._store.find_key(self._key_id) is None:
                    return self._close(4401, "unknown")  # removed while it shook hands
                answer = {}
                assert self._device_id is not None
                events.append(Opened(self.sid, self._device_id, self._door))
                if self._touch(now):
                    events.append(StoreSaved(self._store.to_config()))
        assert self._hs is not None
        msg2 = self._hs.write_message(json.dumps(answer).encode("utf-8"))
        self._recv, self._send = self._hs.split()
        self._hs = None
        self._state = "established"
        relay = self._store.relay and bool(self._store.room)
        hello = {
            "t": "hello",
            "proto": PROTO,
            "tv": self._tv_name,
            "door": self._door,
            "room": self._store.room if relay else None,  # so the page's records stay current
            "relay": self._store.relay_url if relay else None,
        }
        hello.update(self._hello())
        return Reply([msg2, self.send(hello)], events)

    def _touch(self, now: float) -> bool:
        device = next((d for d in self._store.devices if d.id == self._device_id), None)
        if device is None or now - device.last_used < TOUCH_EVERY_S:
            return False
        self._store.touch(device.id, now)
        return True

    def _message(self, frame: bytes) -> Reply:
        now = self._clock()
        self._recent.append(now)
        while self._recent and self._recent[0] <= now - 1.0:
            self._recent.popleft()
        if len(self._recent) > MAX_RATE:
            return self._close(4429, "too fast")
        assert self._recv is not None
        try:
            obj = json.loads(self._recv.decrypt_with_ad(b"", frame))
        except NoiseError, ValueError:
            return self._close(4400, "bad message")
        if not isinstance(obj, dict):
            return self._close(4400, "bad message")
        message = parse_phone_message(obj)
        if message is None:
            logger.debug("Phone remote: ignored a message of type %r", obj.get("t"))
            return Reply()
        return self._act(message, now)

    def _act(self, m: dict, now: float) -> Reply:
        sid, t = self.sid, m["t"]
        if t == "down":
            return Reply(events=[Press(sid, m["b"], True, 1)])
        if t == "up":
            return Reply(events=[Press(sid, m["b"], False, m["n"])])
        if t == "held":
            return Reply(events=[Held(sid)])
        if t == "query":
            return Reply(events=[Query(sid, m["s"], m.get("tv", False))])
        if t == "open":
            return Reply(events=[OpenItem(sid, m["id"])])
        if t == "get":
            what, page = m["what"], m.get("page", 0)
            extra = (m.get("section", ""), m.get("genre", ""), m.get("find", ""), m.get("id", ""))
            n, source = m.get("n", -1), m.get("source", "")
            return Reply(events=[Get(sid, what, m.get("list", ""), page, *extra, n, source)])
        if t == "where":
            return Reply([self.send(self._where(now))])
        if t == "watch":
            return Reply(events=[WatchHere(sid, m.get("id", ""))])
        if t == "totv":
            return Reply(events=[ToTv(sid)])
        if t == "pos":
            return Reply(events=[Position(sid, m["ms"], m["len"])])
        # "forget": the phone leaves for good.
        device_id = self._device_id or ""
        self._store.remove_device(device_id)
        reply = self._close(1000, "forgotten")
        reply.events = [Forgot(sid, device_id), StoreSaved(self._store.to_config())]
        return reply

    def _where(self, now: float) -> dict:
        relay = self._door == "relay"
        home = self._home_url() if relay else ""
        device = self._device_id or ""
        unreachable = relay and self._store.hop_unanswered(device, now)
        ticket = None
        if relay and home and device:
            ticket = self._store.issue_ticket(device, now)
        return {
            "t": "where",
            "same": bool(self._same),
            "home": home or None,
            "ticket": ticket,
            "unreachable": unreachable,  # the last hop never arrived: don't hop by itself
        }

    # ---- frames out ------------------------------------------------------------------------

    def send(self, message: dict) -> bytes:
        """`message` sealed for this phone (the session must be established). A message too
        big for one frame raises MessageTooBig before it is sealed: the phone's count of
        messages stays in step and the session goes on."""
        if self._send is None:
            raise RuntimeError("session not established")
        data = json.dumps(message, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
        if len(data) > MAX_MESSAGE:
            raise MessageTooBig(f"{message.get('t')!r}: {len(data)} bytes")
        return self._send.encrypt_with_ad(b"", data)

    def bye(self, reason: str) -> bytes:
        return self.send({"t": "bye", "reason": reason})

    def _close(self, code: int, reason: str) -> Reply:
        self._state = "closed"
        self._hs = None
        logger.debug("Phone remote session %s closed: %s %s", self.sid, code, reason)
        return Reply(close=(code, reason))


class MessageTooBig(ValueError):
    """A message that wouldn't fit one frame once sealed (see Session.send)."""


def _pairing_payload(payload: bytes, *, named: bool) -> Optional[tuple[bytes, str]]:
    """(public key, name) from message 1 of a pairing or a ticket; None when malformed."""
    try:
        obj = json.loads(payload)
        pub = b64d(obj["pub"])
    except ValueError, KeyError, TypeError:
        return None
    if len(pub) != 32:
        return None
    if not named:
        return pub, ""
    name = obj.get("name")
    name = name.strip()[:MAX_NAME] if isinstance(name, str) else ""
    return pub, name or "Phone"
