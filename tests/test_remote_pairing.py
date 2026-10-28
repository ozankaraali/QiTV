import json
import unittest

from services.remote.noise import Keypair
from services.remote.pairing import RemoteStore, code_psk
from services.remote.protocol import RELAY_URL

PUB = Keypair.generate().pub


class RemoteStoreTests(unittest.TestCase):
    def test_round_trip_keeps_devices_and_identity(self):
        s = RemoteStore.from_config(None)
        s.ensure_identity()
        s.enabled, s.relay, s.room, s.room_secret = True, False, "room-1", "secret-1"
        device, key = s.add_device("Phone A", PUB, "relay", 10.0)
        home = s.add_key(device.id, Keypair.generate().pub, "home")
        t = RemoteStore.from_config(json.loads(json.dumps(s.to_config())))
        self.assertEqual(t.identity_pub(), s.identity_pub())
        self.assertEqual(t.find_key(key.id)[0].name, "Phone A")
        self.assertEqual(t.find_key(home.id)[1].door, "home")
        self.assertEqual(t.find_key(key.id)[1].pub, PUB)
        self.assertEqual(
            (t.enabled, t.relay, t.room, t.room_secret), (True, False, "room-1", "secret-1")
        )
        self.assertEqual(t.to_config()["version"], 1)

    def test_defaults(self):
        s = RemoteStore.from_config(None)
        self.assertEqual((s.enabled, s.relay, s.relay_url), (False, True, RELAY_URL))
        self.assertEqual(s.devices, [])

    def test_unknown_or_broken_config_starts_clean(self):
        for data in ({"version": 99, "enabled": True}, {"version": 1, "devices": "x"}, 5, None, []):
            with self.subTest(data=data):
                with self.assertNoLogs(level="ERROR"):
                    s = RemoteStore.from_config(data)  # type: ignore[arg-type]
                self.assertEqual(s.devices, [])
        broken_identity = RemoteStore.from_config({"version": 1, "identity": "%%%", "relay": "x"})
        self.assertTrue(broken_identity.relay)
        self.assertEqual(len(broken_identity.ensure_identity().pub), 32)

    def test_ids_are_11_characters(self):
        s = RemoteStore.from_config(None)
        device, key = s.add_device("Phone A", PUB, "home", 1.0)
        tid = s.issue_ticket(device.id, 1.0).split(".")[0]
        self.assertEqual({len(device.id), len(key.id), len(tid)}, {11})

    def test_codes_only_while_open(self):
        s = RemoteStore.from_config(None)
        self.assertEqual(s.pairing_psks(0.0), [])
        self.assertIsNone(s.current_code())
        code = s.open_pairing(0.0)
        self.assertEqual(len(code), 16)
        self.assertEqual(s.pairing_psks(1.0), [code_psk(code)])
        s.close_pairing()
        self.assertEqual(s.pairing_psks(2.0), [])
        self.assertIsNone(s.current_code())

    def test_previous_code_accepted_15_s(self):
        s = RemoteStore.from_config(None)
        old = s.open_pairing(0.0)
        new = s.rotate_code(60.0)
        self.assertNotEqual(old, new)
        self.assertEqual(s.pairing_psks(74.9), [code_psk(new), code_psk(old)])
        self.assertEqual(s.pairing_psks(75.1), [code_psk(new)])

    def test_burn_rotates(self):
        s = RemoteStore.from_config(None)
        old = s.open_pairing(0.0)
        s.burn(code_psk(old), 5.0)
        self.assertNotEqual(s.current_code(), old)
        self.assertNotIn(code_psk(old), s.pairing_psks(6.0))

    def test_burning_the_previous_code_keeps_the_current(self):
        s = RemoteStore.from_config(None)
        old = s.open_pairing(0.0)
        new = s.rotate_code(60.0)
        s.burn(code_psk(old), 61.0)
        self.assertEqual(s.current_code(), new)
        self.assertEqual(s.pairing_psks(62.0), [code_psk(new)])

    def test_ticket_one_use_60_s(self):
        s = RemoteStore.from_config(None)
        device, _ = s.add_device("Phone A", PUB, "relay", 0.0)
        tid, secret = s.issue_ticket(device.id, 0.0).split(".")
        got = s.redeem_ticket(tid, 10.0)
        self.assertEqual(got[0], device.id)
        self.assertEqual(len(got[1]), 32)
        self.assertIsNone(s.redeem_ticket(tid, 11.0))
        late, _ = s.issue_ticket(device.id, 100.0).split(".")
        self.assertIsNone(s.redeem_ticket(late, 161.0))
        self.assertIsNone(s.redeem_ticket("nope", 1.0))

    def test_tickets_are_not_saved(self):
        s = RemoteStore.from_config(None)
        device, _ = s.add_device("Phone A", PUB, "relay", 0.0)
        tid, _ = s.issue_ticket(device.id, 0.0).split(".")
        s.open_pairing(0.0)
        saved = json.dumps(s.to_config())
        self.assertNotIn(tid, saved)
        t = RemoteStore.from_config(json.loads(saved))
        self.assertIsNone(t.redeem_ticket(tid, 1.0))
        self.assertEqual(t.pairing_psks(1.0), [])

    def test_remove_device_drops_all_keys(self):
        s = RemoteStore.from_config(None)
        device, key = s.add_device("Phone A", PUB, "relay", 0.0)
        home = s.add_key(device.id, Keypair.generate().pub, "home")
        tid, _ = s.issue_ticket(device.id, 0.0).split(".")
        self.assertTrue(s.remove_device(device.id))
        self.assertIsNone(s.find_key(key.id))
        self.assertIsNone(s.find_key(home.id))
        self.assertIsNone(s.redeem_ticket(tid, 1.0))
        self.assertFalse(s.remove_device(device.id))

    def test_touch_updates_last_used(self):
        s = RemoteStore.from_config(None)
        device, _ = s.add_device("Phone A", PUB, "relay", 0.0)
        s.touch(device.id, 99.0)
        self.assertEqual(s.devices[0].last_used, 99.0)


