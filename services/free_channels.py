"""iptv-org's free playlists: all countries, or one country's (the welcome card's and the
sources column's "Free channels…").

Data only, no widgets. `provider_entry` gives the config entry to add; the window adds it.
"""

from __future__ import annotations

from typing import Optional

from services.i18n import N_, translate

ALL_URL = "https://iptv-org.github.io/iptv/index.m3u"
ALL_NAME = "iptv-org.github.io"  # the default config's entry
_COUNTRY_URL = "https://iptv-org.github.io/iptv/countries/{code}.m3u"
_NAME_PREFIX = "iptv-org: "

# (iptv-org's code, English name), sorted by name. iptv-org says "uk" for the United Kingdom.
# The names stay English here (a country's provider is saved with its English name);
# shown_name() gives one in the active language.
COUNTRIES: tuple[tuple[str, str], ...] = (
    ("af", N_("Afghanistan")),
    ("al", N_("Albania")),
    ("dz", N_("Algeria")),
    ("ad", N_("Andorra")),
    ("ao", N_("Angola")),
    ("ag", N_("Antigua and Barbuda")),
    ("ar", N_("Argentina")),
    ("am", N_("Armenia")),
    ("aw", N_("Aruba")),
    ("au", N_("Australia")),
    ("at", N_("Austria")),
    ("az", N_("Azerbaijan")),
    ("bs", N_("Bahamas")),
    ("bh", N_("Bahrain")),
    ("bd", N_("Bangladesh")),
    ("bb", N_("Barbados")),
    ("by", N_("Belarus")),
    ("be", N_("Belgium")),
    ("bz", N_("Belize")),
    ("bj", N_("Benin")),
    ("bt", N_("Bhutan")),
    ("bo", N_("Bolivia")),
    ("bq", N_("Bonaire")),
    ("ba", N_("Bosnia and Herzegovina")),
    ("br", N_("Brazil")),
    ("vg", N_("British Virgin Islands")),
    ("bn", N_("Brunei")),
    ("bg", N_("Bulgaria")),
    ("bf", N_("Burkina Faso")),
    ("bi", N_("Burundi")),
    ("kh", N_("Cambodia")),
    ("cm", N_("Cameroon")),
    ("ca", N_("Canada")),
    ("cv", N_("Cape Verde")),
    ("td", N_("Chad")),  # i18n: the country
    ("cl", N_("Chile")),
    ("cn", N_("China")),
    ("co", N_("Colombia")),
    ("km", N_("Comoros")),
    ("cr", N_("Costa Rica")),
    ("hr", N_("Croatia")),
    ("cu", N_("Cuba")),
    ("cw", N_("Curacao")),
    ("cy", N_("Cyprus")),
    ("cz", N_("Czech Republic")),
    ("cd", N_("Democratic Republic of the Congo")),
    ("dk", N_("Denmark")),
    ("do", N_("Dominican Republic")),
    ("ec", N_("Ecuador")),
    ("eg", N_("Egypt")),
    ("sv", N_("El Salvador")),
    ("gq", N_("Equatorial Guinea")),
    ("er", N_("Eritrea")),
    ("ee", N_("Estonia")),
    ("et", N_("Ethiopia")),
    ("fi", N_("Finland")),
    ("fr", N_("France")),
    ("gf", N_("French Guiana")),
    ("pf", N_("French Polynesia")),
    ("gm", N_("Gambia")),
    ("ge", N_("Georgia")),  # i18n: the country in the Caucasus
    ("de", N_("Germany")),
    ("gh", N_("Ghana")),
    ("gr", N_("Greece")),
    ("gp", N_("Guadeloupe")),
    ("gu", N_("Guam")),
    ("gt", N_("Guatemala")),
    ("gg", N_("Guernsey")),
    ("gn", N_("Guinea")),
    ("gy", N_("Guyana")),
    ("ht", N_("Haiti")),
    ("hn", N_("Honduras")),
    ("hk", N_("Hong Kong")),
    ("hu", N_("Hungary")),
    ("is", N_("Iceland")),
    ("in", N_("India")),
    ("id", N_("Indonesia")),
    ("int", N_("International")),  # i18n: free channels of no single country (a list entry)
    ("ir", N_("Iran")),
    ("iq", N_("Iraq")),
    ("ie", N_("Ireland")),
    ("il", N_("Israel")),
    ("it", N_("Italy")),
    ("ci", N_("Ivory Coast")),
    ("jm", N_("Jamaica")),
    ("jp", N_("Japan")),
    ("jo", N_("Jordan")),  # i18n: the country
    ("kz", N_("Kazakhstan")),
    ("ke", N_("Kenya")),
    ("xk", N_("Kosovo")),
    ("kw", N_("Kuwait")),
    ("kg", N_("Kyrgyzstan")),
    ("la", N_("Laos")),
    ("lv", N_("Latvia")),
    ("lb", N_("Lebanon")),
    ("ly", N_("Libya")),
    ("li", N_("Liechtenstein")),
    ("lt", N_("Lithuania")),
    ("lu", N_("Luxembourg")),
    ("mo", N_("Macao")),
    ("mg", N_("Madagascar")),
    ("my", N_("Malaysia")),
    ("mv", N_("Maldives")),
    ("ml", N_("Mali")),
    ("mt", N_("Malta")),
    ("mq", N_("Martinique")),
    ("mr", N_("Mauritania")),
    ("mu", N_("Mauritius")),
    ("mx", N_("Mexico")),
    ("md", N_("Moldova")),
    ("mc", N_("Monaco")),
    ("mn", N_("Mongolia")),
    ("me", N_("Montenegro")),
    ("ma", N_("Morocco")),
    ("mz", N_("Mozambique")),
    ("mm", N_("Myanmar")),
    ("na", N_("Namibia")),
    ("np", N_("Nepal")),
    ("nl", N_("Netherlands")),
    ("nc", N_("New Caledonia")),
    ("nz", N_("New Zealand")),
    ("ni", N_("Nicaragua")),
    ("ne", N_("Niger")),
    ("ng", N_("Nigeria")),
    ("kp", N_("North Korea")),
    ("mk", N_("North Macedonia")),
    ("no", N_("Norway")),
    ("om", N_("Oman")),
    ("pk", N_("Pakistan")),
    ("ps", N_("Palestine")),
    ("pa", N_("Panama")),
    ("pg", N_("Papua New Guinea")),
    ("py", N_("Paraguay")),
    ("pe", N_("Peru")),
    ("ph", N_("Philippines")),
    ("pl", N_("Poland")),
    ("pt", N_("Portugal")),
    ("pr", N_("Puerto Rico")),
    ("qa", N_("Qatar")),
    ("cg", N_("Republic of the Congo")),
    ("re", N_("Reunion")),  # i18n: the island (Réunion)
    ("ro", N_("Romania")),
    ("ru", N_("Russia")),
    ("rw", N_("Rwanda")),
    ("kn", N_("Saint Kitts and Nevis")),
    ("lc", N_("Saint Lucia")),
    ("vc", N_("Saint Vincent and the Grenadines")),
    ("ws", N_("Samoa")),
    ("sm", N_("San Marino")),
    ("sa", N_("Saudi Arabia")),
    ("sn", N_("Senegal")),
    ("rs", N_("Serbia")),
    ("sl", N_("Sierra Leone")),
    ("sg", N_("Singapore")),
    ("sx", N_("Sint Maarten")),
    ("sk", N_("Slovakia")),
    ("si", N_("Slovenia")),
    ("so", N_("Somalia")),
    ("za", N_("South Africa")),
    ("kr", N_("South Korea")),
    ("es", N_("Spain")),
    ("lk", N_("Sri Lanka")),
    ("sd", N_("Sudan")),
    ("sr", N_("Suriname")),
    ("se", N_("Sweden")),
    ("ch", N_("Switzerland")),
    ("sy", N_("Syria")),
    ("tw", N_("Taiwan")),
    ("tj", N_("Tajikistan")),
    ("tz", N_("Tanzania")),
    ("th", N_("Thailand")),
    ("tg", N_("Togo")),
    ("tt", N_("Trinidad and Tobago")),
    ("tn", N_("Tunisia")),
    ("tm", N_("Turkmenistan")),
    ("tr", N_("Türkiye")),
    ("ug", N_("Uganda")),
    ("ua", N_("Ukraine")),
    ("ae", N_("United Arab Emirates")),
    ("uk", N_("United Kingdom")),
    ("us", N_("United States")),
    ("uy", N_("Uruguay")),
    ("uz", N_("Uzbekistan")),
    ("va", N_("Vatican City")),
    ("ve", N_("Venezuela")),
    ("vn", N_("Vietnam")),
    ("eh", N_("Western Sahara")),
    ("ye", N_("Yemen")),
    ("zw", N_("Zimbabwe")),
)

_NAMES = dict(COUNTRIES)


def country_url(code: str) -> str:
    return _COUNTRY_URL.format(code=code)


def region_code(territory_code: str) -> Optional[str]:
    """iptv-org's code for an ISO 3166 alpha-2 territory code, None when it has no list."""
    code = (territory_code or "").strip().lower()
    if code == "gb":
        code = "uk"
    return code if code in _NAMES else None


def country_name(code: str) -> str:
    """The country's English name (as its provider is named)."""
    return _NAMES[code]


def shown_name(code: str) -> str:
    """The country's name in the active language."""
    return translate(_NAMES[code])


def provider_entry(code: Optional[str]) -> dict:
    """The M3U provider to add: the list of all countries (None) or one country's."""
    if code is None:
        return {"type": "M3UPLAYLIST", "name": ALL_NAME, "url": ALL_URL}
    return {
        "type": "M3UPLAYLIST",
        "name": f"{_NAME_PREFIX}{_NAMES[code]}",
        "url": country_url(code),
    }
