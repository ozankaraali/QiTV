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

from services.config_manager import ConfigManager
from services.i18n import N_, SOURCE_LANGUAGE, SOURCE_NAME, number, translate
from services.settings_texts import (
    OPENSUBTITLES_BUILT_IN_HINT,
    cache_size_text,
    memory_rewind_text,
    mib_text,
)

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

# Section key -> its icon, a drawing of ui/overlays/painting.py's draw_icon.
ICONS: dict[str, str] = {
    PROVIDERS: "tv",
    GENERAL: "gear",
    PLAYBACK: "play",
    SUBTITLES: "subtitles",
    TIMESHIFT: "record",
    GUIDE: "guide",
    REMOTE: "phone",
    PARENTAL: "lock",
    FILES: "folder",
    ADVANCED: "more",
}


# Providers' rows (plan 2): `provider/<name>…` are ui.settings_providers', which lists the
# providers before these two.
PROVIDER_PREFIX = "provider/"
ADD_PROVIDER = PROVIDER_PREFIX + "@add"
FREE_CHANNELS = PROVIDER_PREFIX + "@free"
# i18n: a row of Settings → Providers, after a ＋ (an icon): adds a provider
ADD_PROVIDER_TEXT = N_("Add provider")

# Files' rows are `files/…` and Advanced's Clear Image Cache `advanced/…` (ui.settings_general's):
# they act, they are no config value.
FILES_PREFIX = "files/"
ADVANCED_PREFIX = "advanced/"
CLEAR_IMAGE_CACHE = ADVANCED_PREFIX + "clear_image_cache"
# i18n: the Language setting's first choice; {language}: the system's language, by its own name
SYSTEM_DEFAULT_TEXT = N_("System default ({language})")
# Subtitles' own rows (ui.settings_subtitles'): AI captions' downloaded files, their deletion and
# OpenSubtitles' Sign out act; they are no config value.
SUBTITLES_PREFIX = "subtitles/"
CAPTIONS_FILES = SUBTITLES_PREFIX + "captions_files"
DELETE_CAPTIONS = SUBTITLES_PREFIX + "delete_captions"
SIGN_OUT = SUBTITLES_PREFIX + "sign_out"
# Time-shift's own rows (ui.settings_timeshift's): the disk cache's size opens a card while the
# disk is on, and the Recordings folder's rows act; they are no config value. Turning the disk
# off asks first: ui.settings_timeshift handles that config row's steps (by its key).
TIMESHIFT_PREFIX = "timeshift/"
TIMESHIFT_DISK = "timeshift_enabled"
DISK_CACHE = TIMESHIFT_PREFIX + "disk_cache"
RECORDINGS_FOLDER = TIMESHIFT_PREFIX + "recordings_folder"
CHANGE_FOLDER = TIMESHIFT_PREFIX + "change_folder"
DEFAULT_FOLDER = TIMESHIFT_PREFIX + "default_folder"
OPEN_FOLDER = TIMESHIFT_PREFIX + "open_folder"
# i18n: a setting: the disk cache's size; it opens a card with a slider
DISK_CACHE_TEXT = N_("Disk cache size")
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
    translated, even when it is also one of QiTV's texts ("General"). A `warn` row's sub line
    is in the warning colour (why a link of Network streams failed last time), else an info
    line's title. A `secret` text
    row (a token) shows dots in its text's place, and its last `secret_tail` characters after
    them when it has that many to show (an API key's last four, as the page that gave it shows
    them); an empty one shows its `placeholder`."""

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
    warn: bool = False  # its sub line (an info line without one: its title) in the warning colour
    secret: bool = False  # a text row's text shows as dots, and is never said or copied
    placeholder: str = ""  # an English text (N_) an empty text row shows, muted
    secret_tail: int = 0  # a secret's last characters shown after its dots
    icon: str = ""  # a drawing (painting.draw_icon) before its title, in the accent: ＋, ▶

    def __post_init__(self) -> None:
        if not self.kind:
            derived = STEP if self.choices else OPENS if self.section is not None else ACTION
            object.__setattr__(self, "kind", derived)
        elif self.kind not in KINDS:
            raise ValueError(f"Unknown row kind: {self.kind!r}")


@dataclass(frozen=True)
class Section:
    """A group of rows. `empty_text` (an English text marked with N_) shows in place of its
    rows while it has none. A `literal` one's `text` is shown as it is given (a provider's
    layer: its name, in the breadcrumb)."""

    key: str
    text: str
    icon: str = ""
    rows: tuple[Row, ...] = ()
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
    as its own); Files' rows, as their status is now (none when QiTV cannot be the app files
    open with: a run from source); whether its build carries an OpenSubtitles key, and whether
    a username is kept for it (Sign out shows only then); and what AI captions' downloaded files
    take (ui.settings_subtitles measures it): the line that says it,
    in the active language ("" while it is measured; None: this build has no such files to
    tell of, and no row says it), and whether there is any to delete; the time-shift settings
    as they are now (whether the disk buffer is on, the memory per stream and the disk cache,
    in MiB) and the Recordings folder (where recordings go, and whether it is one of the
    viewer's own rather than the default). The Guide's rows, built for its source as it is now
    (ui.settings_guide), the phone remote's for its settings and its state
    (ui.settings_remote), and parental controls' for the PIN and the lock now
    (ui.settings_parental)."""

    languages: tuple[tuple[str, str], ...] = ((SOURCE_LANGUAGE, SOURCE_NAME),)
    system_language: str = SOURCE_NAME
    tmdb_built_in: bool = False
    image_cache_mb: Any = IMAGE_CACHE_DEFAULT_MB
    files: tuple[Row, ...] = ()
    opensubtitles_built_in: bool = False
    opensubtitles_signed_in: bool = False
    captions_space: Optional[str] = None
    captions_downloaded: bool = False
    timeshift_disk: bool = ConfigManager.DEFAULT_OPTION_TIMESHIFT_ENABLED
    timeshift_ram_mib: int = ConfigManager.DEFAULT_OPTION_TIMESHIFT_RAM_MIB
    timeshift_max_mib: int = ConfigManager.DEFAULT_OPTION_TIMESHIFT_MAX_MIB
    recordings_folder: str = ""
    recordings_own: bool = False
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
        # The old window's "Your TMDB API Read Access Token", shorter to fit beside its title.
        # i18n: the TMDB token row's hint (muted, empty) in a build without one of its own
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
            # i18n: a setting (General): QiTV may look for a newer version as it starts
            N_("Allow Check for Updates"),
            # i18n: help under the Allow Check for Updates setting
            N_("Looks for a newer QiTV as it starts, and asks before it downloads one."),
            ON_OFF,
            True,
        ),
        Row(
            "channel_logos",
            N_("Enable Channel Logos"),  # i18n: a setting (General): logos in the channel list
            # i18n: help under the Enable Channel Logos setting
            N_("Shows each channel's logo beside its name."),
            ON_OFF,
            True,
        ),
        Row(
            "tmdb_details",
            # i18n: a setting (General): posters and stories from TMDB (a service's name)
            N_("Film and series details from TMDB"),
            # i18n: help under the TMDB setting: what QiTV sends TMDB is only a title and its year
            N_("Fills in posters and stories your provider lacks. TMDB gets the title and year."),
            ON_OFF,
            True,
        ),
        Row(
            "tmdb_token",
            # i18n: a setting: the token QiTV uses with TMDB (an API Read Access Token)
            N_("TMDB token"),
            # i18n: help under the TMDB token (an API Read Access Token), edited in place
            N_("Your own TMDB API Read Access Token, if you have one."),
            kind=TEXT,
            secret=True,
            placeholder=placeholder,
        ),
    )
    return Section(GENERAL, title, ICONS[GENERAL], rows=rows)


