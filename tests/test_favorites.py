from types import SimpleNamespace
import unittest

from services import favorites


class FakeConfig(SimpleNamespace):
    saves = 0

    def save_config(self):
        self.saves += 1


class FavoritesTests(unittest.TestCase):
    def test_names_are_the_key_and_title_stands_in_for_a_missing_name(self):
        self.assertEqual(favorites.favorite_key({"name": "One", "title": "Ignored"}), "One")
        self.assertEqual(favorites.favorite_key({"title": "Film"}), "Film")  # as v1.13.8
        self.assertEqual(favorites.favorite_key({"id": 3}), "")

    def test_toggle_adds_then_removes_and_reports_the_new_state(self):
        config = FakeConfig(favorites=["Kept"])
        self.assertTrue(favorites.toggle(config, {"title": "Film"}))
        self.assertEqual(config.favorites, ["Kept", "Film"])
        self.assertTrue(favorites.is_favorite(config, {"name": "Film"}))
        self.assertFalse(favorites.toggle(config, {"name": "Film"}))
        self.assertEqual(config.favorites, ["Kept"])
        self.assertEqual(config.saves, 0)  # saving is separate

    def test_an_item_without_a_name_or_title_is_left_alone(self):
        config = FakeConfig(favorites=None)
        self.assertFalse(favorites.toggle(config, {"id": 3}))
        self.assertIsNone(config.favorites)
        self.assertFalse(favorites.is_favorite(config, {"id": 3}))
        self.assertEqual(favorites.names(config), [])

    def test_save_reports_a_failure_and_logs_it(self):
        config = FakeConfig(favorites=[])
        self.assertTrue(favorites.save(config))
        self.assertEqual(config.saves, 1)

        def locked():
            raise PermissionError("locked")

        config.save_config = locked
        with self.assertLogs("services.favorites", "WARNING"):
            self.assertFalse(favorites.save(config))


if __name__ == "__main__":
    unittest.main()
