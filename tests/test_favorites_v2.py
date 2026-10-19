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

    def starred_names(self, provider, names=("News", "Film", "Other")):
        catalog = [self.item(n, provider) for n in names]
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
        self.assertEqual(self.config.provider_favorites, [{"name": "News", "provider": "*"}])
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
        self.assertEqual(self.config.provider_favorites, [{"name": "News", "provider": "*"}])
        self.assertTrue(favorites.is_favorite(self.config, demo))

    def test_unstarring_a_provider_star_removes_it(self):
        demo = self.item("Sports", "Demo TV")
        favorites.toggle(self.config, demo)
        self.assertFalse(favorites.toggle(self.config, demo))
        self.assertEqual(self.config.provider_favorites, [])
        self.assertEqual(self.config.favorites, [])

    def test_an_unstamped_item_matches_a_star_by_name_at_any_provider(self):
        favorites.toggle(self.config, self.item("Sports", "Harbor IPTV"))
        self.assertTrue(favorites.is_favorite(self.config, self.item("Sports")))
        self.assertFalse(favorites.is_favorite(self.config, self.item("News")))

    def test_favorites_stays_a_list_of_strings_for_older_builds(self):
        self.config.favorites = ["News", "Film"]
        favorites.migrate(self.config)
        self.assertEqual(self.config.favorites, ["News", "Film"])
        favorites.toggle(self.config, self.item("Sports", "Harbor IPTV"))
        favorites.toggle(self.config, self.item("News", "Demo TV"))
        self.assertTrue(all(isinstance(e, str) for e in self.config.favorites))
        self.assertEqual(self.config.favorites, ["News", "Film", "Sports"])

    def test_migrate_repairs_favorites_that_hold_entries(self):
        held = [
            {"name": "News", "provider": "*"},
            {"name": "News", "provider": "Demo TV", "off": True},
            {"name": "Sports", "provider": "Harbor IPTV"},
        ]
        self.config.favorites = [dict(e) for e in held]
        self.assertTrue(favorites.migrate(self.config))
        self.assertEqual(self.config.favorites, ["News", "Sports"])
        self.assertEqual(self.config.provider_favorites, held)
        self.assertFalse(favorites.is_favorite(self.config, self.item("News", "Demo TV")))
        self.assertTrue(favorites.is_favorite(self.config, self.item("Sports", "Harbor IPTV")))
        self.assertFalse(favorites.migrate(self.config))

    def test_migrate_repairs_favorites_when_provider_favorites_exists(self):
        held = [{"name": "Sports", "provider": "Harbor IPTV"}]
        self.config.provider_favorites = [dict(e) for e in held]
        self.config.favorites = [dict(e) for e in held]
        self.assertTrue(favorites.migrate(self.config))
        self.assertTrue(all(isinstance(e, str) for e in self.config.favorites))
        self.assertEqual(self.config.favorites, ["Sports"])
        self.assertEqual(self.config.provider_favorites, held)

    def test_migrate_takes_the_stars_an_older_build_changed(self):
        # Back on a build before 2.1, stars are edited in `favorites` only (names); upgraded
        # again, `provider_favorites` ignored those edits.
        self.config.favorites = ["News", "Film"]
        favorites.migrate(self.config)
        favorites.toggle(self.config, self.item("Sports", "Harbor IPTV"))  # at one provider
        favorites.toggle(self.config, self.item("News", "Demo TV"))  # un-starred there only
        self.config.favorites = ["News", "Sports", "Kids"]  # the old build: -Film, +Kids
        self.assertTrue(favorites.migrate(self.config))
        self.assertFalse(favorites.migrate(self.config))  # saved once
        self.assertEqual(favorites.names(self.config), ["News", "Sports", "Kids"])
        self.assertEqual(self.config.favorites, ["News", "Sports", "Kids"])
        every = ("News", "Film", "Sports", "Kids")
        self.assertEqual(self.starred_names("Demo TV", every), ["Kids"])  # Kids: everywhere
        self.assertEqual(self.starred_names("Harbor IPTV", every), ["News", "Sports", "Kids"])

    def test_a_name_an_older_build_starred_again_is_starred_everywhere(self):
        self.config.favorites = ["News"]
        favorites.migrate(self.config)
        favorites.toggle(self.config, self.item("News", "Demo TV"))
        favorites.toggle(self.config, self.item("News", "Harbor IPTV"))  # off everywhere
        self.assertEqual(self.config.favorites, ["News"])  # still "*" with two exclusions
        favorites.toggle(self.config, self.item("Film", "Demo TV"))
        favorites.toggle(self.config, self.item("Film", "Demo TV"))  # starred, then not
        self.config.favorites = ["News", "Film"]  # the old build stars Film
        self.assertTrue(favorites.migrate(self.config))
        self.assertEqual(self.starred_names("Harbor IPTV"), ["Film"])  # News: as 2.1 left it
        self.assertEqual(self.starred_names("Demo TV"), ["Film"])

    def test_migrate_leaves_stars_alone_when_favorites_only_moved(self):
        self.config.favorites = ["News", "Film"]
        favorites.migrate(self.config)
        favorites.toggle(self.config, self.item("Sports", "Harbor IPTV"))
        held = [dict(e) for e in self.config.provider_favorites]
        self.config.favorites = ["Sports", "Film", "News"]  # the same names in another order
        self.assertFalse(favorites.migrate(self.config))
        self.assertEqual(self.config.provider_favorites, held)

    def test_an_unstamped_toggle_stars_and_unstars_everywhere(self):
        self.assertTrue(favorites.toggle(self.config, self.item("News")))
        self.assertEqual(self.config.favorites, ["News"])
        self.assertEqual(self.config.provider_favorites, [{"name": "News", "provider": "*"}])
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
