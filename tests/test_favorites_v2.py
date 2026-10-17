from types import SimpleNamespace
import unittest

from services import favorites
from services.provider_ids import PROVIDER_KEY


class StarsPerProviderTests(unittest.TestCase):
    def setUp(self):
        self.config = SimpleNamespace(favorites=[])

    @staticmethod
    def item(name, provider=""):
        item = {"name": name}
        if provider:
            item[PROVIDER_KEY] = provider
        return item

    def starred_names(self, provider):
        catalog = [self.item(n, provider) for n in ("News", "Film", "Other")]
        return [i["name"] for i in catalog if favorites.is_favorite(self.config, i)]

    def test_old_stars_stay_on_every_provider(self):
        self.config.favorites = ["News"]
        self.assertTrue(favorites.migrate(self.config))
        self.assertFalse(favorites.migrate(self.config))  # saved once
        for provider in ("Demo TV", "Harbor IPTV"):
            self.assertTrue(favorites.is_favorite(self.config, self.item("News", provider)))

    def test_unstarring_at_one_provider_keeps_the_others(self):
        self.config.favorites = ["News"]
        favorites.migrate(self.config)
        favorites.toggle(self.config, self.item("News", "Demo TV"))
        self.assertFalse(favorites.is_favorite(self.config, self.item("News", "Demo TV")))
        self.assertTrue(favorites.is_favorite(self.config, self.item("News", "Harbor IPTV")))

    def test_a_new_star_belongs_to_one_provider(self):
        favorites.toggle(self.config, self.item("Sports", "Harbor IPTV"))
        self.assertFalse(favorites.is_favorite(self.config, self.item("Sports", "Demo TV")))
        self.assertTrue(favorites.is_favorite(self.config, self.item("Sports", "Harbor IPTV")))

    def test_one_provider_sees_todays_stars(self):
        self.config.favorites = ["News", "Film"]
        favorites.migrate(self.config)
        self.assertEqual(self.starred_names("Demo TV"), ["News", "Film"])

    def test_the_stored_format(self):
        self.config.favorites = ["News"]
        favorites.migrate(self.config)
        self.assertEqual(self.config.favorites, [{"name": "News", "provider": "*"}])
        favorites.toggle(self.config, self.item("News", "Demo TV"))
        self.assertEqual(
            favorites.entries(self.config),
            [
                {"name": "News", "provider": "*"},
                {"name": "News", "provider": "Demo TV", "off": True},
            ],
        )

    def test_starring_again_lifts_the_exclusion(self):
        self.config.favorites = ["News"]
        favorites.migrate(self.config)
        demo = self.item("News", "Demo TV")
        favorites.toggle(self.config, demo)
        self.assertTrue(favorites.toggle(self.config, demo))
        self.assertEqual(self.config.favorites, [{"name": "News", "provider": "*"}])
        self.assertTrue(favorites.is_favorite(self.config, demo))

    def test_unstarring_a_provider_star_removes_it(self):
        demo = self.item("Sports", "Demo TV")
        favorites.toggle(self.config, demo)
        self.assertFalse(favorites.toggle(self.config, demo))
        self.assertEqual(self.config.favorites, [])

    def test_an_unstamped_item_matches_a_star_by_name_at_any_provider(self):
        favorites.toggle(self.config, self.item("Sports", "Harbor IPTV"))
        self.assertTrue(favorites.is_favorite(self.config, self.item("Sports")))
        self.assertFalse(favorites.is_favorite(self.config, self.item("News")))

    def test_an_unstamped_toggle_stays_a_bare_name_and_unstars_everywhere(self):
        self.assertTrue(favorites.toggle(self.config, self.item("News")))
        self.assertEqual(self.config.favorites, ["News"])
        self.assertTrue(favorites.is_favorite(self.config, self.item("News", "Demo TV")))
        self.assertFalse(favorites.toggle(self.config, self.item("News")))
        self.assertEqual(self.config.favorites, [])

    def test_bare_names_work_before_they_are_migrated(self):
        self.config.favorites = ["News"]
        self.assertTrue(favorites.is_favorite(self.config, self.item("News", "Harbor IPTV")))
        self.assertEqual(favorites.names(self.config), ["News"])

    def test_names_lists_each_starred_name_once_and_skips_exclusions(self):
        self.config.favorites = ["News"]
        favorites.migrate(self.config)
        favorites.toggle(self.config, self.item("News", "Demo TV"))
        favorites.toggle(self.config, self.item("Film", "Demo TV"))
        favorites.toggle(self.config, self.item("Film", "Harbor IPTV"))
        self.assertEqual(favorites.names(self.config), ["News", "Film"])

    def test_the_fingerprint_follows_the_provider(self):
        before = favorites.fingerprint(self.config)
        favorites.toggle(self.config, self.item("News", "Demo TV"))
        mid = favorites.fingerprint(self.config)
        favorites.toggle(self.config, self.item("News", "Demo TV"))
        favorites.toggle(self.config, self.item("News", "Harbor IPTV"))
        self.assertNotEqual(before, mid)
        self.assertNotEqual(mid, favorites.fingerprint(self.config))


if __name__ == "__main__":
    unittest.main()
