from copy import deepcopy
import logging
import os
import sys

from PySide6.QtCore import QObject, Qt, QThread, QTimer, QUrl, Signal, Slot
from PySide6.QtGui import QDesktopServices, QGuiApplication
from PySide6.QtWidgets import (
    QAbstractItemView,
    QButtonGroup,
    QCheckBox,
    QComboBox,
    QDialog,
    QFileDialog,
    QFormLayout,
    QGridLayout,
    QGroupBox,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QLineEdit,
    QPushButton,
    QRadioButton,
    QSlider,
    QSpinBox,
    QTableWidget,
    QTableWidgetItem,
    QTabWidget,
    QVBoxLayout,
    QWidget,
)
import orjson as json

from services import failure_reasons as reasons, free_channels, i18n, provider_link, recording
from services.account_check import check_account
from services.config_manager import ConfigManager
from services.epg_view import (
    GUIDE_OFFSET_KEY,
    GUIDE_OFFSET_MAX,
    GUIDE_OFFSET_STEP,
    guide_offset_minutes,
)
from services.i18n import N_, tr, tr_n, translate
from services.lazy_import import lazy_module
from services.multikeydict import MultiKeyDict
from services.opensubtitles import ANONYMOUS_DAILY, CONSUMERS_URL, built_in_key
from services.provider_api import (
    CLIENT_HEADERS,
    https_fallback_candidates,
    known_http_only,
    remember_http_only,
)
from services.provider_link import parse_provider_link
from services.provider_types import EMBY, JELLYFIN, is_media_server
from services.redact import safe_exc
from services.thread_cleanup import ThreadCleanup
from services.tmdb import built_in_token as tmdb_built_in_token
from ui.free_channels_menu import show_menu as show_free_channels_menu
from ui.parental_settings import build_parental_tab
from ui.provider_drop import unique_name
from ui.remote_settings import build_remote_tab, save_remote_settings
from ui.update_dialogs import check_for_updates


def opensubtitles_hint() -> str:
    return tr(
        'QiTV finds subtitles on <a href="{url}">OpenSubtitles.com</a>. It needs a free'
        " OpenSubtitles account and an API key: sign up there, create a key on its API consumers"
        " page, and paste it here. The subtitles panel's “Search OpenSubtitles…” asks for it too.",
        url=CONSUMERS_URL,
    )


OPENSUBTITLES_BUILT_IN_HINT = N_(
    "This copy of QiTV has a key of its own: yours is optional, and is used instead when set."
)


def opensubtitles_sign_in_hint() -> str:
    return tr_n(
        "Signing in is optional: without it OpenSubtitles allows {n} download a day"
        " per connection; with your account, as many as it allows you. Sign out clears both.",
        "Signing in is optional: without it OpenSubtitles allows {n} downloads a day"
        " per connection; with your account, as many as it allows you. Sign out clears both.",
        ANONYMOUS_DAILY,
    )


EXTERNAL_PLAYERS_HINT = N_(
    "Any channel or movie can also go to VLC or MPV from its right-click menu or the player"
    " controls."
)

WARNING_COLOR = "#b26a00"  # verified, with a note (a portal profile dated in the past)
# i18n: Verify Provider's note under "Provider verified successfully."
HTTP_ONLY_NOTE = N_("This provider does not answer over HTTPS, so QiTV connects to it over HTTP.")
PASTE_LINK_TEXT = N_("Paste link")  # i18n: a button beside the provider's address (verb)
PASTE_LINK_TIP = N_(
    "Fill in this provider from one link your provider gave you: an Xtream link with the"
    " username and password in it, an M3U playlist's address or an STB portal's address."
    " Pasting the link into the address does the same."
)
NO_LINK_TEXT = N_("The clipboard holds no provider link.")
GUIDE_OFFSET_TIP = N_(
    "When this provider's guide is off by whole hours, move its programmes here: with +1 h,"
    " a programme the guide lists at 20:00 shows at 21:00 (the guide, the banner, now and next"
    " and the time-shift bar)."
)


def offset_text(minutes: int) -> str:
    """A guide time offset as the setting lists it: "No offset", "+1 h 30 min", "−30 min"."""
    if not minutes:
        return tr("No offset")  # i18n: the guide time offset setting's choice of none
    hours, rest = divmod(abs(minutes), 60)
    sign = "+" if minutes > 0 else "−"
    if hours and rest:
        # i18n: a guide time offset: sign is + or −, h hours and m minutes (abbreviated)
        return tr("{sign}{h} h {m} min", sign=sign, h=hours, m=rest)
    if hours:
        # i18n: a guide time offset: sign is + or −, h hours (abbreviated)
        return tr("{sign}{h} h", sign=sign, h=hours)
    # i18n: a guide time offset: sign is + or −, m minutes (abbreviated)
    return tr("{sign}{m} min", sign=sign, m=rest)


def _mib_text(size) -> str:
    return tr("{size} MiB", size=size)  # i18n: a size in mebibytes (MiB)


def _gib_text(size) -> str:
    return tr("{size} GiB", size=size)  # i18n: a size in gibibytes (GiB), as "1.5"


def epg_unit_label(unit: str) -> str:
    """The guide's update period unit (a config value, "Hours") as Settings shows it."""
    labels = {
        # i18n: the unit of the guide's update period: "Check update every [2] [Hours]"
        "Minutes": tr("Minutes"),
        "Hours": tr("Hours"),  # i18n: the unit of "Check update every [2] [Hours]"
        "Days": tr("Days"),  # i18n: the unit of "Check update every [2] [Days]"
        "Weeks": tr("Weeks"),  # i18n: the unit of "Check update every [2] [Weeks]"
        "Months": tr("Months"),  # i18n: the unit of "Check update every [2] [Months]"
    }
    return labels.get(unit, unit)


# A provider link's kind, as Paste link names it.
LINK_KINDS = {
    provider_link.XTREAM: "Xtream",
    provider_link.M3U: N_("M3U playlist"),
    provider_link.STB: N_("STB portal"),
}

requests = lazy_module("requests")

# Jobs outlive a closed dialog; a running QThread must never be owned by that dialog.
_verification_jobs: list[tuple[QThread, QObject]] = []


class ProviderVerificationWorker(QObject):
    """Settings' Verify Provider, off the GUI thread: an STB or Xtream account is asked as
    the account check asks it (services.account_check: no stream is opened), and a refusal
    says why in plain words (`reason`, before `result`); a playlist must answer. Each is
    asked over HTTPS first: one that answered only over HTTP says so (`http_only`, before
    `result`; 2.7.1)."""

    result = Signal(int, object, bool)
    reason = Signal(int, str)  # the plain reason a verification failed ("" when none)
    http_only = Signal(int)  # the verified provider answered only over HTTP
    # 2.8: a media server's sign-in: {token, user_id, device_id} to keep (before `result`)
    signed_in = Signal(int, object)
    finished = Signal()

    def __init__(self, request_id, provider, ssl_verify):
        super().__init__()
        self.request_id = request_id
        self.provider = provider
        self.ssl_verify = ssl_verify

    @Slot()
    def run(self):
        verified, why, http_only = False, "", False
        try:
            provider = self.provider
            if provider["type"] in ("STB", "XTREAM"):
                found = check_account(provider, ssl_verify=self.ssl_verify)
                # An account at its stream limit is still a working account, and a portal
                # profile dated in the past alone is only warned of (such dates go stale).
                working = (reasons.OK, reasons.LIMIT, reasons.DATED)
                verified = found is not None and found.kind in working
                if found is not None and found.kind != reasons.LIMIT and found.plain:
                    why = found.text  # why it failed, or (DATED) the warning
            elif provider["type"] in ("M3UPLAYLIST", "M3USTREAM"):
                verified = self.verify_url(provider["url"], verify_ssl=self.ssl_verify)
            elif is_media_server(provider["type"]):
                verified, why = self.sign_in(provider)
            http_only = (
                verified
                and not is_media_server(provider["type"])
                and self.answered_only_over_http(provider)
            )
        except Exception as e:
            # requests exceptions name the URL, credentials and all: the type and host only.
            logging.getLogger(__name__).warning("Provider verification failed: %s", safe_exc(e))
        finally:
            self.reason.emit(self.request_id, why)
            if http_only:
                self.http_only.emit(self.request_id)
            self.result.emit(self.request_id, self.provider, verified)
            self.finished.emit()

    def sign_in(self, provider) -> tuple[bool, str]:
        """A Jellyfin or Emby server's sign-in (2.8): (signed in, why not). Its access goes out
        through `signed_in`; the password is used here only."""
        from services import media_workers

        ok, value = media_workers.sign_in(  # this thread's: it blocks only the verification
            str(provider.get("url") or ""),
            str(provider.get("type") or ""),
            str(provider.get("username") or ""),
            str(provider.get("password") or ""),
            self.ssl_verify,
            device_id=str(provider.get("device_id") or ""),
        )
        if ok:
            self.signed_in.emit(self.request_id, value)
            return True, ""
        return False, str(value)

    def answered_only_over_http(self, provider) -> bool:
        """Whether the verified `provider`, asked over HTTPS first, answered only over HTTP:
        an Xtream panel at neither its address's HTTPS form nor the HTTPS port it advertises;
        a portal or a playlist at its address's HTTPS form."""
        url = str(provider.get("url") or "").strip()
        if not url.startswith("http://"):
            return False
        if provider["type"] == "XTREAM":
            from services.workers import xtream_answers_https

            username, password = provider.get("username"), provider.get("password")
            return not xtream_answers_https(
                url, str(username or ""), str(password or ""), verify_ssl=self.ssl_verify
            )
        return known_http_only(url)

    @staticmethod
    def verify_url(url, *, verify_ssl=True):
        """Whether the playlist at `url` answers (over HTTPS first: an HTTP address that
        answers only over HTTP is remembered so, `known_http_only`)."""
        if url.startswith(("http://", "https://")):
            try:
                for turl in https_fallback_candidates(url, True):
                    try:
                        response = requests.head(
                            turl, timeout=5, headers=CLIENT_HEADERS, verify=verify_ssl
                        )
                        if response.status_code == 200:
                            if turl.startswith("http://"):
                                remember_http_only(turl)
                            return True
                    except requests.RequestException:
                        continue
                return False
            except Exception as e:
                logging.getLogger(__name__).warning("Error verifying URL: %s", safe_exc(e))
                return False
        else:
            return os.path.isfile(url)


