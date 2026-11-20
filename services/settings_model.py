"""The settings' sections and rows, as plain data (no Qt).

The TV window's drawer paints these: sections on the left, a section's rows on the
right. A row is of a kind: it steps through its choices (STEP), opens a layer of rows of its
own or a card (OPENS), acts (ACTION), acts on something that cannot be undone and asks first
(DANGER), holds a text edited in place (TEXT), or only says something (INFO). Which config
value a row key means, and how a step or an edit is applied, is for the callers; this module
only says what exists and how stepping moves. Providers lists the providers' rows its caller
builds (ui.settings_providers: a name is shown as given, never translated), then ＋ Add
provider and Free channels.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Optional

from services.i18n import N_, SOURCE_LANGUAGE, SOURCE_NAME, number, translate

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

# A row's kind (Row.kind).
STEP = "step"  # ‹ value ›: ←/→ step it, Enter and a click on it focused step it on
OPENS = "opens"  # ›: a layer of rows (`section`), or a card the caller shows (`chosen`)
ACTION = "action"  # acts at once (`chosen`)
DANGER = "danger"  # acts, in red, its title ending in "…": the caller asks before it acts
TEXT = "text"  # a value edited in place: Enter or a click on it focused starts editing
INFO = "info"  # a muted line with its value: never focused, the keys go past it
KINDS = (STEP, OPENS, ACTION, DANGER, TEXT, INFO)

# Section key -> the key of the old Settings window's tab that still holds its settings.
LEGACY_TABS: dict[str, str] = {
    SUBTITLES: "subtitles",
    TIMESHIFT: "timeshift",
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


# Providers' rows (plan 2): `provider/<name>…` are ui.settings_providers', which lists the
# providers before these two.
PROVIDER_PREFIX = "provider/"
ADD_PROVIDER = PROVIDER_PREFIX + "@add"
FREE_CHANNELS = PROVIDER_PREFIX + "@free"
# i18n: a row of Settings → Providers, after "＋ ": adds a provider
ADD_PROVIDER_TEXT = N_("Add provider")

# Files' rows are `files/…` and Advanced's Clear Image Cache `advanced/…` (ui.settings_general's):
# they act, they are no config value.
FILES_PREFIX = "files/"
ADVANCED_PREFIX = "advanced/"
CLEAR_IMAGE_CACHE = ADVANCED_PREFIX + "clear_image_cache"
# i18n: the Language setting's first choice; {language}: the system's language, by its own name
SYSTEM_DEFAULT_TEXT = N_("System default ({language})")
# The image cache's largest sizes (MB) the stepper offers; the config's default is 100.
IMAGE_CACHE_MB = (50, 100, 200, 500, 1000, 2000)
IMAGE_CACHE_DEFAULT_MB = 100


@dataclass(frozen=True)
class Choice:
    """One value a stepping row can take, and the English text that names it; `ctx`, the
    catalogs' context it is marked with (N_(text, ctx=...)), when that English names another
    thing elsewhere. A `literal` one's text is shown as it is given (a number, or a text
    already in the active language: a guide offset's "+1 h")."""

    value: Any
    text: str
    ctx: Optional[str] = None
    literal: bool = False

    def shown(self) -> str:
        """Its text in the active language."""
        return self.text if self.literal else translate(self.text, ctx=self.ctx)


@dataclass(frozen=True)
class Row:
    """A line in a section: a stepper (`choices`), a layer (`section`) or an action, unless
    `kind` says otherwise (KINDS; "" derives it so). `text` and `help` are English texts
    marked with N_, shown translated; `note` (muted at the right end of a row that has no
    value of its own: "answered 2 min ago") and `sub` (a second, muted line under the title:
    "Xtream · in use") are shown as they are given, in the active language already. A
    `literal` row's `text` is too: a name the viewer gave (a provider's, a group's), never
    translated, even when it is also one of QiTV's texts ("General"). A `secret` text row (a
    token) shows dots in its text's place; an empty one shows its `placeholder`."""

    key: str
    text: str
    help: str = ""
    choices: tuple[Choice, ...] = ()
    default: Any = None
    section: Optional[Section] = None
    action: bool = False
    restart: bool = False  # a change takes effect after QiTV restarts
    kind: str = ""
    note: str = ""
    sub: str = ""
    literal: bool = False
    secret: bool = False  # a text row's text shows as dots, and is never said or copied
    placeholder: str = ""  # an English text (N_) an empty text row shows, muted

    def __post_init__(self) -> None:
        if not self.kind:
            derived = STEP if self.choices else OPENS if self.section is not None else ACTION
            object.__setattr__(self, "kind", derived)
        elif self.kind not in KINDS:
            raise ValueError(f"Unknown row kind: {self.kind!r}")


