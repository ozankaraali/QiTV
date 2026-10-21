"""What QiTV keeps for the phone remote: its identity, the paired phones, codes and hop tickets.

The identity (an X25519 key), the relay room and the paired phones' public keys are saved in the
config's optional `remote` object (versioned; older builds ignore it). The pairing codes shown on
the TV and the hop tickets live in memory only: a code is valid while the pairing screen shows,
rotates every 60 s (the previous one still pairs for 15 s) and is burnt by the phone that uses
it; a ticket opens one session within 60 s. Not thread-safe: the remote's hub thread owns it.
"""

from __future__ import annotations

from dataclasses import dataclass, field
import hashlib
import logging
import secrets
from typing import Any, Optional

from services.remote.noise import Keypair
from services.remote.protocol import RELAY_URL, STORE_VERSION, b64d, b64e

logger = logging.getLogger(__name__)

VERSION = STORE_VERSION
CODE_BYTES = 16
CODE_GRACE_S = 15.0
TICKET_S = 60.0
MAX_HOME_KEYS = 4  # a phone's keys for home addresses (one per address it has opened QiTV at)
_ID_BYTES = 8


def code_psk(code: bytes) -> bytes:
    """The Noise pre-shared key of a pairing code."""
    return hashlib.sha256(b"qitv pair psk" + code).digest()


def _new_id() -> str:
    return b64e(secrets.token_bytes(_ID_BYTES))


@dataclass
class DeviceKey:
    id: str
    pub: bytes
    door: str  # "relay" or "home": where the phone keeps this key


@dataclass
class Device:
    id: str
    name: str
    paired_at: float
    last_used: float
    keys: list[DeviceKey] = field(default_factory=list)


@dataclass
class _Ticket:
    device_id: str
    psk: bytes
    expires: float