def _release_verification_job(thread, worker):
    _verification_jobs.remove((thread, worker))


class WidgetGroup:
    """Helper class to show/hide a group of related widgets together."""

    def __init__(self, *widgets):
        self._widgets = list(widgets)

    def add(self, *widgets):
        """Add widgets to the group."""
        self._widgets.extend(widgets)

    def show(self):
        """Show all widgets in the group."""
        for widget in self._widgets:
            widget.show()

    def hide(self):
        """Hide all widgets in the group."""
        for widget in self._widgets:
            widget.hide()


class AddXmltvMappingDialog(QDialog):
    def __init__(self, parent=None, channel_name="", logo_url="", channel_ids=""):
        super().__init__(parent)
        self.setWindowTitle(tr("Add/Edit XMLTV Mapping"))

        form_layout = QFormLayout(self)

        self.channel_name_input = QLineEdit(self)
        self.channel_name_input.setText(channel_name)
        form_layout.addRow(tr("Channel Name:"), self.channel_name_input)

        self.logo_url_input = QLineEdit(self)
        self.logo_url_input.setText(logo_url)
        form_layout.addRow(tr("Logo URL:"), self.logo_url_input)

        self.channel_ids_input = QLineEdit(self)
        self.channel_ids_input.setText(channel_ids)
        # i18n: the XMLTV guide's ids of a channel, separated by commas
        form_layout.addRow(tr("Channel IDs (comma-separated):"), self.channel_ids_input)

        self.button_box = QHBoxLayout()
        self.ok_button = QPushButton(tr("OK"), self)  # i18n: a button
        self.ok_button.clicked.connect(self.accept)
        self.cancel_button = QPushButton(tr("Cancel"), self)  # i18n: a button
        self.cancel_button.clicked.connect(self.reject)
        self.button_box.addWidget(self.ok_button)
        self.button_box.addWidget(self.cancel_button)

        form_layout.addRow(self.button_box)

    def get_data(self):
        return (
            self.channel_name_input.text(),
            self.logo_url_input.text(),
            self.channel_ids_input.text(),
        )


