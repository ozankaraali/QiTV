"""The settings' sections and rows, as plain data (no Qt).

The TV window's drawer paints these: sections on the left, a section's rows on the
right. A row steps through its choices, opens a layer of rows of its own, or acts. Which
config value a row key means, and how a step is applied, is for the callers; this module
only says what exists and how stepping moves.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Optional

from services.i18n import N_, translate

PROVIDERS = "providers"
GENERAL = "general"
PLAYBACK = "playback"
SUBTITLES = "subtitles"
TIMESHIFT = "timeshift"
GUIDE = "guide"
REMOTE = "remote"
PARENTAL = "parental"
FILES = "files"
ADVANCED = "advanced"

# Section key -> the key of the old Settings window's tab that still holds its settings.
LEGACY_TABS: dict[str, str] = {
    PROVIDERS: "providers",
    GENERAL: "general",
    SUBTITLES: "subtitles",
    TIMESHIFT: "timeshift",
    GUIDE: "epg",
    REMOTE: "remote",
    PARENTAL: "parental",
    FILES: "general",
    ADVANCED: "general",
}

# Section key -> its icon, a drawing of ui/overlays/painting.py's draw_icon. A section with no
# fitting drawing yet (Phone remote, Parental controls) has none.
ICONS: dict[str, str] = {
    PROVIDERS: "tv",
    GENERAL: "gear",
    PLAYBACK: "play",
    SUBTITLES: "subtitles",
    TIMESHIFT: "record",
    GUIDE: "guide",
    FILES: "folder",
    ADVANCED: "more",
}


@dataclass(frozen=True)
class Choice:
    """One value a stepping row can take, and the English text that names it; `ctx`, the
    catalogs' context it is marked with (N_(text, ctx=...)), when that English names another
    thing elsewhere."""

    value: Any
    text: str
    ctx: Optional[str] = None

    def shown(self) -> str:
        """Its text in the active language."""
        return translate(self.text, ctx=self.ctx)


@dataclass(frozen=True)
class Row:
    """A line in a section: a stepper (`choices`), a layer (`section`) or an action."""

    key: str
    text: str
    help: str = ""
    choices: tuple[Choice, ...] = ()
    default: Any = None
    section: Optional[Section] = None
    action: bool = False
    restart: bool = False  # a change takes effect after QiTV restarts


@dataclass(frozen=True)
class Section:
    """A group of rows. `legacy_tab` names the old Settings tab that still holds the
    section's settings until its rows move here."""

    key: str
    text: str
    icon: str = ""
    rows: tuple[Row, ...] = ()
    legacy_tab: str = ""


# A setting's On and Off have their own catalog entries: the plain "Off" also names the
# Subtitles panel's row that shows none, the relay's state and the phone's Subtitles row, which
# some languages say otherwise (a plural). N_ needs the context written out, as a literal.
VALUE_CTX = "setting value"
ON_OFF = (
    Choice(True, N_("On", ctx="setting value"), VALUE_CTX),  # i18n: a setting's value: on
    Choice(False, N_("Off", ctx="setting value"), VALUE_CTX),  # i18n: a setting's value: off
)


def _index(row: Row, current: Any) -> int:
    """Where `current` sits among the row's choices, else where its default does."""
    for i, choice in enumerate(row.choices):
        if choice.value == current:
            return i
    for i, choice in enumerate(row.choices):
        if choice.value == row.default:
            return i
    return 0


def choice_of(row: Row, current: Any) -> Choice:
    """`current`'s choice; a value the row does not know shows as the default's."""
    return row.choices[_index(row, current)]


def step(row: Row, current: Any, delta: int) -> Any:
    """The value after (`delta` > 0) or before (< 0) `current`, wrapping round; a `delta` of 0
    gives `current` back as it is."""
    if not delta:
        return current
    place = _index(row, current)
    return row.choices[(place + (1 if delta > 0 else -1)) % len(row.choices)].value


def is_lit(row: Row, current: Any) -> bool:
    """Whether `current` differs from the default, so the row is shown highlighted."""
    return bool(choice_of(row, current).value != row.default)