class RemoteStore:
    def __init__(self) -> None:
        self.enabled = False
        self.relay = True
        self.relay_url = RELAY_URL
        self.room: Optional[str] = None
        self.room_secret: Optional[str] = None
        self.devices: list[Device] = []
        self._identity: Optional[Keypair] = None
        self._code: Optional[bytes] = None
        self._previous: Optional[tuple[bytes, float]] = None  # (code, accepted until)
        self._tickets: dict[str, _Ticket] = {}

    # ---- config ----------------------------------------------------------------------------

    @classmethod
    def from_config(cls, data: Optional[dict]) -> RemoteStore:
        store = cls()
        if not isinstance(data, dict):
            return store
        if data.get("version") != VERSION:
            logger.warning("Phone remote settings of version %r ignored", data.get("version"))
            return store
        for name, kind in (("enabled", bool), ("relay", bool), ("relay_url", str)):
            value = data.get(name)
            if isinstance(value, kind):
                setattr(store, name, value)
            elif value is not None:
                logger.warning("Phone remote setting %s dropped: %r", name, value)
        for name in ("room", "room_secret"):
            if isinstance(data.get(name), str):
                setattr(store, name, data[name])
        try:
            store._identity = Keypair.from_private(b64d(data["identity"]))
        except KeyError:
            pass
        except ValueError, TypeError:
            logger.warning("Phone remote identity dropped; a new one will be made")
        devices = data.get("devices", [])
        if not isinstance(devices, list):
            logger.warning("Phone remote devices dropped: not a list")
            devices = []
        for raw in devices:
            device = _device_from(raw)
            if device is None:
                logger.warning("A paired phone could not be read and was dropped")
            else:
                store.devices.append(device)
        return store

    def to_config(self) -> dict:
        data: dict[str, Any] = {
            "version": VERSION,
            "enabled": self.enabled,
            "relay": self.relay,
            "relay_url": self.relay_url,
            "room": self.room,
            "room_secret": self.room_secret,
            "devices": [
                {
                    "id": d.id,
                    "name": d.name,
                    "paired_at": d.paired_at,
                    "last_used": d.last_used,
                    "keys": [{"id": k.id, "pub": b64e(k.pub), "door": k.door} for k in d.keys],
                }
                for d in self.devices
            ],
        }
        if self._identity is not None:
            data["identity"] = b64e(self._identity.priv)
        return data

    # ---- identity --------------------------------------------------------------------------

    def ensure_identity(self) -> Keypair:
        if self._identity is None:
            self._identity = Keypair.generate()
        return self._identity

    def identity_pub(self) -> bytes:
        return self.ensure_identity().pub

    # ---- the pairing window ----------------------------------------------------------------

    def open_pairing(self, now: float) -> bytes:
        self._previous = None
        self._code = secrets.token_bytes(CODE_BYTES)
        return self._code

    def rotate_code(self, now: float) -> bytes:
        if self._code is not None:
            self._previous = (self._code, now + CODE_GRACE_S)
        self._code = secrets.token_bytes(CODE_BYTES)
        return self._code

    def close_pairing(self) -> None:
        self._code = None
        self._previous = None

    def current_code(self) -> Optional[bytes]:
        return self._code

    def pairing_psks(self, now: float) -> list[bytes]:
        """The pre-shared keys a pairing phone may use now, the current code's first."""
        if self._code is None:
            return []
        psks = [code_psk(self._code)]
        if self._previous is not None and now < self._previous[1]:
            psks.append(code_psk(self._previous[0]))
        return psks

    def burn(self, psk: bytes, now: float) -> None:
        """The code behind `psk` paired a phone: it pairs no other."""
        if self._previous is not None and code_psk(self._previous[0]) == psk:
            self._previous = None
        if self._code is not None and code_psk(self._code) == psk:
            self._code = secrets.token_bytes(CODE_BYTES)

    # ---- paired phones ---------------------------------------------------------------------

    def add_device(self, name: str, pub: bytes, door: str, now: float) -> tuple[Device, DeviceKey]:
        key = DeviceKey(_new_id(), pub, door)
        device = Device(_new_id(), name, now, now, [key])
        self.devices.append(device)
        return device, key

    def add_key(self, device_id: str, pub: bytes, door: str) -> DeviceKey:
        device = self._device(device_id)
        if device is None:
            raise KeyError(device_id)
        key = DeviceKey(_new_id(), pub, door)
        device.keys.append(key)
        homes = [k for k in device.keys if k.door == "home"]
        for old in homes[: max(0, len(homes) - MAX_HOME_KEYS)]:
            device.keys.remove(old)  # the oldest home addresses' keys stop opening sessions
        return key

    def find_key(self, key_id: str) -> Optional[tuple[Device, DeviceKey]]:
        for device in self.devices:
            for key in device.keys:
                if key.id == key_id:
                    return device, key
        return None

    def remove_device(self, device_id: str) -> bool:
        device = self._device(device_id)
        if device is None:
            return False
        self.devices.remove(device)
        self._tickets = {t: v for t, v in self._tickets.items() if v.device_id != device_id}
        return True

    def touch(self, device_id: str, now: float) -> None:
        device = self._device(device_id)
        if device is not None:
            device.last_used = now

    def _device(self, device_id: str) -> Optional[Device]:
        return next((d for d in self.devices if d.id == device_id), None)

    # ---- hop tickets -----------------------------------------------------------------------

    def issue_ticket(self, device_id: str, now: float) -> str:
        """A one-use ticket "<id>.<secret>" that opens one home-door session within 60 s."""
        self._tickets = {t: v for t, v in self._tickets.items() if v.expires > now}
        tid, secret = _new_id(), secrets.token_bytes(32)
        self._tickets[tid] = _Ticket(device_id, secret, now + TICKET_S)
        return f"{tid}.{b64e(secret)}"

    def redeem_ticket(self, tid: str, now: float) -> Optional[tuple[str, bytes]]:
        """(device id, psk) for a live ticket, used up by this call; None otherwise."""
        ticket = self._tickets.pop(tid, None)
        if ticket is None or ticket.expires <= now or self._device(ticket.device_id) is None:
            return None
        return ticket.device_id, ticket.psk


def _device_from(raw: object) -> Optional[Device]:
    if not isinstance(raw, dict):
        return None
    try:
        keys = [
            DeviceKey(str(k["id"]), b64d(k["pub"]), str(k["door"]))
            for k in raw["keys"]
            if isinstance(k, dict)
        ]
        return Device(
            str(raw["id"]),
            str(raw["name"]),
            float(raw["paired_at"]),
            float(raw["last_used"]),
            keys,
        )
    except KeyError, TypeError, ValueError:
        return None