@dataclass(frozen=True)
class Section:
    """A group of rows. `legacy_tab` names the old Settings tab that still holds the
    section's settings until its rows move here. `empty_text` (an English text marked with
    N_) shows in place of its rows while it has none. A `literal` one's `text` is shown as it
    is given (a provider's layer: its name, in the breadcrumb)."""

    key: str
    text: str
    icon: str = ""
    rows: tuple[Row, ...] = ()
    legacy_tab: str = ""
    empty_text: str = ""
    literal: bool = False


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


@dataclass(frozen=True)
class Context:
    """What some rows show that only the running QiTV knows (ui.settings_general gathers it):
    the languages it has texts for, as (code, the language's own name), English first, and the
    name of the one the system's choice gives; whether its build carries a TMDB token; the image
    cache's largest size as set (MB: one the old window took that is none of the steps shows
    as its own); and Files' rows, as their status is now (none when QiTV cannot be the app
    files open with: a run from source). The Guide's rows, built for its source as it is now
    (ui.settings_guide), the phone remote's for its settings and its state
    (ui.settings_remote), and parental controls' for the PIN and the lock now
    (ui.settings_parental)."""

    languages: tuple[tuple[str, str], ...] = ((SOURCE_LANGUAGE, SOURCE_NAME),)
    system_language: str = SOURCE_NAME
    tmdb_built_in: bool = False
    image_cache_mb: Any = IMAGE_CACHE_DEFAULT_MB
    files: tuple[Row, ...] = ()
    guide: tuple[Row, ...] = ()
    remote: tuple[Row, ...] = ()
    parental: tuple[Row, ...] = ()


def _general(title: str, context: Context) -> Section:
    system = translate(SYSTEM_DEFAULT_TEXT, language=context.system_language)
    languages = (Choice(None, system, literal=True),) + tuple(
        Choice(code, name, literal=True) for code, name in context.languages
    )
    if context.tmdb_built_in:
        # i18n: the TMDB token field's hint in a build that carries one
        placeholder = N_("Built in; or paste your own")
    else:
        # i18n: the TMDB token row's hint in a build without one (from the source code); the
        # old window's "Your TMDB API Read Access Token", shorter to fit beside its title
        placeholder = N_("Your TMDB Read Access Token")
    rows = (
        Row(
            "language",
            N_("Language"),
            # i18n: help under the Language setting (the restart note follows it)
            N_("The language of QiTV's menus and texts."),
            languages,
            None,
            restart=True,
        ),
        Row(
            "check_updates",
            N_("Allow Check for Updates"),
            # i18n: help under the Allow Check for Updates setting
            N_("Looks for a newer QiTV as it starts, and asks before it downloads one."),
            ON_OFF,
            True,
        ),
        Row(
            "channel_logos",
            N_("Enable Channel Logos"),
            # i18n: help under the Enable Channel Logos setting
            N_("Shows each channel's logo beside its name."),
            ON_OFF,
            True,
        ),
        Row(
            "tmdb_details",
            N_("Film and series details from TMDB"),
            # i18n: help under the TMDB setting: what QiTV sends TMDB is only a title and its year
            N_("Fills in posters and stories your provider lacks. TMDB gets the title and year."),
            ON_OFF,
            True,
        ),
        Row(
            "tmdb_token",
            N_("TMDB token"),
            # i18n: help under the TMDB token (an API Read Access Token), edited in place
            N_("Your own TMDB API Read Access Token, if you have one."),
            kind=TEXT,
            secret=True,
            placeholder=placeholder,
        ),
    )
    return Section(GENERAL, title, ICONS[GENERAL], rows=rows)


def _image_cache_choices(current: Any) -> tuple[Choice, ...]:
    """The steps, and a size as set that is none of them (the old window took any) in its
    place among them."""
    sizes = list(IMAGE_CACHE_MB)
    if isinstance(current, int) and not isinstance(current, bool) and current not in sizes:
        sizes = sorted(sizes + [current])
    return tuple(Choice(size, number(size), literal=True) for size in sizes)


