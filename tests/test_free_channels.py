import unittest

from services import free_channels as fc
from services.config_manager import ConfigManager


class CountriesTests(unittest.TestCase):
    def test_codes_are_unique_and_the_known_ones_are_there(self):
        codes = [code for code, _name in fc.COUNTRIES]
        self.assertEqual(len(codes), len(set(codes)))
        for code in ("tr", "us", "uk", "int"):
            self.assertIn(code, codes)
        self.assertNotIn("undefined", codes)
        self.assertNotIn("gb", codes)  # iptv-org says uk

    def test_sorted_by_name_with_turkiye_spelled_right(self):
        names = [name for _code, name in fc.COUNTRIES]
        self.assertEqual(names, sorted(names, key=str.casefold))
        self.assertEqual(fc.country_name("tr"), "Türkiye")

    def test_country_url(self):
        self.assertEqual(fc.country_url("de"), "https://iptv-org.github.io/iptv/countries/de.m3u")


class RegionTests(unittest.TestCase):
    def test_region_codes(self):
        self.assertEqual(fc.region_code("GB"), "uk")
        self.assertEqual(fc.region_code("TR"), "tr")
        self.assertEqual(fc.region_code("us"), "us")
        self.assertIsNone(fc.region_code("ZZ"))
        self.assertIsNone(fc.region_code(""))


class EntryTests(unittest.TestCase):
    def test_all(self):
        self.assertEqual(
            fc.provider_entry(None),
            {
                "type": "M3UPLAYLIST",
                "name": "iptv-org.github.io",
                "url": "https://iptv-org.github.io/iptv/index.country.m3u",  # one group a country
            },
        )

    def test_the_list_of_all_countries_is_one_list_at_either_address(self):
        # Older configs hold the list grouped by genre: the same channels.
        new = fc.provider_entry(None)
        old = dict(new, url="https://iptv-org.github.io/iptv/index.m3u")
        self.assertTrue(fc.same_list(old, new))
        self.assertTrue(fc.same_list(new, new))
        self.assertFalse(fc.same_list(fc.provider_entry("uk"), new))
        self.assertTrue(fc.same_list(fc.provider_entry("uk"), fc.provider_entry("uk")))

    def test_a_country(self):
        self.assertEqual(
            fc.provider_entry("uk"),
            {
                "type": "M3UPLAYLIST",
                "name": "iptv-org: United Kingdom",
                "url": "https://iptv-org.github.io/iptv/countries/uk.m3u",
            },
        )

    def test_the_default_config_uses_the_all_entry(self):
        data = ConfigManager.default_config()
        self.assertEqual(data["data"], [fc.provider_entry(None)])
        self.assertEqual(data["selected_provider_name"], fc.ALL_NAME)


if __name__ == "__main__":
    unittest.main()