def _subtitles(title: str, context: Context) -> Section:
    files: tuple[Row, ...] = ()
    if context.captions_space is not None:
        files = (
            Row(
                CAPTIONS_FILES,
                N_("Downloaded files"),  # i18n: a line of Settings → Subtitles; its size follows
                # i18n: help (a tooltip) of Settings → Subtitles → Downloaded files
                N_("The space AI captions' speech models take on this computer."),
                kind=INFO,
                note=context.captions_space,
            ),
        )
        if context.captions_downloaded:
            files += (
                Row(
                    DELETE_CAPTIONS,
                    N_("Delete downloaded files"),  # i18n: deletes AI captions' speech models
                    # i18n: help under Settings → Subtitles → Delete downloaded files (it asks)
                    N_("Removes AI captions' speech models from this computer. Asked first."),
                    kind=DANGER,
                ),
            )
    if context.opensubtitles_built_in:
        key_help = OPENSUBTITLES_BUILT_IN_HINT
        key_placeholder = N_("Optional: your own key")  # i18n: the API key field's placeholder
    else:
        # i18n: help under Settings → Subtitles → OpenSubtitles API key in a build without one
        key_help = N_("Create one on the API consumers page of your free OpenSubtitles account.")
        key_placeholder = N_("Paste your API key")  # i18n: the API key field's placeholder
    signed_in = context.opensubtitles_signed_in
    # Signing in is optional: the empty fields say so (the password's only while no username
    # is kept: then it is wanted). Sign out only while there is a sign-in to forget.
    # i18n: what Settings → Subtitles' empty Username (and Password) fields show, muted
    optional = N_("Optional")
    sign_out: tuple[Row, ...] = ()
    if signed_in:
        sign_out = (
            Row(
                SIGN_OUT,
                N_("Sign out"),  # i18n: forget the OpenSubtitles username and password
                # i18n: help under Settings → Subtitles → Sign out
                N_("Clears the username and the password."),
                kind=ACTION,
            ),
        )
    rows = (
        Row(
            "ai_captions_model",
            N_("Speech model"),  # i18n: a setting: AI captions' speech model
            # i18n: help under Settings → Subtitles → Speech model
            N_("Automatic takes the more accurate one on a fast computer."),
            (
                # i18n: Settings → AI captions → Speech model: QiTV picks it by the computer's speed
                Choice("auto", N_("Automatic", ctx="a speech model"), "a speech model"),
                # i18n: Settings → AI captions → Speech model: the smaller one; keep "0.6B"
                Choice("small", N_("Faster (0.6B)")),
                # i18n: Settings → AI captions → Speech model: the larger one; keep "1.7B"
                Choice("large", N_("More accurate (1.7B)")),
            ),
            "auto",
        ),
        *files,
        Row(
            "opensubtitles_api_key",
            N_("OpenSubtitles API key"),  # i18n: a setting, edited in place
            key_help,
            kind=TEXT,
            secret=True,
            placeholder=key_placeholder,
            secret_tail=4,
        ),
        Row(
            "opensubtitles_username",
            N_("Username"),  # i18n: Settings → Subtitles: the OpenSubtitles account's username
            # i18n: help under Settings → Subtitles → Username (OpenSubtitles' sign-in)
            N_("Optional: signed in, OpenSubtitles allows you more downloads a day."),
            kind=TEXT,
            placeholder=optional,
        ),
        Row(
            "opensubtitles_password",
            N_("Password"),  # i18n: Settings → Subtitles: the OpenSubtitles account's password
            # i18n: help under Settings → Subtitles → Password (OpenSubtitles' sign-in)
            N_("The password of your OpenSubtitles account, to sign in with."),
            kind=TEXT,
            secret=True,
            placeholder="" if signed_in else optional,
        ),
        *sign_out,
    )
    return Section(SUBTITLES, title, ICONS[SUBTITLES], rows=rows)


