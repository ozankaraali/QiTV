"""The phone remote page's words, translated here and sent with QiTV's hello, so the phone
speaks the TV's language. The page keeps the same English as its fallback (tests check both
stay alike). Plain dict lookups: safe on the remote's network thread."""

from __future__ import annotations

from services.i18n import N_, language, translate

PAGE_STRINGS: dict[str, str] = {
    # i18n: the phone remote page (in the phone's browser) while it connects
    "connecting": N_("Connecting to QiTV…"),
    # i18n: the phone remote page before it is paired: its title
    "scan_title": N_("Scan the code on the TV"),
    # i18n: the phone remote page before it is paired; "Phone Remote…" is QiTV's menu entry
    "scan_hint": N_(
        "On the computer, open QiTV's menu → Phone Remote…, then scan the code with your "
        "phone's camera."
    ),
    # i18n: a button on the phone remote page: read the TV's QR with the phone's camera
    "scan_camera": N_("Scan with the camera"),
    # i18n: the phone remote: the badge of a channel playing live (short, capitals)
    "live": N_("LIVE"),
    # i18n: the phone remote's now-playing card when nothing plays
    "nothing_playing": N_("Nothing playing"),
    # i18n: the phone remote: the button that goes back to live TV (short)
    "tv": N_("TV"),
    "movies": N_("Movies"),  # i18n: the phone remote: a section button
    "series": N_("Series"),  # i18n: the phone remote: a section button
    "guide": N_("Guide"),  # i18n: the phone remote: a section button and a tab
    "ok": N_("OK"),  # i18n: the phone remote: the middle of the arrows (Enter)
    # i18n: the phone remote's touchpad, small, under it
    "touchpad_hint": N_("Swipe to move · tap for OK"),
    "back": N_("Back"),  # i18n: the phone remote: a button (Esc)
    "search": N_("Search"),  # i18n: the phone remote: a button
    "more": N_("More"),  # i18n: the phone remote: a button opening more keys
    "vol": N_("Vol"),  # i18n: the phone remote: the volume rocker's label (very short)
    "go_live": N_("Go live"),  # i18n: the phone remote: a button
    "ch": N_("Ch"),  # i18n: the phone remote: the channel rocker's label (very short)
    "all_channels": N_("All channels"),  # i18n: the phone remote's guide: a filter
    "favorites": N_("Favorites"),  # i18n: the phone remote's guide: a filter
    "recent": N_("Recent"),  # i18n: the phone remote's guide: a filter
    "done": N_("Done"),  # i18n: the phone remote: closes the search or the numbers
    "library": N_("Library"),  # i18n: the phone remote: a tab
    "remote": N_("Remote"),  # i18n: the phone remote: the tab with the buttons
    "channel_number": N_("Channel number"),  # i18n: the phone remote: the numpad's heading
    "previous": N_("Previous"),  # i18n: the phone remote's numpad: the previous channel
    "record": N_("Record"),  # i18n: the phone remote: a button (verb)
    "pip": N_("Picture in picture"),  # i18n: the phone remote: a button
    "swap": N_("Swap pictures"),  # i18n: the phone remote: a button
    "multiview": N_("Multiview"),  # i18n: the phone remote: a button
    "picture": N_("Picture"),  # i18n: the phone remote: a button (aspect, sleep timer)
    "fullscreen": N_("Full screen"),  # i18n: the phone remote: a button
    "this_phone": N_("This phone"),  # i18n: the phone remote: its menu's title
    "guide_find": N_("Find a channel or programme"),  # i18n: the phone remote's guide field
    "search_hint": N_("Search channels, movies and series"),  # i18n: the search field
    # i18n: the phone remote: the pairing code shown on the TV changed meanwhile
    "code_expired": N_("The code expired. Scan the new code on the TV."),
    # i18n: the phone remote: this phone was removed in QiTV's Settings
    "removed": N_("This phone was removed in QiTV. Scan the code on the TV to pair again."),
    "reconnecting": N_("Reconnecting…"),  # i18n: the phone remote: the connection dropped
    # i18n: the phone remote: QiTV can't be reached
    "offline": N_("Can't reach QiTV. Is it open on the computer?"),
    "forget": N_("Forget this TV"),  # i18n: the phone remote's menu: unpair this phone
    "pair_another": N_("Pair another TV"),  # i18n: the phone remote's menu
    "choose_tv": N_("Choose a TV"),  # i18n: the phone remote: the list of paired TVs
    # i18n: the phone remote through the relay: QiTV isn't connected to it
    "tv_offline": N_("QiTV isn't reachable. Is it open on the computer?"),
    # i18n: the phone remote: the TV already has its 8 phones connected
    "full": N_("Too many phones are connected to this TV."),
    # i18n: the phone remote's menu: skip the relay, talk to QiTV over the home Wi-Fi
    "connect_directly": N_("Connect directly"),
    "watching": N_("Watching"),  # i18n: the phone remote's guide: the channel on the TV now
    "genre": N_("Genre"),  # i18n: the phone remote's Library: the genre picker, read aloud
    # i18n: the phone remote's Library: a list that failed to arrive
    "load_failed": N_("Couldn't load this list"),
    "try_again": N_("Try again"),  # i18n: the phone remote: a button under a failed list
    "channels": N_("Channels"),  # i18n: the phone remote's search: a group of results
    "no_results": N_("Nothing found"),  # i18n: the phone remote's search found nothing
    "nothing_here": N_("Nothing here yet"),  # i18n: an empty list on the phone remote
    "loading": N_("Loading…"),  # i18n: the phone remote: a list on its way
    "use_touchpad": N_("Use a touchpad"),  # i18n: the phone remote's menu: the d-pad's style
    "use_buttons": N_("Use buttons"),  # i18n: the phone remote's menu: the d-pad's style
}


def page_strings() -> dict[str, str]:
    return {key: translate(text) for key, text in PAGE_STRINGS.items()}


def page_hello() -> dict:
    """What QiTV's hello adds for the page: its language and its words."""
    return {"lang": language(), "strings": page_strings()}