def _playback(system: str) -> Section:
    pip: tuple[Row, ...] = ()
    if system == "Darwin":
        pip = (
            Row(
                "pip",
                N_("Picture in picture"),
                # i18n: help under the Picture in picture setting
                N_("When the picture floats in a small window above other apps."),
                section=Section(
                    "pip",
                    N_("Picture in picture"),
                    rows=(
                        Row(
                            "desktop_pip",
                            N_("Float the picture when you leave a fullscreen desktop"),
                            # i18n: help under the setting that floats the picture
                            N_("Keeps watching in a small window when you switch desktops."),
                            ON_OFF,
                            True,
                        ),
                        Row(
                            "covered_pip",
                            N_("Float the picture when another window covers QiTV"),
                            # i18n: help under the setting that floats the picture
                            N_("Keeps watching in a small window when something covers QiTV."),
                            ON_OFF,
                            False,
                        ),
                    ),
                ),
            ),
        )
    rows = (
        Row(
            "live_latency",
            N_("Live latency"),
            # i18n: help under the Live latency setting
            N_("How far behind live channels play. Auto adjusts each channel."),
            (
                # i18n: the Live latency setting's choices
                Choice("auto", N_("Auto (recommended)")),
                Choice("low", N_("Low")),  # i18n: the Live latency setting's choice (low latency)
                Choice("normal", N_("Normal")),  # i18n: the Live latency setting's choice
                Choice("stable", N_("Stable")),  # i18n: the Live latency setting's choice
            ),
            "auto",
        ),
        Row(
            "hardware_decoding",
            N_("Hardware video decoding"),
            # i18n: help under the Hardware video decoding setting (the restart note follows it)
            N_("Lets the graphics chip decode video."),
            ON_OFF,
            True,
            restart=True,
        ),
        Row(
            "media_4k_versions",
            N_("4K versions (Jellyfin, Emby)"),
            # i18n: help under the 4K versions setting
            N_("Whether a title plays its 4K version when the server has one."),
            (
                Choice("screen", N_("On a 4K screen")),
                Choice("always", N_("Always")),
                Choice("never", N_("Never")),
            ),
            "screen",
        ),
        Row(
            "start_with_last_channel",
            N_("Start with the last channel"),
            # i18n: help under the Start with the last channel setting
            N_("Opens QiTV on the channel you watched last."),
            ON_OFF,
            True,
        ),
        Row(
            "mouse_wheel_action",
            N_("Mouse wheel"),
            # i18n: help under the Mouse wheel setting
            N_("What the mouse wheel does over the picture."),
            (
                Choice("volume", N_("Volume")),
                # i18n: the Mouse wheel setting's choice
                Choice("channel", N_("Change channel")),
            ),
            "volume",
        ),
        Row(
            "play_in",
            N_("Play in"),
            # i18n: help under the Play in setting
            N_("Which player shows the picture: QiTV's own, or VLC or MPV outside it."),
            (
                Choice("qitv", N_("QiTV")),
                Choice("vlc", N_("VLC")),
                Choice("mpv", N_("MPV")),
            ),
            "qitv",
        ),
        *pip,
        Row(
            "autoplay",
            N_("Autoplay"),
            # i18n: help under the Autoplay setting
            N_("What plays next when an episode or a movie ends."),
            section=Section(
                "autoplay",
                N_("Autoplay"),
                rows=(
                    Row(
                        "auto_play_episodes",
                        N_("Auto-play next episode (10s countdown)"),
                        # i18n: help under the Auto-play next episode setting
                        N_("Starts the next episode after a 10 second countdown."),
                        ON_OFF,
                        True,
                    ),
                    Row(
                        "auto_play_movies",
                        N_("Suggest next movie from category (10s countdown)"),
                        # i18n: help under the Suggest next movie setting
                        N_("Offers the next movie of the category after a 10 second countdown."),
                        ON_OFF,
                        True,
                    ),
                ),
            ),
        ),
    )
    return Section(PLAYBACK, N_("Playback"), ICONS[PLAYBACK], rows=rows)


def sections(system: str) -> tuple[Section, ...]:
    """The sections in the drawer's order; `system` is platform.system()."""
    titles = (
        (PROVIDERS, N_("Providers")),  # i18n: a settings section's title
        (GENERAL, N_("General")),  # i18n: a settings section's title
        (PLAYBACK, N_("Playback")),  # i18n: a settings section's title
        (SUBTITLES, N_("Subtitles & AI captions")),  # i18n: a settings section's title
        (TIMESHIFT, N_("Time-shift & recordings")),  # i18n: a settings section's title
        (GUIDE, N_("Guide")),  # i18n: a settings section's title
        (REMOTE, N_("Phone remote")),  # i18n: a settings section's title
        (PARENTAL, N_("Parental controls")),  # i18n: a settings section's title
        (FILES, N_("Files")),  # i18n: a settings section's title
        (ADVANCED, N_("Advanced")),  # i18n: a settings section's title
    )
    return tuple(
        (
            _playback(system)
            if key == PLAYBACK
            else Section(key, text, ICONS.get(key, ""), legacy_tab=LEGACY_TABS[key])
        )
        for key, text in titles
    )