class OptionsDialog(QDialog):
    def __init__(self, parent=None):
        super().__init__(parent)

        self.setWindowTitle(tr("Settings"))  # i18n: the Settings window's title

        self.config_manager = parent.config_manager
        self.provider_manager = parent.provider_manager
        self.epg_manager = parent.epg_manager
        self.providers = deepcopy(self.provider_manager.providers)
        self._original_providers = deepcopy(self.providers)
        self._original_provider_identities = {
            provider["name"]: self.provider_manager.provider_cache_identity(provider)
            for provider in self.providers
        }
        self.edited_provider = None
        self._refresh_provider_ids = set()
        self._verification_request = 0
        self._verification_why = ""  # the plain reason the verification on its way failed
        self._verification_insecure = False  # the provider it verifies answered only over HTTP
        self._media_access: dict = {}  # 2.8: a media server's sign-in on its way gave this
        self._verification_closed = False
        self.selected_provider_name = self.config_manager.selected_provider_name
        self.selected_provider_index = 0
        self.epg_settings_modified = False
        self.xmltv_mapping_modified = False
        self.providers_modified = False
        self._original_selected_provider_name = self.selected_provider_name

        for i in range(len(self.providers)):
            if self.providers[i]["name"] == self.config_manager.selected_provider_name:
                self.selected_provider_index = i
                break

        self.main_layout = QVBoxLayout(self)

        self.create_options_ui()

        self.save_button = QPushButton(tr("Save"), self)  # i18n: a button (verb)
        self.save_button.clicked.connect(self.save_settings)

        self.main_layout.addWidget(self.options_tab)
        self.main_layout.addWidget(self.save_button)

        self.load_providers()

    def create_options_ui(self):
        self.options_tab = QTabWidget(self)

        # Add tab with settings
        self.create_settings_ui()
        self.create_timeshift_ui()
        self.create_subtitles_ui()
        build_remote_tab(self)
        build_parental_tab(self)  # 2.8

        # Add tab with providers
        self.create_providers_ui()

        # Add tab with EPG settings
        self.create_epg_ui()

    def create_settings_ui(self):
        self.settings_tab = QWidget(self)
        # i18n: the Settings window's first tab (the general settings)
        self.options_tab.addTab(self.settings_tab, tr("Settings"))
        self.settings_layout = QFormLayout(self.settings_tab)

        # The language: every text is read as it is shown, so a new one shows from the next
        # start (a switch now would leave half the window in the old one).
        self.language_combo = QComboBox(self.settings_tab)
        languages = i18n.available_languages()
        system = dict(languages).get(i18n.system_language(), i18n.SOURCE_NAME)
        self.language_combo.addItem(tr("System default ({language})", language=system), "")
        for code, name in languages:
            self.language_combo.addItem(name, code)
        self.language_combo.setCurrentIndex(
            max(0, self.language_combo.findData(self.config_manager.language or ""))
        )
        self.language_note = QLabel(tr("Applies when QiTV starts again."), self.settings_tab)
        self.language_note.setEnabled(False)  # the theme's quieter text
        language_row = QHBoxLayout()
        language_row.addWidget(self.language_combo)
        language_row.addWidget(self.language_note)
        language_row.addStretch(1)
        self.settings_layout.addRow(tr("Language"), language_row)

        # Add check button to allow checking for updates
        self.check_updates_checkbox = QCheckBox(tr("Allow Check for Updates"), self.settings_tab)
        self.check_updates_checkbox.setChecked(self.config_manager.check_updates)
        self.check_updates_checkbox.stateChanged.connect(self.on_check_updates_toggled)
        self.settings_layout.addRow(self.check_updates_checkbox)

        # Add check button to enable channel logos
        self.channel_logos_checkbox = QCheckBox(tr("Enable Channel Logos"), self.settings_tab)
        self.channel_logos_checkbox.setChecked(self.config_manager.channel_logos)
        self.settings_layout.addRow(self.channel_logos_checkbox)

        # Add cache options
        self.cache_image_size_label = QLabel(self._cache_image_size_text(), self.settings_tab)
        self.cache_image_size_input = QLineEdit(self.settings_tab)
        self.cache_image_size_input.setText(str(self.config_manager.max_cache_image_size))
        self.settings_layout.addRow(self.cache_image_size_label, self.cache_image_size_input)

        self.clear_image_cache_button = QPushButton(tr("Clear Image Cache"), self.settings_tab)
        self.clear_image_cache_button.clicked.connect(self.clear_image_cache)
        self.settings_layout.addRow(self.clear_image_cache_button)

        # Network security options (2.7.1: HTTPS is chosen by what each provider answers)
        self.ssl_verify_checkbox = QCheckBox(
            tr("Verify SSL certificates (recommended)"), self.settings_tab
        )
        self.ssl_verify_checkbox.setToolTip(
            tr("Disable only if your provider uses self-signed certificates.")
        )
        self.ssl_verify_checkbox.setChecked(self.config_manager.ssl_verify)
        self.settings_layout.addRow(self.ssl_verify_checkbox)

        # TV playback
        self.live_latency_combo = QComboBox(self.settings_tab)
        for label, value in (
            # i18n: the Live latency setting's choices
            (tr("Auto (recommended)"), "auto"),
            (tr("Low"), "low"),  # i18n: the Live latency setting's choice (low latency)
            (tr("Normal"), "normal"),  # i18n: the Live latency setting's choice
            (tr("Stable"), "stable"),  # i18n: the Live latency setting's choice
        ):
            self.live_latency_combo.addItem(label, value)
        self.live_latency_combo.setCurrentIndex(
            max(0, self.live_latency_combo.findData(self.config_manager.live_latency))
        )
        self.live_latency_combo.setToolTip(
            tr(
                "Auto adjusts each channel: it starts at Normal and plays further behind on "
                "channels that run dry, coming back over time.\n"
                "Low starts live channels sooner; Stable buffers more for unreliable streams."
            )
        )
        # i18n: a setting: how far behind live a channel plays
        self.settings_layout.addRow(tr("Live latency"), self.live_latency_combo)

        self.hardware_decoding_checkbox = QCheckBox(
            tr("Hardware video decoding"), self.settings_tab
        )
        self.hardware_decoding_checkbox.setToolTip(
            tr(
                "Decode video with your graphics chip instead of the processor: it uses less CPU."
                " A channel whose picture freezes while its sound plays switches to software"
                " decoding by itself, for a day. Applies after QiTV restarts."
            )
        )
        self.hardware_decoding_checkbox.setChecked(self.config_manager.hardware_decoding)
        self.settings_layout.addRow(self.hardware_decoding_checkbox)

        # 2.8: Jellyfin and Emby servers: which version of a title plays, and when the server
        # converts it (never, by default: the file plays as it is).
        self.media_4k_combo = QComboBox(self.settings_tab)
        for text, value in (
            # i18n: a choice for a Jellyfin or Emby title's 4K version: it plays (rather than the
            # title's other version) on a 4K screen
            (tr("On a 4K screen"), "screen"),
            (tr("Always"), "always"),  # i18n: a choice: always play a title's 4K version
            (tr("Never"), "never"),  # i18n: a choice: never play a title's 4K version
        ):
            self.media_4k_combo.addItem(text, value)
        self.media_4k_combo.setCurrentIndex(
            max(0, self.media_4k_combo.findData(self.config_manager.media_4k_versions))
        )
        self.media_4k_combo.setToolTip(
            tr(
                "Jellyfin and Emby: when a title has a 4K version and another one, which plays."
                " Either plays as it is: below a 4K screen your computer scales 4K down, sharper"
                " but with more data. A title only in 4K plays unless you choose Never."
            )
        )
        # i18n: a setting (Jellyfin and Emby): when a title's 4K version plays
        self.settings_layout.addRow(tr("4K versions (Jellyfin, Emby)"), self.media_4k_combo)

        # 2.8: TMDB fills in what the Library's titles lack (services/tmdb.py).
        self.tmdb_checkbox = QCheckBox(tr("Film and series details from TMDB"), self.settings_tab)
        self.tmdb_checkbox.setToolTip(
            tr(
                "Where your provider gives a film or series no poster, story, year, running time,"
                " genres, rating or cast, the Library shows TMDB's. QiTV sends TMDB the title and"
                " its year, nothing about you."
            )
        )
        self.tmdb_checkbox.setChecked(bool(self.config_manager.tmdb_details))
        self.settings_layout.addRow(self.tmdb_checkbox)
        self.tmdb_token_input = QLineEdit(self.settings_tab)
        self.tmdb_token_input.setEchoMode(QLineEdit.EchoMode.Password)
        self.tmdb_token_input.setPlaceholderText(
            # i18n: the TMDB token field's hint in a build that carries one
            tr("Built in; or paste your own")
            if tmdb_built_in_token()
            # i18n: the TMDB token field's hint in a build without one (from the source code)
            else tr("Your TMDB API Read Access Token")
        )
        self.tmdb_token_input.setText(self.config_manager.tmdb_token)
        # i18n: a setting: the token QiTV uses with TMDB (an API Read Access Token)
        self.settings_layout.addRow(tr("TMDB token"), self.tmdb_token_input)

        # macOS only: another desktop than the fullscreen player's floats its picture.
        self.desktop_pip_checkbox = QCheckBox(
            tr("Float the picture when you leave a fullscreen desktop"), self.settings_tab
        )
        self.desktop_pip_checkbox.setToolTip(
            tr(
                "In fullscreen, switching to another desktop or app shows the picture in a small"
                " window on every desktop; back on the player's desktop it fills the screen again."
            )
        )
        self.desktop_pip_checkbox.setChecked(bool(self.config_manager.desktop_pip))
        if sys.platform == "darwin":
            self.settings_layout.addRow(self.desktop_pip_checkbox)
        else:
            self.desktop_pip_checkbox.hide()

        self.mouse_wheel_combo = QComboBox(self.settings_tab)
        for label, value in (
            (tr("Change channel"), "channel"),  # i18n: the Mouse wheel setting's choice
            (tr("Volume"), "volume"),  # i18n: the Mouse wheel setting's choice
        ):
            self.mouse_wheel_combo.addItem(label, value)
        self.mouse_wheel_combo.setCurrentIndex(
            max(0, self.mouse_wheel_combo.findData(self.config_manager.mouse_wheel_action))
        )
        self.mouse_wheel_combo.setToolTip(tr("Ctrl/⌘ + wheel always changes the volume."))
        # i18n: a setting: what the mouse wheel does over the picture
        self.settings_layout.addRow(tr("Mouse wheel"), self.mouse_wheel_combo)

        self.start_last_channel_checkbox = QCheckBox(
            tr("Start with the last channel"), self.settings_tab
        )
        self.start_last_channel_checkbox.setChecked(self.config_manager.start_with_last_channel)
        self.settings_layout.addRow(self.start_last_channel_checkbox)

        # External players as a mode: every channel opens there instead of the TV screen.
        self.play_in_vlc_checkbox = QCheckBox(tr("Always play in VLC"), self.settings_tab)
        self.play_in_mpv_checkbox = QCheckBox(tr("Always play in MPV"), self.settings_tab)
        self.play_in_vlc_checkbox.setChecked(self.config_manager.play_in_vlc)
        self.play_in_mpv_checkbox.setChecked(
            self.config_manager.play_in_mpv and not self.config_manager.play_in_vlc
        )
        for box, other in (
            (self.play_in_vlc_checkbox, self.play_in_mpv_checkbox),
            (self.play_in_mpv_checkbox, self.play_in_vlc_checkbox),
        ):
            # One player at most; both off plays on the TV screen.
            box.toggled.connect(lambda on, other=other: other.setChecked(False) if on else None)
            self.settings_layout.addRow(box)
        self.external_players_hint = QLabel(translate(EXTERNAL_PLAYERS_HINT), self.settings_tab)
        self.external_players_hint.setWordWrap(True)
        self.settings_layout.addRow(self.external_players_hint)

        # Auto-play settings section
        self.autoplay_section_label = QLabel(
            "<b>" + tr("Auto-Play Settings") + "</b>", self.settings_tab
        )
        self.settings_layout.addRow(self.autoplay_section_label)

        self.auto_play_episodes_checkbox = QCheckBox(
            tr("Auto-play next episode (10s countdown)"), self.settings_tab
        )
        self.auto_play_episodes_checkbox.setToolTip(
            tr(
                "Automatically play the next episode when one ends. "
                "Shows a 10-second countdown to cancel."
            )
        )
        self.auto_play_episodes_checkbox.setChecked(self.config_manager.auto_play_episodes)
        self.settings_layout.addRow(self.auto_play_episodes_checkbox)

        self.auto_play_movies_checkbox = QCheckBox(
            tr("Suggest next movie from category (10s countdown)"), self.settings_tab
        )
        self.auto_play_movies_checkbox.setToolTip(
            tr(
                "After a movie ends, suggest another from the same category. Shows a 10-second"
                " countdown."
            )
        )
        self.auto_play_movies_checkbox.setChecked(self.config_manager.auto_play_movies)
        self.settings_layout.addRow(self.auto_play_movies_checkbox)

    def create_subtitles_ui(self):
        """OpenSubtitles: the API key and an optional sign-in, masked (never logged)."""
        tab = QWidget(self)
        tab.setObjectName("subtitlesSettings")
        self.options_tab.addTab(tab, tr("Subtitles"))  # i18n: a tab of the Settings window
        layout = QFormLayout(tab)
        intro = QLabel(opensubtitles_hint(), tab)
        intro.setWordWrap(True)
        intro.setOpenExternalLinks(True)
        intro.setTextFormat(Qt.TextFormat.RichText)
        layout.addRow(intro)
        built_in = bool(built_in_key())
        if built_in:
            note = QLabel(translate(OPENSUBTITLES_BUILT_IN_HINT), tab)
            note.setWordWrap(True)
            layout.addRow(note)
        self.opensubtitles_key_input = QLineEdit(tab)
        self.opensubtitles_key_input.setEchoMode(QLineEdit.EchoMode.Password)
        if built_in:
            placeholder = tr("Optional: your own key")  # i18n: the API key field's placeholder
        else:
            placeholder = tr("Paste your API key")  # i18n: the API key field's placeholder
        self.opensubtitles_key_input.setPlaceholderText(placeholder)
        self.opensubtitles_key_input.setText(self.config_manager.opensubtitles_api_key)
        layout.addRow(tr("API key"), self.opensubtitles_key_input)
        sign_in = QLabel(opensubtitles_sign_in_hint(), tab)
        sign_in.setWordWrap(True)
        layout.addRow(sign_in)
        self.opensubtitles_username_input = QLineEdit(tab)
        self.opensubtitles_username_input.setText(self.config_manager.opensubtitles_username)
        layout.addRow(tr("Username"), self.opensubtitles_username_input)
        self.opensubtitles_password_input = QLineEdit(tab)
        self.opensubtitles_password_input.setEchoMode(QLineEdit.EchoMode.Password)
        self.opensubtitles_password_input.setText(self.config_manager.opensubtitles_password)
        layout.addRow(tr("Password"), self.opensubtitles_password_input)
        # i18n: a button: forget the OpenSubtitles username and password
        self.opensubtitles_sign_out_button = QPushButton(tr("Sign out"), tab)
        self.opensubtitles_sign_out_button.clicked.connect(self.sign_out_of_opensubtitles)
        layout.addRow(self.opensubtitles_sign_out_button)

    def sign_out_of_opensubtitles(self):
        """Sign out: the username and password go (saved with Save, which ends the sign-in)."""
        self.opensubtitles_username_input.clear()
        self.opensubtitles_password_input.clear()

    def create_timeshift_ui(self):
        tab = QWidget(self)
        tab.setObjectName("timeshiftSettings")
        self.options_tab.addTab(tab, tr("Time-shift"))  # i18n: a tab of the Settings window
        layout = QVBoxLayout(tab)
        self.timeshift_checkbox = QCheckBox(
            tr(
                "Use disk for a longer time-shift buffer.\n"
                "A channel you leave keeps its buffer for up to 24 hours, after a restart too; "
                "when the cache fills, the oldest goes first."
            ),
            tab,
        )
        self.timeshift_checkbox.setObjectName("timeshiftEnabled")
        self.timeshift_checkbox.setChecked(self.config_manager.timeshift_enabled)
        layout.addWidget(self.timeshift_checkbox)

        # Every stream not on the disk ring: without disk, and the small picture always.
        memory = QGroupBox(tr("Memory per stream"), tab)  # i18n: a group's title
        memory_layout = QFormLayout(memory)
        self.timeshift_ram_combo = QComboBox(memory)
        self.timeshift_ram_combo.setObjectName("timeshiftMemoryPerStream")
        self.timeshift_ram_combo.setAccessibleName(tr("Time-shift memory per stream"))
        for mib in ConfigManager.TIMESHIFT_RAM_CHOICES:
            self.timeshift_ram_combo.addItem(_mib_text(mib), mib)
        self.timeshift_ram_combo.setCurrentIndex(
            self.timeshift_ram_combo.findData(self.config_manager.timeshift_ram_mib)
        )
        self.timeshift_ram_estimate = QLabel(memory)
        memory_layout.addRow(tr("Memory per stream:"), self.timeshift_ram_combo)
        # i18n: how far back the time-shift buffer reaches
        memory_layout.addRow(tr("Estimated rewind:"), self.timeshift_ram_estimate)
        self.timeshift_ram_combo.currentIndexChanged.connect(self._update_ram_estimate)
        self._update_ram_estimate()
        layout.addWidget(memory)

        cache = QGroupBox(tr("Disk cache"), tab)  # i18n: a group's title
        cache_layout = QVBoxLayout(cache)
        self.timeshift_size_label = QLabel(cache)
        self.timeshift_size_label.setObjectName("timeshiftCacheSizeLabel")
        cache_layout.addWidget(self.timeshift_size_label)

        self.timeshift_size_slider = QSlider(Qt.Orientation.Horizontal, cache)
        self.timeshift_size_slider.setObjectName("timeshiftCacheSize")
        self.timeshift_size_slider.setAccessibleName(tr("Time-shift disk cache size"))
        self.timeshift_size_slider.setRange(256, 16384)
        self.timeshift_size_slider.setSingleStep(256)
        self.timeshift_size_slider.setPageStep(1024)
        self.timeshift_size_slider.setTickInterval(1024)
        self.timeshift_size_slider.setTickPosition(QSlider.TickPosition.TicksBelow)
        self.timeshift_size_slider.setValue(self.config_manager.timeshift_max_mib)
        cache_layout.addWidget(self.timeshift_size_slider)
        scale = QHBoxLayout()
        scale.addWidget(QLabel(_mib_text(256), cache))
        scale.addStretch()
        scale.addWidget(QLabel(_gib_text("16"), cache))
        cache_layout.addLayout(scale)

        cache_layout.addWidget(QLabel("<b>" + tr("Estimated rewind") + "</b>", cache))
        estimates = QFormLayout()
        self.timeshift_1080p_estimate = QLabel(cache)
        self.timeshift_4k_estimate = QLabel(cache)
        # i18n: an example video stream: its resolution and bitrate (megabits per second)
        estimates.addRow(tr("1080p at 8 Mb/s:"), self.timeshift_1080p_estimate)
        estimates.addRow(tr("4K at 25 Mb/s:"), self.timeshift_4k_estimate)
        cache_layout.addLayout(estimates)
        assumptions = QLabel(
            tr(
                "These are example bitrates, not quality settings. Actual rewind time "
                "depends on the channel's bitrate, codec and audio tracks. "
                "Video and audio are not re-encoded."
            ),
            cache,
        )
        assumptions.setWordWrap(True)
        cache_layout.addWidget(assumptions)
        self.timeshift_size_slider.valueChanged.connect(self._update_timeshift_estimates)
        self._update_timeshift_estimates(self.timeshift_size_slider.value())
        cache.setEnabled(self.timeshift_checkbox.isChecked())
        self.timeshift_checkbox.toggled.connect(cache.setEnabled)
        layout.addWidget(cache)

        layout.addWidget(self._recordings_group(tab))

        explanation = QLabel(
            tr(
                "Without disk, each picture keeps its memory per stream (picture-in-picture's "
                "small picture always does), and only the channel left last keeps its buffer."
                "\n\n"
                "This is temporary playback history, not a permanent recording. The cache size "
                "is the total for every channel's buffer: when it fills, the oldest content goes "
                "first; anything older than 24 hours goes too. The buffer keeps filling while "
                "paused or rewound, and stops when you leave the channel.\n\n"
                "After Save, enabling and size changes apply on the next channel you open; "
                "turning the disk off removes what it kept."
            ),
            tab,
        )
        explanation.setWordWrap(True)
        layout.addWidget(explanation)
        layout.addStretch()

    def _recordings_group(self, tab: QWidget) -> QGroupBox:
        """Where recordings go (2.3; the optional config key `recordings_dir`, "" for the
        system's Movies or Videos folder's QiTV): Change…, Use the default, Open folder."""
        group = QGroupBox(tr("Recordings"), tab)  # i18n: a group's title: recorded channels
        group.setObjectName("recordingsSettings")
        box = QVBoxLayout(group)
        self._recordings_dir: str = self.config_manager.recordings_dir
        self.recordings_folder = QLineEdit(group)
        self.recordings_folder.setObjectName("recordingsFolder")
        self.recordings_folder.setReadOnly(True)
        # i18n: the accessible name of the Recordings folder's field in Settings
        self.recordings_folder.setAccessibleName(tr("Recordings folder"))
        box.addWidget(self.recordings_folder)
        buttons = QHBoxLayout()
        self.recordings_change_button = QPushButton(tr("Change…"), group)  # i18n: a button
        self.recordings_change_button.clicked.connect(self.choose_recordings_folder)
        # i18n: a button: the Recordings folder goes back to the system's Movies (Videos) folder
        self.recordings_default_button = QPushButton(tr("Use the default"), group)
        self.recordings_default_button.clicked.connect(lambda: self._set_recordings_dir(""))
        # i18n: a button: show the Recordings folder in the system's file manager
        self.recordings_open_button = QPushButton(tr("Open folder"), group)
        self.recordings_open_button.clicked.connect(self.open_recordings_folder)
        for button in (
            self.recordings_change_button,
            self.recordings_default_button,
            self.recordings_open_button,
        ):
            buttons.addWidget(button)
        buttons.addStretch()
        box.addLayout(buttons)
        self.recordings_status = QLabel("", group)  # why Open folder could not open it
        self.recordings_status.setObjectName("recordingsStatus")
        box.addWidget(self.recordings_status)
        note = QLabel(
            tr(
                "R or the record button on the player controls records the live channel you "
                "watch, from where you watch it, into this folder. Only channels with "
                "time-shift can be recorded; a recording needs no second stream."
            ),
            group,
        )
        note.setWordWrap(True)
        box.addWidget(note)
        self._set_recordings_dir(self._recordings_dir)
        return group

    def _set_recordings_dir(self, path: str) -> None:
        self._recordings_dir = path
        self.recordings_folder.setText(recording.folder(self))
        self.recordings_default_button.setEnabled(bool(path))
        self.recordings_status.setText("")

    @property
    def recordings_dir(self) -> str:
        """The folder chosen here ("": the default), as `recording.folder` reads it."""
        return self._recordings_dir

    def choose_recordings_folder(self) -> None:
        chosen = QFileDialog.getExistingDirectory(
            self,
            tr("Choose the Recordings folder"),  # i18n: the title of a folder picker
            recording.folder(self),
        )
        if chosen:
            self._set_recordings_dir(os.path.normpath(chosen))

    def open_recordings_folder(self) -> None:
        """The folder shown, in the system's file manager (saved or not)."""
        opened = QDesktopServices.openUrl(QUrl.fromLocalFile(recording.folder(self)))
        self.recordings_status.setText("" if opened else translate(recording.NO_RECORDINGS))

    def _update_ram_estimate(self, *_args) -> None:
        mib = (
            self.timeshift_ram_combo.currentData() or ConfigManager.DEFAULT_OPTION_TIMESHIFT_RAM_MIB
        )
        minutes = max(1, round(mib * 1024 * 1024 * 8 / (8 * 1_000_000 * 60)))
        # i18n: how far back the memory reaches: n minutes (abbreviated) of HD video at 8 Mb/s
        self.timeshift_ram_estimate.setText(tr("≈ {n} min of HD at 8 Mb/s", n=minutes))

    def _update_timeshift_estimates(self, mib):
        if mib < 1024:
            size = _mib_text(mib)
        else:
            size = _gib_text(i18n.decimal(f"{mib / 1024:.2f}".rstrip("0").rstrip(".")))
        # i18n: size as "512 MiB" or "1.5 GiB"
        self.timeshift_size_label.setText("<b>" + tr("Cache size: {size}", size=size) + "</b>")
        self.timeshift_size_label.setToolTip(
            # i18n: size is a number of mebibytes, as "4,096"
            tr("{size} MiB of disk space, not RAM.", size=i18n.number(mib))
        )
        for bitrate, label in (
            (8, self.timeshift_1080p_estimate),
            (25, self.timeshift_4k_estimate),
        ):
            minutes = max(1, round(mib * 1024 * 1024 * 8 / (bitrate * 1_000_000 * 60)))
            hours, remainder = divmod(minutes, 60)
            if hours:
                # i18n: an estimated rewind: h hours and m minutes (abbreviated)
                label.setText(tr("About {h} h {m} min", h=hours, m=remainder))
            else:
                # i18n: an estimated rewind: m minutes (abbreviated)
                label.setText(tr("About {m} min", m=minutes))

    def create_providers_ui(self):
        self.providers_tab = QWidget(self)
        self.options_tab.addTab(self.providers_tab, tr("Providers"))  # i18n: a Settings tab
        self.providers_layout = QFormLayout(self.providers_tab)

        self.provider_label = QLabel(tr("Select Provider:"), self.providers_tab)
        self.provider_combo = QComboBox(self.providers_tab)
        self.provider_combo.currentIndexChanged.connect(self.load_provider_settings)
        self.providers_layout.addRow(self.provider_label, self.provider_combo)

        self.add_provider_button = QPushButton(tr("Add Provider"), self.providers_tab)
        self.add_provider_button.clicked.connect(self.add_new_provider)
        self.providers_layout.addWidget(self.add_provider_button)

        # i18n: a Settings button beside Add Provider; opens a menu of free playlists
        self.free_channels_button = QPushButton(tr("Free channels…"), self.providers_tab)
        self.free_channels_button.clicked.connect(self.show_free_channels)
        self.providers_layout.addWidget(self.free_channels_button)

        self.remove_provider_button = QPushButton(tr("Remove Provider"), self.providers_tab)
        self.remove_provider_button.clicked.connect(self.remove_provider)
        self.providers_layout.addWidget(self.remove_provider_button)

        self.name_label = QLabel(tr("Name:"), self.providers_tab)  # i18n: a provider's name
        self.name_input = QLineEdit(self.providers_tab)
        self.providers_layout.addRow(self.name_label, self.name_input)

        self.create_stream_type_ui()

        self.url_label = QLabel(tr("Server URL:"), self.providers_tab)
        self.url_input = QLineEdit(self.providers_tab)
        # One link from the provider fills the form: this button, or a link pasted here.
        self.paste_link_button = QPushButton(translate(PASTE_LINK_TEXT), self.providers_tab)
        self.paste_link_button.setToolTip(translate(PASTE_LINK_TIP))
        self.paste_link_button.clicked.connect(self.paste_link)
        url_row = QHBoxLayout()
        url_row.setContentsMargins(0, 0, 0, 0)
        url_row.addWidget(self.url_input, 1)
        url_row.addWidget(self.paste_link_button)
        self.providers_layout.addRow(self.url_label, url_row)
        self._url_before = ""  # the address before the edit textEdited reports
        self.url_input.textEdited.connect(self._on_url_edited)
        self.url_input.textChanged.connect(self._remember_url)

        self.mac_label = QLabel(tr("MAC Address:"), self.providers_tab)
        self.mac_input = QLineEdit(self.providers_tab)
        self.providers_layout.addRow(self.mac_label, self.mac_input)

        self.serial_label = QLabel(tr("Serial Number (Optional):"), self.providers_tab)
        self.serial_input = QLineEdit(self.providers_tab)
        # i18n: an optional field's placeholder
        self.serial_input.setPlaceholderText(tr("Leave empty if not required"))
        self.providers_layout.addRow(self.serial_label, self.serial_input)

        self.device_id_label = QLabel(tr("Device ID (Optional):"), self.providers_tab)
        self.device_id_input = QLineEdit(self.providers_tab)
        self.device_id_input.setPlaceholderText(tr("Leave empty if not required"))
        self.providers_layout.addRow(self.device_id_label, self.device_id_input)

        # i18n: a button: choose a playlist file (verb)
        self.file_button = QPushButton(tr("Load File"), self.providers_tab)
        self.file_button.clicked.connect(self.load_file)
        self.providers_layout.addWidget(self.file_button)

        self.username_label = QLabel(tr("Username:"), self.providers_tab)
        self.username_input = QLineEdit(self.providers_tab)
        self.providers_layout.addRow(self.username_label, self.username_input)

        self.password_label = QLabel(tr("Password:"), self.providers_tab)
        self.password_input = QLineEdit(self.providers_tab)
        self.providers_layout.addRow(self.password_label, self.password_input)

        # Per-provider network preferences
        self.provider_ssl_verify_checkbox = QCheckBox(
            tr("Verify SSL certificates for this provider"), self.providers_tab
        )
        self.providers_layout.addRow(self.provider_ssl_verify_checkbox)

        self.max_streams_label = QLabel(tr("Max simultaneous streams:"), self.providers_tab)
        self.max_streams_input = QSpinBox(self.providers_tab)
        self.max_streams_input.setRange(0, 10)
        # i18n: the Max simultaneous streams setting's value for "as the provider reports"
        self.max_streams_input.setSpecialValueText(tr("Automatic"))
        self.max_streams_input.setToolTip(
            tr(
                "How many streams this provider allows at once. Automatic uses what the provider "
                "reports."
            )
        )
        self.providers_layout.addRow(self.max_streams_label, self.max_streams_input)

        # 2.2: a guide whose times are off by whole hours moves by this (an optional key).
        self.guide_offset_label = QLabel(tr("Guide time offset:"), self.providers_tab)
        self.guide_offset_combo = QComboBox(self.providers_tab)
        for minutes in range(-GUIDE_OFFSET_MAX, GUIDE_OFFSET_MAX + 1, GUIDE_OFFSET_STEP):
            self.guide_offset_combo.addItem(offset_text(minutes), minutes)
        self.guide_offset_combo.setCurrentIndex(self.guide_offset_combo.findData(0))
        self.guide_offset_combo.setToolTip(translate(GUIDE_OFFSET_TIP))
        self.providers_layout.addRow(self.guide_offset_label, self.guide_offset_combo)

        self.verify_apply_group = QWidget(self.providers_tab)
        # i18n: a button: check that the provider answers (verb)
        self.verify_button = QPushButton(tr("Verify Provider"), self.verify_apply_group)
        self.verify_button.clicked.connect(self.verify_provider)
        # i18n: a button: keep this provider's edits (verb)
        self.apply_button = QPushButton(tr("Apply Change"), self.verify_apply_group)
        self.apply_button.clicked.connect(self.apply_provider)
        verify_apply_layout = QHBoxLayout(self.verify_apply_group)
        verify_apply_layout.addWidget(self.verify_button)
        verify_apply_layout.addWidget(self.apply_button)
        self.verify_result = QLabel("", self.providers_tab)
        self.providers_layout.addWidget(self.verify_apply_group)
        self.providers_layout.addWidget(self.verify_result)

    def create_stream_type_ui(self):
        self.type_label = QLabel(tr("Stream Type:"), self)  # i18n: a provider's kind
        self.type_group = QButtonGroup(self)
        self.type_STB = QRadioButton("STB", self)
        self.type_M3UPLAYLIST = QRadioButton(tr("M3U Playlist"), self)
        self.type_M3USTREAM = QRadioButton(tr("M3U Stream"), self)
        self.type_XTREAM = QRadioButton("Xtream", self)
        self.type_JELLYFIN = QRadioButton("Jellyfin", self)  # 2.8: media servers
        self.type_EMBY = QRadioButton("Emby", self)
        for button in (
            self.type_STB,
            self.type_M3UPLAYLIST,
            self.type_M3USTREAM,
            self.type_XTREAM,
            self.type_JELLYFIN,
            self.type_EMBY,
        ):
            self.type_group.addButton(button)
            button.toggled.connect(self.update_inputs)

        grid_layout = QGridLayout()
        grid_layout.addWidget(self.type_STB, 0, 0)
        grid_layout.addWidget(self.type_M3UPLAYLIST, 0, 1)
        grid_layout.addWidget(self.type_M3USTREAM, 1, 0)
        grid_layout.addWidget(self.type_XTREAM, 1, 1)
        grid_layout.addWidget(self.type_JELLYFIN, 2, 0)
        grid_layout.addWidget(self.type_EMBY, 2, 1)
        self.providers_layout.addRow(self.type_label, grid_layout)

    def create_epg_ui(self):
        self.epg_tab = QWidget(self)
        self.options_tab.addTab(self.epg_tab, "EPG")
        self.epg_layout = QFormLayout(self.epg_tab)

        # Add EPG settings
        self.epg_source_label = QLabel(tr("EPG Source"))  # i18n: where the guide comes from
        self.epg_source_combo = QComboBox()
        # Shown in the language, saved as the config's value (the item's data).
        for source, label in (
            ("No Source", tr("No Source")),  # i18n: the EPG Source setting's choice: no guide
            ("STB", "STB"),
            ("Local File", tr("Local File")),  # i18n: the EPG Source setting's choice
            ("URL", tr("URL")),  # i18n: the EPG Source setting's choice: a web address
        ):
            self.epg_source_combo.addItem(label, source)
        source_index = self.epg_source_combo.findData(self.config_manager.epg_source)
        if source_index >= 0:
            self.epg_source_combo.setCurrentIndex(source_index)
        self.epg_source_combo.currentIndexChanged.connect(self.on_epg_source_changed)
        self.epg_layout.addRow(self.epg_source_label)
        self.epg_layout.addRow(self.epg_source_combo)

        self.epg_url_label = QLabel(tr("EPG URL"))
        self.epg_url_input = QLineEdit()
        self.epg_url_input.setText(self.config_manager.epg_url)
        self.epg_layout.addRow(self.epg_url_label)
        self.epg_layout.addRow(self.epg_url_input)

        self.epg_file_label = QLabel(tr("EPG File"))
        self.epg_file_input = QLineEdit()
        self.epg_file_input.setText(self.config_manager.epg_file)
        self.epg_file_button = QPushButton(tr("Browse"))  # i18n: a button: choose a file (verb)
        self.epg_file_button.clicked.connect(self.browse_epg_file)
        self.epg_layout.addRow(self.epg_file_label)
        self.epg_layout.addRow(self.epg_file_input)
        self.epg_layout.addRow(self.epg_file_button)

        # Add expiring EPG settings
        self.epg_expiration_layout = QHBoxLayout()
        # i18n: before a number and a unit: "Check update every [2] [Hours]"
        self.epg_expiration_label = QLabel(tr("Check update every"))
        self.epg_expiration_spinner = QSpinBox()
        self.epg_expiration_spinner.setValue(self.config_manager.epg_expiration_value)
        self.epg_expiration_spinner.setMinimum(1)
        self.epg_expiration_spinner.setMaximum(9999)
        self.epg_expiration_spinner.setSingleStep(1)
        self.epg_expiration_combo = QComboBox()
        unit = self.config_manager.epg_expiration_unit
        units = list(ConfigManager.EPG_EXPIRATION_CHOICES)
        if unit not in units and unit in ConfigManager.EPG_EXPIRATION_SECONDS:
            units.insert(0, unit)  # "Minutes" from an older version stays as it was
        for choice in units:  # shown in the language, saved as the config's value
            self.epg_expiration_combo.addItem(epg_unit_label(choice), choice)
        unit_index = self.epg_expiration_combo.findData(unit)
        if unit_index >= 0:
            self.epg_expiration_combo.setCurrentIndex(unit_index)
        self.epg_expiration_layout.addWidget(self.epg_expiration_label)
        self.epg_expiration_layout.addWidget(self.epg_expiration_spinner)
        self.epg_expiration_layout.addWidget(self.epg_expiration_combo)
        self.epg_layout.addRow(self.epg_expiration_layout)

        # Create a vertical layout for the settings, XMLTV mapping table, and buttons
        self.xmltv_group_widget = QWidget()
        self.xmltv_group_layout = QVBoxLayout(self.xmltv_group_widget)
        self.xmltv_group_label = QLabel(tr("XMLTV Mapping"))
        self.xmltv_group_layout.addWidget(self.xmltv_group_label)

        # XMLTV mapping table
        self.xmltv_mapping_table = QTableWidget(self.xmltv_group_widget)
        self.xmltv_mapping_table.setSelectionBehavior(QAbstractItemView.SelectRows)
        self.xmltv_mapping_table.setSelectionMode(QAbstractItemView.ExtendedSelection)
        self.xmltv_mapping_table.setColumnCount(3)
        self.xmltv_mapping_table.setHorizontalHeaderLabels(
            # i18n: the XMLTV mapping table's columns
            [tr("Channel Name"), tr("Logo URL"), tr("Channel IDs")]
        )
        self.xmltv_mapping_table.horizontalHeader().setSectionResizeMode(
            0, QHeaderView.ResizeToContents
        )
        self.xmltv_mapping_table.horizontalHeader().setSectionResizeMode(1, QHeaderView.Stretch)
        self.xmltv_mapping_table.horizontalHeader().setSectionResizeMode(2, QHeaderView.Stretch)
        self.xmltv_mapping_table.setEditTriggers(QAbstractItemView.NoEditTriggers)
        self.xmltv_group_layout.addWidget(self.xmltv_mapping_table)

        self.load_xmltv_channel_mapping()

        # Create a horizontal layout for the buttons
        self.xmltv_buttons_layout = QHBoxLayout()
        # i18n: the XMLTV mapping table's buttons (verbs)
        self.add_xmltv_mapping_button = QPushButton(tr("Add"))
        self.add_xmltv_mapping_button.clicked.connect(self.add_xmltv_mapping)
        self.xmltv_buttons_layout.addWidget(self.add_xmltv_mapping_button)

        self.edit_xmltv_mapping_button = QPushButton(tr("Edit"))  # i18n: a button (verb)
        self.edit_xmltv_mapping_button.clicked.connect(self.edit_xmltv_mapping)
        self.xmltv_buttons_layout.addWidget(self.edit_xmltv_mapping_button)

        self.delete_xmltv_mapping_button = QPushButton(tr("Delete"))  # i18n: a button (verb)
        self.delete_xmltv_mapping_button.clicked.connect(self.delete_xmltv_mapping)
        self.xmltv_buttons_layout.addWidget(self.delete_xmltv_mapping_button)

        self.import_xmltv_mapping_button = QPushButton(tr("Import"))  # i18n: a button (verb)
        self.import_xmltv_mapping_button.clicked.connect(self.import_xmltv_mapping)
        self.xmltv_buttons_layout.addWidget(self.import_xmltv_mapping_button)

        self.export_xmltv_mapping_button = QPushButton(tr("Export"))  # i18n: a button (verb)
        self.export_xmltv_mapping_button.clicked.connect(self.export_xmltv_mapping)
        self.xmltv_buttons_layout.addWidget(self.export_xmltv_mapping_button)

        # Add the horizontal layout to the vertical layout
        self.xmltv_group_layout.addLayout(self.xmltv_buttons_layout)

        self.epg_layout.addRow(self.xmltv_group_widget)

        # EPG list window (hours; 0 = unlimited)
        self.epg_list_window_label = QLabel(tr("EPG List Window (hours; 0 = unlimited)"))
        self.epg_list_window_spinner = QSpinBox()
        self.epg_list_window_spinner.setMinimum(0)
        self.epg_list_window_spinner.setMaximum(168)
        try:
            self.epg_list_window_spinner.setValue(int(self.config_manager.epg_list_window_hours))
        except Exception:
            self.epg_list_window_spinner.setValue(24)
        self.epg_layout.addRow(self.epg_list_window_label, self.epg_list_window_spinner)

        # STB EPG fetch period (server period in hours)
        self.epg_stb_period_label = QLabel(tr("STB EPG Server Fetch Period (hours)"))
        self.epg_stb_period_spinner = QSpinBox()
        self.epg_stb_period_spinner.setMinimum(1)
        self.epg_stb_period_spinner.setMaximum(168)
        try:
            self.epg_stb_period_spinner.setValue(int(self.config_manager.epg_stb_period_hours))
        except Exception:
            self.epg_stb_period_spinner.setValue(5)
        self.epg_layout.addRow(self.epg_stb_period_label, self.epg_stb_period_spinner)

        # Create widget groups for EPG source visibility toggling
        self._epg_url_group = WidgetGroup(self.epg_url_label, self.epg_url_input)
        self._epg_file_group = WidgetGroup(
            self.epg_file_label, self.epg_file_input, self.epg_file_button
        )
        self._epg_expiration_group = WidgetGroup(
            self.epg_expiration_label, self.epg_expiration_spinner, self.epg_expiration_combo
        )
        self._epg_stb_period_group = WidgetGroup(
            self.epg_stb_period_label, self.epg_stb_period_spinner
        )

        # Initial call to set visibility based on the current selection
        # (after all dependent widgets are created)
        self.on_epg_source_changed()

    def load_providers(self):
        self.provider_combo.blockSignals(True)
        self.provider_combo.clear()
        for i, provider in enumerate(self.providers):
            # can we get the first couple ... last couple of characters of the name?
            prov = (
                provider["name"][:30] + "..." + provider["name"][-15:]
                if len(provider["name"]) > 45
                else provider["name"]
            )
            self.provider_combo.addItem(f"{i + 1}: {prov}", userData=provider)
        self.provider_combo.setCurrentIndex(self.selected_provider_index)
        self.provider_combo.blockSignals(False)
        self.load_provider_settings(self.selected_provider_index)

    def load_provider_settings(self, index):
        if index == -1 or index >= len(self.providers):
            return
        if self.edited_provider is not None and self.providers[index] is not self.edited_provider:
            self._store_provider_edits()
        self._verification_request += 1
        self.verify_button.setEnabled(True)
        self.verify_result.clear()
        self.selected_provider_name = self.providers[index].get(
            "name", self.providers[index].get("url", "")
        )
        self.selected_provider_index = index
        self.edited_provider = self.providers[index]
        self.name_input.setText(self.edited_provider.get("name", ""))
        self.url_input.setText(self.edited_provider.get("url", ""))
        self.mac_input.setText(self.edited_provider.get("mac", ""))
        self.serial_input.setText(self.edited_provider.get("serial_number", ""))
        self.device_id_input.setText(self.edited_provider.get("device_id", ""))
        self.username_input.setText(self.edited_provider.get("username", ""))
        self.password_input.setText(self.edited_provider.get("password", ""))
        # Set per-provider network preferences with global fallbacks
        self.provider_ssl_verify_checkbox.setChecked(
            self.edited_provider.get("ssl_verify", self.ssl_verify_checkbox.isChecked())
        )
        try:
            max_streams = int(self.edited_provider.get("max_streams") or 0)
        except TypeError, ValueError, OverflowError:
            max_streams = 0  # a hand-edited bad value means Automatic
        self.max_streams_input.setValue(max(0, min(10, max_streams)))
        self._loaded_max_streams = self.max_streams_input.value()
        offset = guide_offset_minutes(self.edited_provider)
        self.guide_offset_combo.setCurrentIndex(self.guide_offset_combo.findData(offset))
        self._loaded_provider_network = {
            "ssl_verify": self.provider_ssl_verify_checkbox.isChecked(),
        }
        self.update_radio_buttons()
        self.update_inputs()

    def on_epg_source_changed(self):
        epg_source = self.epg_source_combo.currentData()  # the config's value, never the label

        # Hide all EPG-related widget groups by default
        self._epg_url_group.hide()
        self._epg_file_group.hide()
        self._epg_expiration_group.hide()
        self._epg_stb_period_group.hide()
        self.xmltv_group_widget.hide()

        # Show relevant widgets based on EPG source
        if epg_source == "URL":
            self._epg_url_group.show()
            self._epg_expiration_group.show()
            self.xmltv_group_widget.show()
        elif epg_source == "Local File":
            self._epg_file_group.show()
            self.xmltv_group_widget.show()
        elif epg_source == "STB":
            self._epg_expiration_group.show()
            self._epg_stb_period_group.show()

    def update_radio_buttons(self):
        provider_type = self.edited_provider.get("type", "")
        self.type_STB.setChecked(provider_type == "STB")
        self.type_M3UPLAYLIST.setChecked(provider_type == "M3UPLAYLIST")
        self.type_M3USTREAM.setChecked(provider_type == "M3USTREAM")
        self.type_XTREAM.setChecked(provider_type == "XTREAM")
        self.type_JELLYFIN.setChecked(provider_type == JELLYFIN)
        self.type_EMBY.setChecked(provider_type == EMBY)

    def _media_server_checked(self) -> str:
        """JELLYFIN or EMBY when one of them is the type chosen, else "" (2.8)."""
        if self.type_JELLYFIN.isChecked():
            return JELLYFIN
        return EMBY if self.type_EMBY.isChecked() else ""

    def update_inputs(self):
        self.mac_label.setVisible(self.type_STB.isChecked())
        self.mac_input.setVisible(self.type_STB.isChecked())
        self.serial_label.setVisible(self.type_STB.isChecked())
        self.serial_input.setVisible(self.type_STB.isChecked())
        self.device_id_label.setVisible(self.type_STB.isChecked())
        self.device_id_input.setVisible(self.type_STB.isChecked())
        self.file_button.setVisible(
            self.type_M3UPLAYLIST.isChecked() or self.type_M3USTREAM.isChecked()
        )

        self.url_input.setEnabled(True)

        # A media server signs in with them (Verify); its password is not kept (2.8).
        signs_in = self.type_XTREAM.isChecked() or bool(self._media_server_checked())
        self.username_label.setVisible(signs_in)
        self.username_input.setVisible(signs_in)
        self.password_label.setVisible(signs_in)
        self.password_input.setVisible(signs_in)

    def add_new_provider(self):
        self._store_provider_edits()
        new_provider = {
            "type": "STB",
            "name": "",
            "url": "",
            "mac": "",
            "serial_number": "",
            "device_id": "",
        }
        self.providers.append(new_provider)
        self.selected_provider_index = len(self.providers) - 1
        self.edited_provider = None
        self.load_providers()
        self.providers_modified = True

    def show_free_channels(self):
        """Free channels…: iptv-org's lists in the menu under the button (as the TV's)."""
        button = self.free_channels_button
        below = button.mapToGlobal(button.rect().bottomLeft())
        show_free_channels_menu(self, below, self.add_free_channels)

    def add_free_channels(self, code):
        """List iptv-org's list (all countries, or `code`'s) as a provider, shown in the form;
        one listed already is only shown. Saved with Save, as any provider added here."""
        entry = free_channels.provider_entry(code)
        self._store_provider_edits()
        index = next(
            (
                i
                for i, listed in enumerate(self.providers)
                if free_channels.same_list(listed, entry)
            ),
            None,
        )
        if index is None:
            entry["name"] = unique_name(entry["name"], (p.get("name") for p in self.providers))
            self.providers.append(entry)
            index = len(self.providers) - 1
            self.providers_modified = True
        self.selected_provider_index = index
        self.edited_provider = None
        self.load_providers()

    def start_new_provider(self):
        """Open on the Providers tab with a new, empty provider to fill in (TV: Add provider)."""
        self.options_tab.setCurrentWidget(self.providers_tab)
        self.add_new_provider()
        self.name_input.setFocus()

    def remove_provider(self):
        if len(self.providers) == 1:
            return
        del self.providers[self.provider_combo.currentIndex()]
        self.edited_provider = None
        self.selected_provider_index = min(self.selected_provider_index, len(self.providers) - 1)
        self.load_providers()
        self.providers_modified = True

    def _manager_of(self, name):
        """The provider's own ProviderManager when its catalog exists (it holds the provider's
        queued cache writes), else the list's: a catalog is never made just for this."""
        catalogs = self.parent().catalogs
        if name in catalogs.created():
            return catalogs.catalog(name).provider_manager
        return self.provider_manager

    def browse_epg_file(self):
        file_path, _ = QFileDialog.getOpenFileName(self)
        if file_path:
            self.epg_file_input.setText(file_path)

    def save_settings(self):
        self._store_provider_edits()
        new_identities = {
            provider["name"]: self.provider_manager.provider_cache_identity(
                provider, ssl_verify=self.ssl_verify_checkbox.isChecked()
            )
            for provider in self.providers
        }
        changed_names = {
            name
            for name in self._original_provider_identities.keys() | new_identities.keys()
            if self._original_provider_identities.get(name) != new_identities.get(name)
        }
        changed_names.update(
            provider["name"]
            for provider in self.providers
            if id(provider) in self._refresh_provider_ids
        )
        force_provider_refresh = self.selected_provider_name in changed_names
        current_provider_changed = (
            self._original_selected_provider_name != self.selected_provider_name
        )
        self.config_manager.language = self.language_combo.currentData() or None
        self.config_manager.check_updates = self.check_updates_checkbox.isChecked()
        self.config_manager.max_cache_image_size = int(self.cache_image_size_input.text())
        self.config_manager.ssl_verify = self.ssl_verify_checkbox.isChecked()
        self.config_manager.live_latency = self.live_latency_combo.currentData()
        self.config_manager.hardware_decoding = self.hardware_decoding_checkbox.isChecked()
        self.config_manager.media_4k_versions = self.media_4k_combo.currentData() or "screen"
        self.config_manager.tmdb_details = self.tmdb_checkbox.isChecked()
        self.config_manager.tmdb_token = "".join(self.tmdb_token_input.text().split())
        if sys.platform == "darwin":
            self.config_manager.desktop_pip = self.desktop_pip_checkbox.isChecked()
        self.config_manager.mouse_wheel_action = self.mouse_wheel_combo.currentData()
        self.config_manager.start_with_last_channel = self.start_last_channel_checkbox.isChecked()
        self.config_manager.play_in_vlc = self.play_in_vlc_checkbox.isChecked()
        self.config_manager.play_in_mpv = self.play_in_mpv_checkbox.isChecked()
        self.config_manager.auto_play_episodes = self.auto_play_episodes_checkbox.isChecked()
        self.config_manager.auto_play_movies = self.auto_play_movies_checkbox.isChecked()
        self.config_manager.timeshift_enabled = self.timeshift_checkbox.isChecked()
        self.config_manager.timeshift_max_mib = self.timeshift_size_slider.value()
        self.config_manager.timeshift_ram_mib = self.timeshift_ram_combo.currentData()
        if self._recordings_dir != self.config_manager.recordings_dir:
            self.config_manager.recordings_dir = self._recordings_dir  # an optional key
        self.config_manager.channel_logos = self.channel_logos_checkbox.isChecked()
        self.config_manager.opensubtitles_api_key = "".join(
            self.opensubtitles_key_input.text().split()
        )
        self.config_manager.opensubtitles_username = (
            self.opensubtitles_username_input.text().strip()
        )
        self.config_manager.opensubtitles_password = self.opensubtitles_password_input.text()

        # The combos' data: the values as the config has always had them ("STB", "Hours").
        epg_source = self.epg_source_combo.currentData()
        if epg_source != self.config_manager.epg_source:
            self.config_manager.epg_source = epg_source
            self.epg_settings_modified = True
        if self.config_manager.epg_url != self.epg_url_input.text():
            self.config_manager.epg_url = self.epg_url_input.text()
            self.epg_settings_modified = True
        if self.config_manager.epg_file != self.epg_file_input.text():
            self.config_manager.epg_file = self.epg_file_input.text()
            self.epg_settings_modified = True
        if self.config_manager.epg_expiration_value != self.epg_expiration_spinner.value():
            self.config_manager.epg_expiration_value = self.epg_expiration_spinner.value()
        # Always: an old "Monthes" reads as "Months" and is saved so.
        self.config_manager.epg_expiration_unit = self.epg_expiration_combo.currentData()

        self.config_manager.selected_provider_name = self.selected_provider_name

        # Save EPG list window hours
        try:
            self.config_manager.epg_list_window_hours = int(self.epg_list_window_spinner.value())
        except Exception:
            pass
        try:
            self.config_manager.epg_stb_period_hours = int(self.epg_stb_period_spinner.value())
        except Exception:
            pass

        save_remote_settings(self)
        # Save the configuration
        self.parent().save_config()

        self.providers_modified = self.providers != self._original_providers
        if self.providers_modified:
            # In place: the providers' own managers share this list.
            self.provider_manager.providers[:] = deepcopy(self.providers)
            self.provider_manager.save_providers()
        for name in changed_names:
            # The selected provider is invalidated after setup is serialized by the parent.
            # The others' caches go before the catalogs reload: an edited provider in use is
            # set up again at once and must not lose the lists its new sign-in brings.
            if name != self.selected_provider_name:
                self._manager_of(name).invalidate_provider_cache(name)
        if self.providers_modified:
            self.parent().providers_saved()  # catalogs of removed or edited providers go

        if current_provider_changed or force_provider_refresh:
            self.parent().set_provider(force_update=force_provider_refresh)
        elif self.epg_settings_modified:
            self.parent().reload_epg()  # in the background; the guide repaints when ready
        elif self.xmltv_mapping_modified:
            if self.config_manager.epg_source != "STB":
                (self.parent().catalog.epg_manager or self.epg_manager).reindex_programs()

        self.accept()

    # ---- one link fills the form ----------------------------------------------------------------

    def paste_link(self):
        """Paste link: the clipboard's provider link fills the form (it is never logged)."""
        link = parse_provider_link(QGuiApplication.clipboard().text())
        if link is None:
            self.verify_result.setText(translate(NO_LINK_TEXT))
            self.verify_result.setStyleSheet("color: red;")
            return
        self.fill_from_link(link)

    def _remember_url(self, text):
        self._url_before = text

    def _on_url_edited(self, text):
        """A link pasted (or dropped) into the address fills the form too; typing never does
        (a link half typed would be cut up under the cursor)."""
        pasted = len(text) - len(self._url_before) > 1  # textEdited comes before textChanged
        link = parse_provider_link(text) if pasted else None
        if link is None:
            return
        # After this edit's own signals: the field is changed again only once they are out.
        QTimer.singleShot(0, self, lambda: self._fill_if_unchanged(text, link))

    def _fill_if_unchanged(self, text, link):
        if self.url_input.text() == text:
            self.fill_from_link(link)

    def fill_from_link(self, link):
        """The type, the address and what the link carries (username and password, or a MAC);
        the provider's name, when it has none yet, is the link's host."""
        button = {
            provider_link.XTREAM: self.type_XTREAM,
            provider_link.M3U: self.type_M3UPLAYLIST,
            provider_link.STB: self.type_STB,
        }[link.type]
        if not (link.type == provider_link.M3U and self.type_M3USTREAM.isChecked()):
            button.setChecked(True)  # update_inputs shows the type's fields (a stream stays)
        self.url_input.setText(link.url)
        if link.type == provider_link.XTREAM:
            self.username_input.setText(link.username)
            self.password_input.setText(link.password)
        if link.mac:
            self.mac_input.setText(link.mac)
        if not self.name_input.text().strip():
            self.name_input.setText(link.name)
        mac_missing = link.type == provider_link.STB and not self.mac_input.text().strip()
        kind = translate(LINK_KINDS[link.type])
        if mac_missing:
            text = tr(
                # i18n: kind is the link's kind: Xtream, M3U playlist or STB portal
                "Filled in from the link ({kind}). Add the portal's MAC address, then press"
                " Verify Provider.",
                kind=kind,
            )
        else:
            text = tr(
                # i18n: kind is the link's kind: Xtream, M3U playlist or STB portal
                "Filled in from the link ({kind}). Check the details, then press Verify Provider.",
                kind=kind,
            )
        self.verify_result.setText(text)
        self.verify_result.setStyleSheet("")

    def load_file(self):
        file_path, _ = QFileDialog.getOpenFileName(self)
        if file_path:
            self.url_input.setText(file_path)
            self.file_button.setVisible(False)

    def verify_provider(self):
        provider = self._verification_provider()
        self._verification_request += 1
        request_id = self._verification_request
        self.verify_result.setText(tr("Verifying..."))
        self.verify_result.setStyleSheet("")
        self.verify_button.setEnabled(False)
        thread = QThread()
        worker = ProviderVerificationWorker(
            request_id, provider, provider.get("ssl_verify", self.ssl_verify_checkbox.isChecked())
        )
        worker.moveToThread(thread)
        thread.started.connect(worker.run)
        worker.reason.connect(self._verification_reason, Qt.QueuedConnection)
        worker.http_only.connect(self._verification_http_only, Qt.QueuedConnection)
        worker.signed_in.connect(self._media_signed_in, Qt.QueuedConnection)
        worker.result.connect(self._provider_verified, Qt.QueuedConnection)
        worker.finished.connect(thread.quit, Qt.DirectConnection)
        ThreadCleanup(thread, worker).finished.connect(
            lambda finished_thread: _release_verification_job(finished_thread, worker)
        )
        _verification_jobs.append((thread, worker))
        thread.start()

    @Slot(int, str)
    def _verification_reason(self, request_id, reason):
        """Why the verification on its way failed, in plain words; `_provider_verified` says it."""
        if request_id == self._verification_request:
            self._verification_why = reason

    @Slot(int, object)
    def _media_signed_in(self, request_id, access):
        """A media server's sign-in on its way gave its access: `_provider_verified` keeps it."""
        if request_id == self._verification_request:
            self._media_access = dict(access or {})

    @Slot(int)
    def _verification_http_only(self, request_id):
        """The provider on its way answered only over HTTP: `_provider_verified` notes it."""
        if request_id == self._verification_request:
            self._verification_insecure = True

    @Slot(int, object, bool)
    def _provider_verified(self, request_id, provider, verified):
        if self._verification_closed or request_id != self._verification_request:
            return
        why, self._verification_why = self._verification_why, ""
        insecure, self._verification_insecure = self._verification_insecure, False
        access, self._media_access = self._media_access, {}
        self.verify_button.setEnabled(True)
        if provider != self._verification_provider():
            self.verify_result.clear()
            return
        # `why` is the account check's reason, already in the language.
        if verified:
            if access and self.edited_provider is not None:
                # 2.8: a media server's access is kept; its password goes from the field.
                self.edited_provider.update(access)
                self.edited_provider.pop("password", None)
                self.password_input.clear()
            self._refresh_provider_ids.add(id(self.edited_provider))
            if why:
                # i18n: why is a warning, as "The portal says your subscription ended on 3 Oct 2026"
                text = tr("Provider verified successfully. Note: {why}.", why=why)
            else:
                text = tr("Provider verified successfully.")
            if insecure:  # the only place QiTV says so (2.7.1)
                text = f"{text}\n{translate(HTTP_ONLY_NOTE)}"
            color = WARNING_COLOR if why or insecure else "green"
        else:
            if why:
                # i18n: why is the reason, as "The username or password is wrong"
                text = tr("Failed to verify provider: {why}.", why=why)
            else:
                text = tr("Failed to verify provider.")
            color = "red"
        self.verify_result.setText(text)
        self.verify_result.setStyleSheet(f"color: {color};")

    def done(self, result):
        self._verification_closed = True
        self._verification_request += 1
        super().done(result)

    def _verification_provider(self):
        provider = self._provider_from_inputs()
        provider.setdefault("ssl_verify", self.ssl_verify_checkbox.isChecked())
        return provider

    def _provider_from_inputs(self):
        provider = dict(self.edited_provider or {})
        provider["url"] = self.url_input.text()
        provider["name"] = self.name_input.text() or provider["url"]
        if self.type_STB.isChecked():
            provider["type"] = "STB"
            provider["mac"] = self.mac_input.text()
            provider["serial_number"] = self.serial_input.text()
            provider["device_id"] = self.device_id_input.text()
        elif self.type_M3UPLAYLIST.isChecked():
            provider["type"] = "M3UPLAYLIST"
        elif self.type_M3USTREAM.isChecked():
            provider["type"] = "M3USTREAM"
        elif self.type_XTREAM.isChecked():
            provider["type"] = "XTREAM"
            provider["username"] = self.username_input.text()
            provider["password"] = self.password_input.text()
        elif self._media_server_checked():
            # 2.8: the password signs in (Verify) and is never stored: the token is.
            provider["type"] = self._media_server_checked()
            provider["username"] = self.username_input.text()
            provider["password"] = self.password_input.text()
        # Automatic is the absence of the key; an existing key keeps its explicit 0.
        if "max_streams" in provider or self.max_streams_input.value() != self._loaded_max_streams:
            provider["max_streams"] = self.max_streams_input.value()
        # No offset is the absence of the key (the provider's guide as it comes, as before).
        offset = int(self.guide_offset_combo.currentData() or 0)
        if offset:
            provider[GUIDE_OFFSET_KEY] = offset
        else:
            provider.pop(GUIDE_OFFSET_KEY, None)
        # Preserve inheritance when merely viewing or saving an unchanged provider. (2.7.1
        # reads no "prefer_https": one an older QiTV saved stays as it was.)
        verify = self.provider_ssl_verify_checkbox.isChecked()
        if "ssl_verify" in provider or verify != self._loaded_provider_network["ssl_verify"]:
            provider["ssl_verify"] = verify
        return provider

    def _store_provider_edits(self):
        if self.edited_provider is None:
            return
        provider = self._provider_from_inputs()
        if is_media_server(provider.get("type")):
            provider.pop("password", None)  # its sign-in's token is kept instead (2.8)
        self.edited_provider.clear()
        self.edited_provider.update(provider)
        self.selected_provider_name = provider["name"]
        self.provider_combo.setItemText(
            self.selected_provider_index,
            f"{self.selected_provider_index + 1}: {provider['name']}",
        )

    def apply_provider(self):
        self._store_provider_edits()
        self._refresh_provider_ids.add(id(self.edited_provider))

    def clear_image_cache(self):
        self.parent().image_manager.clear_cache()
        self.cache_image_size_label = QLabel(self._cache_image_size_text(), self.settings_tab)

    def _cache_image_size_text(self):
        return tr(
            # i18n: size is in megabytes, as "12.34"
            "Max size of image cache (actual size: {size} MB)",
            size=i18n.decimal(f"{self.get_cache_image_size():.2f}"),
        )

    def get_cache_image_size(self):
        total_size = self.parent().image_manager.current_cache_size
        return total_size / (1024 * 1024)  # Convert to MB

    def on_check_updates_toggled(self):
        if self.check_updates_checkbox.isChecked():
            check_for_updates(config_manager=self.config_manager, manual=True)

    def load_xmltv_channel_mapping(self):
        self.xmltv_mapping_table.setRowCount(len(self.config_manager.xmltv_channel_map))
        for row_position, (key, value) in enumerate(self.config_manager.xmltv_channel_map.items()):
            self.xmltv_mapping_table.setItem(row_position, 0, QTableWidgetItem(value["name"]))
            self.xmltv_mapping_table.setItem(
                row_position, 1, QTableWidgetItem(value.get("icon", ""))
            )
            self.xmltv_mapping_table.setItem(row_position, 2, QTableWidgetItem(", ".join(key)))

    def add_xmltv_mapping(self):
        dialog = AddXmltvMappingDialog(self)
        if dialog.exec() == QDialog.Accepted:
            channel_name, logo_url, channel_ids = dialog.get_data()

            if not channel_name or not channel_ids:
                # Show an error message if the input is invalid
                error_dialog = QDialog(self)
                error_dialog.setWindowTitle(tr("Error"))  # i18n: a message window's title
                error_layout = QVBoxLayout(error_dialog)
                error_label = QLabel(tr("Channel Name and Channel IDs are required."), error_dialog)
                error_layout.addWidget(error_label)
                error_button = QPushButton(tr("OK"), error_dialog)
                error_button.clicked.connect(error_dialog.accept)
                error_layout.addWidget(error_button)
                error_dialog.exec()
                return

            # Split the Channel IDs by comma and strip any extra whitespace
            channel_ids_list = [id.strip() for id in channel_ids.split(",")]

            # Add the new mapping to the config manager
            self.config_manager.xmltv_channel_map[tuple(channel_ids_list)] = {
                "name": channel_name,
                "icon": logo_url,
            }

            # Refresh the XMLTV mapping table
            self.xmltv_mapping_modified = True
            self.load_xmltv_channel_mapping()

    def edit_xmltv_mapping(self):
        # Assuming you have a way to get the selected mapping's current values
        selected_items = self.xmltv_mapping_table.selectedItems()
        if not selected_items:
            return

        row = selected_items[0].row()
        current_channel_name = self.xmltv_mapping_table.item(row, 0).text()
        current_logo_url = self.xmltv_mapping_table.item(row, 1).text()
        current_channel_ids = self.xmltv_mapping_table.item(row, 2).text()

        dialog = AddXmltvMappingDialog(
            self,
            channel_name=current_channel_name,
            logo_url=current_logo_url,
            channel_ids=current_channel_ids,
        )
        if dialog.exec() == QDialog.Accepted:
            channel_name, logo_url, channel_ids = dialog.get_data()

            if not channel_name or not channel_ids:
                # Show an error message if the input is invalid
                error_dialog = QDialog(self)
                error_dialog.setWindowTitle(tr("Error"))  # i18n: a message window's title
                error_layout = QVBoxLayout(error_dialog)
                error_label = QLabel(tr("Channel Name and Channel IDs are required."), error_dialog)
                error_layout.addWidget(error_label)
                error_button = QPushButton(tr("OK"), error_dialog)
                error_button.clicked.connect(error_dialog.accept)
                error_layout.addWidget(error_button)
                error_dialog.exec()
                return

            # Split the Channel IDs by comma and strip any extra whitespace
            channel_ids_list = [id.strip() for id in channel_ids.split(",")]

            # Update the existing mapping in the config manager
            key_tuple = tuple(current_channel_ids.split(","))
            del self.config_manager.xmltv_channel_map[key_tuple[0].strip()]
            self.config_manager.xmltv_channel_map[tuple(channel_ids_list)] = {
                "name": channel_name,
                "icon": logo_url,
            }

            # Refresh the XMLTV mapping table
            self.xmltv_mapping_modified = True
            self.load_xmltv_channel_mapping()

    def delete_xmltv_mapping(self):
        selected_items = self.xmltv_mapping_table.selectedItems()
        if not selected_items:
            return

        self.xmltv_mapping_modified = True
        rows = {item.row() for item in selected_items}
        keys = [self.xmltv_mapping_table.item(row, 2).text() for row in rows]
        for key in keys:
            self.config_manager.xmltv_channel_map.pop(tuple(key.split(","))[0].strip(), None)

        self.load_xmltv_channel_mapping()

    def import_xmltv_mapping(self):
        file_path, _ = QFileDialog.getOpenFileName(self)
        if file_path:
            try:
                with open(file_path, "r", encoding="utf-8") as f:
                    list_channels = json.loads(f.read())
                if list_channels is not None:
                    multiKey = MultiKeyDict()
                    for k, v in list_channels.items():
                        xmltv_ids = v.get("xmltv_id", [])
                        if xmltv_ids:
                            v.pop("xmltv_id")
                            multiKey[tuple(xmltv_ids)] = v
                    self.config_manager.xmltv_channel_map = multiKey
                    self.load_xmltv_channel_mapping()
                    self.xmltv_mapping_modified = True
            except FileNotFoundError, json.JSONDecodeError:
                pass

    def export_xmltv_mapping(self):
        file_path, _ = QFileDialog.getSaveFileName(self)
        if file_path:
            with open(
                file_path if file_path.endswith(".json") else file_path + ".json",
                "w",
                encoding="utf-8",
            ) as f:
                export = {}
                for k, v in self.config_manager.xmltv_channel_map.items():
                    mainKey = k[0].strip()
                    export[mainKey] = v
                    export[mainKey]["xmltv_id"] = list(k)
                f.write(json.dumps(export, option=json.OPT_INDENT_2).decode("utf-8"))