def _timeshift(title: str, context: Context) -> Section:
    if context.timeshift_disk:
        # i18n: help under Settings → Time-shift → Disk cache size (it opens a card)
        cache_help = N_("The room every channel's buffer shares on disk. Applies at once.")
    else:
        # i18n: help (a tooltip) of Settings → Time-shift → Disk cache size while the disk is off
        cache_help = N_("The room the buffers share on disk, once the disk is used.")
    default_folder: tuple[Row, ...] = ()
    if context.recordings_own:
        default_folder = (
            Row(
                DEFAULT_FOLDER,
                # i18n: the Recordings folder goes back to the system's Movies (Videos) folder
                N_("Use the default"),
                # i18n: help under Settings → Time-shift → Use the default (the Recordings folder)
                N_("New recordings go to QiTV's folder in Movies (or Videos) again."),
                kind=ACTION,
            ),
        )
    rows = (
        Row(
            TIMESHIFT_DISK,
            N_("Use disk for a longer time-shift buffer"),  # i18n: a setting
            # i18n: help under Settings → Time-shift → Use disk for a longer time-shift buffer
            N_(
                "A channel you leave keeps its buffer up to 24 hours."
                " Applies from the next channel."
            ),
            ON_OFF,
            ConfigManager.DEFAULT_OPTION_TIMESHIFT_ENABLED,
        ),
        Row(
            "timeshift_ram_mib",
            N_("Memory per stream"),  # i18n: a setting: each stream's time-shift buffer in RAM
            # i18n: help under Settings → Time-shift → Memory per stream
            N_("Each picture's buffer when it isn't on disk. Applies from the next channel."),
            tuple(
                Choice(mib, mib_text(mib), literal=True)
                for mib in ConfigManager.TIMESHIFT_RAM_CHOICES
            ),
            ConfigManager.DEFAULT_OPTION_TIMESHIFT_RAM_MIB,
            sub=memory_rewind_text(context.timeshift_ram_mib),
        ),
        Row(
            DISK_CACHE,
            DISK_CACHE_TEXT,
            cache_help,
            kind=OPENS if context.timeshift_disk else INFO,  # its card: ›
            note=cache_size_text(context.timeshift_max_mib),
        ),
        Row(
            RECORDINGS_FOLDER,
            N_("Recordings folder"),  # i18n: a line of Settings → Time-shift; its path follows
            # i18n: help (a tooltip) of Settings → Time-shift → Recordings folder
            N_("Where R or the record button saves the live channel you watch."),
            kind=INFO,
            note=context.recordings_folder,
        ),
        Row(
            CHANGE_FOLDER,
            N_("Change…"),  # i18n: a row under the Recordings folder: picks another one
            # i18n: help under Settings → Time-shift → Change… (the Recordings folder)
            N_("Picks the folder new recordings go to."),
            kind=ACTION,
        ),
        *default_folder,
        Row(
            OPEN_FOLDER,
            N_("Open folder"),  # i18n: shows the Recordings folder in the system's file manager
            # i18n: help under Settings → Time-shift → Open folder
            N_("Shows the Recordings folder in the system's file manager."),
            kind=ACTION,
        ),
    )
    return Section(TIMESHIFT, title, ICONS[TIMESHIFT], rows=rows)


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
            # i18n: a setting (Advanced): whether QiTV checks providers' HTTPS certificates
            N_("Verify SSL certificates (recommended)"),
            # i18n: help under the Verify SSL certificates setting
            N_("Disable only if your provider uses self-signed certificates."),
            ON_OFF,
            True,
        ),
        Row(
            "secure_dns",
            N_("Secure DNS"),  # i18n: a setting (Advanced): DNS over HTTPS; keep "DNS"
            # i18n: help under the Secure DNS setting (DNS over HTTPS); keep "DNS"
            N_("Looks up providers' addresses over HTTPS, with Cloudflare's or Google's resolver."),
            ON_OFF,
            False,
        ),
        Row(
            "detailed_log",
            # i18n: a setting (Advanced): QiTV's log file gets every step, for a report
            N_("Detailed log (for troubleshooting)"),
            # i18n: help under the Detailed log setting
            N_("The log also gets every step QiTV takes, for a report. Passwords are left out."),
            ON_OFF,
            False,
        ),
        Row(
            "max_cache_image_size",
            # i18n: a setting (Advanced): the largest room posters and logos may take on disk
            N_("Max size of image cache (MB)"),
            # i18n: help under the image cache's largest size (the restart note follows it)
            N_("How much room the posters and logos kept on disk may take."),
            _image_cache_choices(context.image_cache_mb),
            IMAGE_CACHE_DEFAULT_MB,
            restart=True,
        ),
        Row(
            CLEAR_IMAGE_CACHE,
            N_("Clear Image Cache"),  # i18n: a row of Settings → Advanced: empties it (asks)
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
        # Settings → Files when QiTV runs from its source code: it can't be the app that opens
        # video or music files.
        # i18n: Settings → Files, in place of its rows, when QiTV runs from its source code
        empty_text=N_("Not available while QiTV runs from source."),
    )