def _advanced(title: str, context: Context) -> Section:
    rows = (
        Row(
            "ssl_verify",
            N_("Verify SSL certificates (recommended)"),
            N_("Disable only if your provider uses self-signed certificates."),
            ON_OFF,
            True,
        ),
        Row(
            "secure_dns",
            N_("Secure DNS"),
            # i18n: help under the Secure DNS setting (DNS over HTTPS); keep "DNS"
            N_("Looks up providers' addresses over HTTPS, with Cloudflare's or Google's resolver."),
            ON_OFF,
            False,
        ),
        Row(
            "detailed_log",
            N_("Detailed log (for troubleshooting)"),
            # i18n: help under the Detailed log setting
            N_("The log also gets every step QiTV takes, for a report. Passwords are left out."),
            ON_OFF,
            False,
        ),
        Row(
            "max_cache_image_size",
            N_("Max size of image cache (MB)"),
            # i18n: help under the image cache's largest size (the restart note follows it)
            N_("How much room the posters and logos kept on disk may take."),
            _image_cache_choices(context.image_cache_mb),
            IMAGE_CACHE_DEFAULT_MB,
            restart=True,
        ),
        Row(
            CLEAR_IMAGE_CACHE,
            N_("Clear Image Cache"),
            # i18n: help under Settings → Advanced → Clear Image Cache (it asks first)
            N_("Deletes the posters and logos kept on disk. Asked first."),
            kind=DANGER,
        ),
    )
    return Section(ADVANCED, title, ICONS[ADVANCED], rows=rows)


def _files(title: str, rows: tuple[Row, ...]) -> Section:
    return Section(
        FILES,
        title,
        ICONS[FILES],
        rows=rows,
        # i18n: Settings → Files when QiTV runs from its source code: it can't be the app that
        # opens video or music files
        empty_text=N_("Not available while QiTV runs from source."),
    )


def _guide(title: str, rows: tuple[Row, ...]) -> Section:
    """Guide: its rows as ui.settings_guide builds them (they follow the guide's source)."""
    return Section(GUIDE, title, ICONS[GUIDE], rows=rows)


def _remote(title: str, rows: tuple[Row, ...]) -> Section:
    """Phone remote: its rows as ui.settings_remote builds them (they follow the remote)."""
    return Section(REMOTE, title, ICONS.get(REMOTE, ""), rows=rows)


def _parental(title: str, rows: tuple[Row, ...]) -> Section:
    """Parental controls: its rows as ui.settings_parental builds them (they follow the lock)."""
    return Section(PARENTAL, title, ICONS.get(PARENTAL, ""), rows=rows)


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
        # Off applies at once; Auto again only once QiTV starts again (libVLC 3 keeps the Off a
        # player was given). So no restart note, which would say it of Off too: the help says it.
        Row(
            "deinterlace",
            N_("Deinterlacing"),
            # i18n: help under the Deinterlacing setting (Off applies at once, Auto after a restart)
            N_(
                "Auto deinterlaces only interlaced pictures."
                " Back to Auto applies when QiTV starts again."
            ),
            (
                Choice("auto", N_("Auto", ctx="deinterlacing"), "deinterlacing"),
                Choice("off", N_("Off", ctx="deinterlacing"), "deinterlacing"),
            ),
            "auto",
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


def _providers(title: str, rows: tuple[Row, ...]) -> Section:
    """Providers: the providers' rows as given (ui.settings_providers builds them), then ＋ Add
    provider and Free channels."""
    add = Row(
        ADD_PROVIDER,
        "＋ " + translate(ADD_PROVIDER_TEXT),
        # i18n: help under Settings → Providers → Add provider
        N_("A provider from its link: Xtream, an M3U playlist, a portal, Jellyfin or Emby."),
        kind=ACTION,
        literal=True,
    )
    free = Row(
        FREE_CHANNELS,
        N_("Free channels"),  # i18n: a row of Settings → Providers; opens iptv-org's menu
        # i18n: help under Settings → Providers → Free channels (iptv-org is a project's name)
        N_("iptv-org's free channels: every country's, or one country's."),
        kind=OPENS,
    )
    return Section(PROVIDERS, title, ICONS[PROVIDERS], rows=rows + (add, free))


def sections(
    system: str, providers: tuple[Row, ...] = (), context: Optional[Context] = None
) -> tuple[Section, ...]:
    """The sections in the drawer's order; `system` is platform.system(), `providers` the
    rows of the providers, listed first in Providers, `context` what only the running QiTV
    knows (by default: English only, no TMDB token built in, the image cache's default size
    and no Files, Guide, Phone remote or Parental controls rows)."""
    context = context if context is not None else Context()
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
    built: list[Section] = []
    for key, text in titles:
        if key == PROVIDERS:
            built.append(_providers(text, tuple(providers)))
        elif key == PLAYBACK:
            built.append(_playback(system))
        elif key == GENERAL:
            built.append(_general(text, context))
        elif key == FILES:
            built.append(_files(text, tuple(context.files)))
        elif key == ADVANCED:
            built.append(_advanced(text, context))
        elif key == GUIDE:
            built.append(_guide(text, tuple(context.guide)))
        elif key == REMOTE:
            built.append(_remote(text, tuple(context.remote)))
        elif key == PARENTAL:
            built.append(_parental(text, tuple(context.parental)))
        else:
            built.append(Section(key, text, ICONS.get(key, ""), legacy_tab=LEGACY_TABS[key]))
    return tuple(built)