class ReviewFixStoreTests(unittest.TestCase):
    def test_home_keys_are_capped_oldest_dropped(self):
        s = RemoteStore.from_config(None)
        device, relay_key = s.add_device("Phone A", PUB, "relay", 0.0)
        homes = [s.add_key(device.id, Keypair.generate().pub, "home") for _ in range(7)]
        kept = [k.id for k in s.devices[0].keys]
        self.assertIn(relay_key.id, kept)  # the relay door's key is never dropped for a home one
        self.assertEqual(
            [k.id for k in s.devices[0].keys if k.door == "home"], [h.id for h in homes[-4:]]
        )
        self.assertIsNone(s.find_key(homes[0].id))

    def test_no_port_field(self):
        self.assertNotIn("port", RemoteStore.from_config(None).to_config())

    def test_a_kept_port_round_trips_and_a_wrong_one_is_dropped(self):
        kept = RemoteStore.from_config({"version": 1, "port": 51234})
        self.assertEqual(kept.port, 51234)
        self.assertEqual(kept.to_config()["port"], 51234)
        for wrong in (0, 70000, "51234", True, None):
            store = RemoteStore.from_config({"version": 1, "port": wrong})
            self.assertIsNone(store.port, wrong)
            self.assertNotIn("port", store.to_config())


class HopAnsweredTests(unittest.TestCase):
    def test_an_unredeemed_ticket_means_home_was_out_of_reach(self):
        s = RemoteStore.from_config(None)
        device, _ = s.add_device("Phone A", PUB, "relay", 0.0)
        self.assertFalse(s.hop_unanswered(device.id, 0.0))
        tid, _ = s.issue_ticket(device.id, 10.0).split(".")
        self.assertFalse(s.hop_unanswered(device.id, 11.0))  # just issued: give it a moment
        self.assertTrue(s.hop_unanswered(device.id, 20.0))
        self.assertFalse(s.hop_unanswered(device.id, 140.0))  # long ago: try home again
        tid, _ = s.issue_ticket(device.id, 200.0).split(".")
        s.redeem_ticket(tid, 205.0)
        self.assertFalse(s.hop_unanswered(device.id, 210.0))


if __name__ == "__main__":
    unittest.main()