def _guide(title: str, rows: tuple[Row, ...]) -> Section:
    """Guide: its rows as ui.settings_guide builds them (they follow the guide's source)."""
    return Section(GUIDE, title, ICONS[GUIDE], rows=rows)


def _remote(title: str, rows: tuple[Row, ...]) -> Section:
    """Phone remote: its rows as ui.settings_remote builds them (they follow the remote)."""
    return Section(REMOTE, title, ICONS[REMOTE], rows=rows)


def _parental(title: str, rows: tuple[Row, ...]) -> Section:
    """Parental controls: its rows as ui.settings_parental builds them (they follow the lock)."""
    return Section(PARENTAL, title, ICONS[PARENTAL], rows=rows)


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
                            # i18n: a setting (macOS): a small window when you switch desktops
                            N_("Float the picture when you leave a fullscreen desktop"),
                            # i18n: help under the setting that floats the picture
                            N_("Keeps watching in a small window when you switch desktops."),
                            ON_OFF,
                            True,
                        ),
                        Row(
                            "covered_pip",
                            # i18n: a setting (macOS): a small window when QiTV is covered
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
            N_("Live latency"),  # i18n: a setting: how far behind live a channel plays
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
            # i18n: a setting (Playback): the graphics chip decodes the video
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
            N_("Deinterlacing"),  # i18n: a setting: Auto or Off for interlaced TV pictures
            # i18n: help under the Deinterlacing setting (Off applies at once, Auto after a restart)
            N_(
                "Auto deinterlaces only interlaced pictures."
                " Back to Auto applies when QiTV starts again."
            ),
            (
                # i18n: deinterlacing: VLC's own, for the pictures marked interlaced
                Choice("auto", N_("Auto", ctx="deinterlacing"), "deinterlacing"),
                # i18n: deinterlacing: none
                Choice("off", N_("Off", ctx="deinterlacing"), "deinterlacing"),
            ),
            "auto",
        ),
        Row(
            "media_4k_versions",
            # i18n: a setting (Jellyfin and Emby): when a title's 4K version plays
            N_("4K versions (Jellyfin, Emby)"),
            # i18n: help under the 4K versions setting
            N_("Whether a title plays its 4K version when the server has one."),
            (
                # i18n: the 4K versions setting's choice: a title's 4K version plays on a 4K screen
                Choice("screen", N_("On a 4K screen")),
                Choice("always", N_("Always")),  # i18n: a choice: always play a title's 4K version
                Choice("never", N_("Never")),  # i18n: a choice: never play a title's 4K version
            ),
            "screen",
        ),
        Row(
            "start_with_last_channel",
            # i18n: a setting (Playback): QiTV opens on the channel watched last
            N_("Start with the last channel"),
            # i18n: help under the Start with the last channel setting
            N_("Opens QiTV on the channel you watched last."),
            ON_OFF,
            True,
        ),
        Row(
            "mouse_wheel_action",
            N_("Mouse wheel"),  # i18n: a setting: what the mouse wheel does over the picture
            # i18n: help under the Mouse wheel setting
            N_("What the mouse wheel does over the picture."),
            (
                Choice("volume", N_("Volume")),  # i18n: the Mouse wheel setting's choice
                # i18n: the Mouse wheel setting's choice
                Choice("channel", N_("Change channel")),
            ),
            "volume",
        ),
        Row(
            "play_in",
            N_("Play in"),  # i18n: a setting: which player shows the picture (QiTV, VLC, MPV)
            # i18n: help under the Play in setting
            N_("Which player shows the picture: QiTV's own, or VLC or MPV outside it."),
            (
                Choice("qitv", N_("QiTV")),  # i18n: the Play in setting's choice: QiTV's own
                Choice("vlc", N_("VLC")),  # i18n: the Play in setting's choice (a player's name)
                Choice("mpv", N_("MPV")),  # i18n: the Play in setting's choice (a player's name)
            ),
            "qitv",
        ),
        *pip,
        Row(
            "autoplay",
            N_("Autoplay"),  # i18n: a setting (Playback): what plays next; opens its rows
            # i18n: help under the Autoplay setting
            N_("What plays next when an episode or a movie ends."),
            section=Section(
                "autoplay",
                N_("Autoplay"),  # i18n: the Autoplay layer's name, in Settings' breadcrumb
                rows=(
                    Row(
                        "auto_play_episodes",
                        # i18n: a setting (Autoplay): the next episode starts after a countdown
                        N_("Auto-play next episode (10s countdown)"),
                        # i18n: help under the Auto-play next episode setting
                        N_("Starts the next episode after a 10 second countdown."),
                        ON_OFF,
                        True,
                    ),
                    Row(
                        "auto_play_movies",
                        # i18n: a setting (Autoplay): another movie of the category is offered
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
    provider and ＋ Free channels…, as the channel list names them (its ＋ painted)."""
    add = Row(
        ADD_PROVIDER,
        ADD_PROVIDER_TEXT,
        # i18n: help under Settings → Providers → Add provider
        N_("A provider from its link: Xtream, an M3U playlist, a portal, Jellyfin or Emby."),
        kind=ACTION,
        icon="plus",
    )
    free = Row(
        FREE_CHANNELS,
        # i18n: a row of Settings → Providers, after a ＋ (an icon); opens iptv-org's menu
        N_("Free channels…"),
        # i18n: help under Settings → Providers → Free channels (iptv-org is a project's name)
        N_("iptv-org's free channels: every country's, or one country's."),
        kind=ACTION,
        icon="plus",
    )
    return Section(PROVIDERS, title, ICONS[PROVIDERS], rows=rows + (add, free))


def sections(
    system: str, providers: tuple[Row, ...] = (), context: Optional[Context] = None
) -> tuple[Section, ...]:
    """The sections in the drawer's order; `system` is platform.system(), `providers` the
    rows of the providers, listed first in Providers, `context` what only the running QiTV
    knows (by default: English only, no TMDB or OpenSubtitles key built in, the image cache's
    default size, no Files rows, nothing to tell of AI captions' files, the time-shift
    settings' defaults, no Recordings folder of one's own and no Guide, Phone remote or
    Parental controls rows)."""
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
        elif key == SUBTITLES:
            built.append(_subtitles(text, context))
        elif key == TIMESHIFT:
            built.append(_timeshift(text, context))
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
    return tuple(built)
