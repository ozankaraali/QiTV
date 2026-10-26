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
    # i18n: the phone remote: a button while the TV offers where a channel was left; {time}
    # is when ("Resume · 20:10")
    "back_to": N_("Resume · {time}"),
    # i18n: the phone remote: a button (2.6): nothing plays on the TV, nothing streams
    "stop": N_("Stop"),
    # i18n: the phone remote: what records in the background, before the channel's name
    "rec": N_("REC"),
    # i18n: the phone remote's REC line menu: play the channel that records again
    "go_back": N_("Go back to {channel}"),
    "stop_recording": N_("Stop recording"),  # i18n: the phone remote's REC line menu
    # i18n: the phone remote: the phone's Watch was answered Keep recording
    "recording_kept": N_("The recording goes on: nothing opened on this phone"),
    "ch": N_("Ch"),  # i18n: the phone remote: the channel rocker's label (very short)
    "done": N_("Done"),  # i18n: the phone remote: closes the search or the numbers
    # i18n: the phone remote's search: the TV's own search shows it too (else the phone's alone)
    "show_on_tv": N_("Show on TV"),
    "library": N_("Library"),  # i18n: the phone remote: a tab
    # i18n: the phone remote: plays what the TV plays in the phone's own player instead
    "watch_here": N_("Watch on this phone"),
    "opening_here": N_("Opening on this phone…"),  # i18n: the phone remote, while it waits
    # i18n: the phone remote's channel list: where a tap plays, on the TV or on the phone
    "phone": N_("Phone"),
    # i18n: the phone remote, watching on the phone, when nothing plays there yet
    "pick_here": N_("Pick a channel below to watch it here"),
    # i18n: the phone remote, when the TV took over what the phone played
    "now_on_tv": N_("Now playing on the TV"),
    # i18n: the phone remote: a stream its page can't play opens in the phone's own player
    "open_player": N_("Open in the phone's player"),
    # i18n: the phone remote, where a film would play on the phone; TV: the TV | Phone switch
    "film_format": N_("This film doesn't play on a phone. Tap TV to watch it there."),
    # i18n: the phone remote, where a film would play on the phone (its sound: DTS and the like)
    "film_sound": N_("This film's sound doesn't play on a phone. Tap TV to watch it there."),
    # i18n: the phone remote through the relay (QiTV's server on the internet): films of
    # this kind come from QiTV itself, at home; a guest network or a VPN can't reach it
    "film_relay": N_(
        "The relay can't carry films. To watch this one on the phone, be at home on QiTV's "
        "network (not a guest network or a VPN)."
    ),
    # i18n: the phone remote: the provider didn't hand QiTV the film for the phone
    "film_provider": N_("The film didn't open. Tap TV to watch it there."),
    "remote": N_("Remote"),  # i18n: the phone remote: the tab with the buttons
    "channel_number": N_("Channel number"),  # i18n: the phone remote: the numpad's heading
    "previous": N_("Previous"),  # i18n: the phone remote's numpad: the previous channel
    "record": N_("Record"),  # i18n: the phone remote: a button (verb)
    # 2.8: casting and scheduled recordings
    "cast_to": N_("Cast to a TV"),  # i18n: the phone remote: a button: QiTV sends it to a TV
    "stop_casting": N_("Stop casting"),  # i18n: the phone remote: the picture back on QiTV
    # i18n: the phone remote: no device to cast to was found yet
    "looking_devices": N_("Looking for devices…"),
    # i18n: the phone remote's guide: record every airing of this programme (a short chip)
    "record_series": N_("Series"),
    # i18n: the phone remote's guide: a scheduled programme's chip: it will not be recorded
    "cancel_recording": N_("Cancel recording"),
    "pip": N_("Picture in picture"),  # i18n: the phone remote: a button
    # i18n: the phone remote: the same button while the TV shows a small picture
    "close_pip": N_("Close picture in picture"),
    "swap": N_("Swap pictures"),  # i18n: the phone remote: a button
    "multiview": N_("Multiview"),  # i18n: the phone remote: a button
    "picture": N_("Picture"),  # i18n: the phone remote: a button (aspect, sleep timer)
    "fullscreen": N_("Full screen"),  # i18n: the phone remote: a button
    "this_phone": N_("This phone"),  # i18n: the phone remote: its menu's title
    "guide_find": N_("Find a channel"),  # i18n: the phone remote's guide field
    "search_hint": N_("Search channels, movies and series"),  # i18n: the search field
    # i18n: the phone remote: the pairing code shown on the TV changed meanwhile
    "code_expired": N_("The code expired. Scan the new code on the TV."),
    # i18n: the phone remote: this phone was removed in QiTV's Settings
    "removed": N_("This phone was removed in QiTV. Scan the code on the TV to pair again."),
    "reconnecting": N_("Reconnecting…"),  # i18n: the phone remote: the connection dropped
    # i18n: the phone remote: QiTV can't be reached
    "offline": N_("Can't reach QiTV. Is it open on the computer?"),
    # i18n: the phone remote: after "Can't reach QiTV…", once it stopped trying by itself
    "tap_retry": N_("Tap here to try again."),
    "forget": N_("Forget this TV"),  # i18n: the phone remote's menu: unpair this phone
    "pair_another": N_("Pair another TV"),  # i18n: the phone remote's menu
    "choose_tv": N_("Choose a TV"),  # i18n: the phone remote: the list of paired TVs
    # i18n: the phone remote through the relay: QiTV isn't connected to it
    "tv_offline": N_("QiTV isn't reachable. Is it open on the computer?"),
    # i18n: the phone remote: the TV already has its 8 phones connected
    "full": N_("Too many phones are connected to this TV."),
    # i18n: the phone remote's menu: skip the relay, talk to QiTV over the home Wi-Fi
    "connect_directly": N_("Connect directly"),
    # i18n: the phone remote's menu: keep the remote as an app on the phone's Home Screen (as
    # iOS names it); also the title of the steps that follow
    "add_home": N_("Add to Home Screen"),
    # i18n: the phone remote's Add to Home Screen, step 1 (iPhone): a page opened from the camera
    # shows in a browser view whose compass button opens it in Safari
    "add_home_safari": N_("Open this page in Safari (from a scanned code: tap the compass)."),
    # i18n: the phone remote's Add to Home Screen, step 2 (iPhone): Share and Add to Home Screen
    # as Safari names them; "⋯" is Safari's menu button
    "add_home_share": N_("Tap Share (under ⋯ if you don't see it), then Add to Home Screen."),
    # i18n: the phone remote's Add to Home Screen, step 3 (iPhone): the Home Screen copy pairs
    # anew
    "add_home_pair": N_("Open QiTV from your Home Screen and scan the code on the TV once more."),
    # i18n: the phone remote's Add to Home Screen on other phones: as their browsers name it
    "add_home_menu": N_("Open your browser's menu, then Add to Home screen or Install app."),
    "watching": N_("Watching"),  # i18n: the phone remote's guide: the channel on the TV now
    "watch": N_("Watch"),  # i18n: the phone remote's timetable: tune this channel (a verb)
    "on_now": N_("Now"),  # i18n: the phone remote's timetable: the programme on now (short)
    # i18n: the phone remote's timetable: a past programme, from the provider's archive
    "from_start": N_("Watch from the start"),
    # i18n: the phone remote's timetable when the channel has no programme guide
    "no_programmes": N_("No programme guide for this channel"),
    "groups": N_("Groups"),  # i18n: the phone remote's guide: back to the channel list's groups
    "genres": N_("Genres"),  # i18n: the phone remote's Library: back to a section's genres
    # i18n: the phone remote's guide field, over its groups: finds groups and channels
    "find_group": N_("Find a group or channel"),
    # i18n: the phone remote's channel list: back to the providers, above their groups
    "providers": N_("Providers"),
    # i18n: the phone remote's Library field: finds genres and the section's films or series
    "find_genre": N_("Find a genre or title"),
    "find_title": N_("Find a title"),  # i18n: the phone remote's Library field, over a genre
    # i18n: the phone remote's Library: a list that failed to arrive
    "load_failed": N_("Couldn't load this list"),
    "try_again": N_("Try again"),  # i18n: the phone remote: a button under a failed list
    # i18n: the phone remote: a group of search results, and the button showing the channel list
    "channels": N_("Channels"),
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
