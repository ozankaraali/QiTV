import json
from pathlib import Path
import unittest

from services.remote.noise import HandshakeState, Keypair, NoiseError

VECTORS = Path(__file__).parent / "fixtures" / "noise_vectors.json"
PATTERNS = {
    "Noise_KK_25519_ChaChaPoly_SHA256": "KK",
    "Noise_NKpsk0_25519_ChaChaPoly_SHA256": "NKpsk0",
}


def load_vectors() -> list[dict]:
    return json.loads(VECTORS.read_text())["vectors"]


def unhex(text: str) -> bytes:
    return bytes.fromhex(text)


def _key(v: dict, name: str) -> Keypair | None:
    return Keypair.from_private(unhex(v[name])) if name in v else None


def _psk(v: dict, name: str) -> bytes | None:
    return unhex(v[name][0]) if v.get(name) else None


class Pair:
    """Both ends of one handshake, then their transport ciphers."""

    def __init__(self, v: dict) -> None:
        pattern = PATTERNS[v["protocol_name"]]
        self.init = HandshakeState(
            pattern,
            initiator=True,
            prologue=unhex(v["init_prologue"]),
            s=_key(v, "init_static"),
            rs=unhex(v["init_remote_static"]) if "init_remote_static" in v else None,
            psk=_psk(v, "init_psks"),
            e=_key(v, "init_ephemeral"),
        )
        self.resp = HandshakeState(
            pattern,
            initiator=False,
            prologue=unhex(v["resp_prologue"]),
            s=_key(v, "resp_static"),
            rs=unhex(v["resp_remote_static"]) if "resp_remote_static" in v else None,
            psk=_psk(v, "resp_psks"),
            e=_key(v, "resp_ephemeral"),
        )
        self.ciphers: dict[str, object] = {}

    def send(self, i: int, payload: bytes) -> bytes:
        """Message i goes initiator→responder when i is even, and must arrive intact."""
        sender, receiver = (self.init, self.resp) if i % 2 == 0 else (self.resp, self.init)
        if not self.ciphers:
            if not sender.finished:
                message = sender.write_message(payload)
                self.assert_read(receiver.read_message(message), payload)
                if self.init.finished and self.resp.finished:
                    i_send, i_recv = self.init.split()
                    r_recv, r_send = self.resp.split()
                    self.ciphers = {"i": i_send, "r": r_send, "i_in": r_recv, "r_in": i_recv}
                return message
        out, into = ("i", "i_in") if i % 2 == 0 else ("r", "r_in")
        message = self.ciphers[out].encrypt_with_ad(b"", payload)
        self.assert_read(self.ciphers[into].decrypt_with_ad(b"", message), payload)
        return message

    @staticmethod
    def assert_read(got: bytes, want: bytes) -> None:
        if got != want:
            raise AssertionError(f"read {got!r}, sent {want!r}")


def nk_round(psk_init: bytes, psk_resp: bytes) -> None:
    tv = Keypair.generate()
    phone = HandshakeState("NKpsk0", initiator=True, prologue=b"p", s=None, rs=tv.pub, psk=psk_init)
    resp = HandshakeState("NKpsk0", initiator=False, prologue=b"p", s=tv, rs=None, psk=psk_resp)
    resp.read_message(phone.write_message(b"hello"))


def kk_ciphers():
    a, b = Keypair.generate(), Keypair.generate()
    init = HandshakeState("KK", initiator=True, prologue=b"", s=a, rs=b.pub)
    resp = HandshakeState("KK", initiator=False, prologue=b"", s=b, rs=a.pub)
    resp.read_message(init.write_message(b""))
    init.read_message(resp.write_message(b""))
    send, _ = init.split()
    receive, _ = resp.split()
    return send, receive


class NoiseVectorTests(unittest.TestCase):
    def test_vectors(self):
        vectors = load_vectors()
        self.assertEqual(sorted(v["protocol_name"] for v in vectors), sorted(PATTERNS))
        for v in vectors:
            with self.subTest(v["protocol_name"]):
                pair = Pair(v)
                for i, m in enumerate(v["messages"]):
                    self.assertEqual(pair.send(i, unhex(m["payload"])), unhex(m["ciphertext"]))
                self.assertEqual(pair.init.handshake_hash, unhex(v["handshake_hash"]))
                self.assertEqual(pair.resp.handshake_hash, unhex(v["handshake_hash"]))

    def test_wrong_psk_fails_on_first_message(self):
        nk_round(b"\1" * 32, b"\1" * 32)  # the same psk on both sides works
        with self.assertRaises(NoiseError):
            nk_round(b"\1" * 32, b"\2" * 32)

    def test_tampered_byte_fails(self):
        send, receive = kk_ciphers()
        message = bytearray(send.encrypt_with_ad(b"", b'{"t":"held"}'))
        message[3] ^= 0x01
        with self.assertRaises(NoiseError):
            receive.decrypt_with_ad(b"", bytes(message))

    def test_nonce_counter_increments(self):
        send, receive = kk_ciphers()
        first = send.encrypt_with_ad(b"", b"same")
        second = send.encrypt_with_ad(b"", b"same")
        self.assertNotEqual(first, second)
        self.assertEqual(receive.decrypt_with_ad(b"", first), b"same")
        self.assertEqual(receive.decrypt_with_ad(b"", second), b"same")

    def test_keypair_round_trip(self):
        key = Keypair.generate()
        self.assertEqual(len(key.priv), 32)
        self.assertEqual(len(key.pub), 32)
        self.assertEqual(Keypair.from_private(key.priv), key)


if __name__ == "__main__":
    unittest.main()
