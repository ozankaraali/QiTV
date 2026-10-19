"""Subtitles from OpenSubtitles for what plays (2.2): the Subtitles panel's search.

"Search OpenSubtitles…" on the Subtitles panel (films and episodes; never a live channel)
opens the search page (ui/overlays/subtitle_search.py). Without an API key (neither the
user's, from Settings or the page, nor one the build carries) the page asks for one; Save
keeps it in the config and searches. With a key it searches at once for what plays
(services.opensubtitles.search_query), in a worker: the languages found, the viewer's last
one and then this computer's first, and each language's subtitles ranked. A subtitle chosen
is downloaded in a worker into the subtitles folder, shown with what plays (as a file loaded
from the panel is: it is chosen, under its own name), kept as the title's pick
(services.subtitle_picks) and said in a toast with the downloads left today; the page closes.
What went wrong shows on the page in plain words; a key OpenSubtitles refuses brings the key
page back.

The next time the title plays, the Subtitles panel offers its pick at its top ("Name.srt
(OpenSubtitles)"): it loads from the folder, or is downloaded again when the file is gone.
A download that ends after another title or channel took the player is kept as that title's
pick but not shown; the page closes when what plays changes. The sign-in (Settings) is the
client's: changed or ended there, the next request uses a new client and the old one signs out.
"""

from __future__ import annotations

from dataclasses import dataclass
import logging
from typing import Any, Callable, Optional

from PySide6.QtCore import QObject

from services.opensubtitles import (
    BAD_KEY,
    CONSUMERS_URL,
    NO_KEY,
    Credentials,
    Downloaded,
    DownloadWorker,
    LogoutWorker,
    OpenSubtitlesClient,
    Problem,
    SearchQuery,
    SearchResult,
    SearchWorker,
    SubtitleFile,
    api_key,
    group_languages,
    language_name,
    parse_query,
    ranked,
    search_query,
    system_language,
)
from services.redact import safe_message
from services.subtitle_picks import Pick, SubtitlePicks, pick_key
from ui.keymap import Action
from ui.link_lanes import Target
from ui.overlays.subtitle_search import LanguageRow, SubtitleSearchPage

logger = logging.getLogger(__name__)

SEARCHING_TEXT = "Searching OpenSubtitles…"
DOWNLOADING_TEXT = "Downloading {name}…"
LOADING_TEXT = "Loading {name}…"
NOTHING_FOUND_TEXT = 'No subtitles found for "{query}". Try another spelling, or fewer words.'
NOTHING_TO_SEARCH_TEXT = "Type a title to search for."
FAILED_TEXT = "Something went wrong. Try again."
SAVED_ONLY_TEXT = "Subtitles saved: {name}. They show when it plays again."
REMEMBERED_SUFFIX = " (OpenSubtitles)"
INFO_MS, ERROR_MS = 5000, 8000
# Keys that still reach the player while the page shows; the others are its own.
_PASS_ON = frozenset(
    {
        Action.PLAY_PAUSE,
        Action.FULLSCREEN,
        Action.MUTE,
        Action.VOL_UP,
        Action.VOL_DOWN,
        Action.MEDIA_NEXT,
        Action.MEDIA_PREV,
        Action.MINI_PLAYER,
        Action.SEARCH,
    }
)


@dataclass
class OnlineSetup:
    """What the search needs from the window."""

    jobs: Any  # runs the workers (services.jobs.JobRunner)
    picks: SubtitlePicks  # each title's pick, and the folder the downloads are kept in
    open_url: Callable[[str], None]  # opens the OpenSubtitles page in the browser
    http: Any = None  # stands in for `requests` (tests)


def found_text(count: int, languages: int) -> str:
    noun = "subtitle" if count == 1 else "subtitles"
    places = "language" if languages == 1 else "languages"
    return f"{count:,} {noun} in {languages} {places}"


def remaining_text(remaining: Optional[int], reset: str = "") -> str:
    """ "4 downloads left today, more in 23 hours" ("" when OpenSubtitles did not say)."""
    if remaining is None:
        return ""
    noun = "download" if remaining == 1 else "downloads"
    text = f"{remaining} {noun} left today"
    return f"{text}, more in {reset}" if reset else text


def _same(a: Optional[Target], b: Optional[Target]) -> bool:
    return a is b or (a is not None and b is not None and a.same_as(b))


class OnlineSubtitles(QObject):
    def __init__(
        self,
        *,
        page: SubtitleSearchPage,
        setup: OnlineSetup,
        config: Any,
        current: Callable[[], Optional[Target]],
        playing: Callable[[], bool],
        provider: Callable[[dict], str],
        load: Callable[[str], bool],
        toast: Callable[..., None],
        parent: Optional[QObject] = None,
    ) -> None:
        """`current()` is what the large picture plays and `playing()` whether it has media;
        `provider(item)` an item's provider; `load(path)` shows a subtitle file with what plays
        (False: it could not)."""
        super().__init__(parent)
        self._page = page
        self._setup = setup
        self._config = config
        self._current = current
        self._playing = playing
        self._provider = provider
        self._load = load
        self._toast = toast
        self._target: Optional[Target] = None  # what the page searches for
        self._client: Optional[OpenSubtitlesClient] = None
        self._generation = 0  # the newest search: older results are dropped
        self._downloading = False
        self._remaining: Optional[int] = None  # downloads left today, as said last
        page.saveKeyRequested.connect(self._on_save_key)
        page.getKeyRequested.connect(lambda: setup.open_url(CONSUMERS_URL))
        page.searchRequested.connect(self._on_search)
        page.fileChosen.connect(self._on_file)

    # ---- what the Subtitles panel asks ---------------------------------------------------------

    def available(self, target: Optional[Target]) -> bool:
        """A film or an episode (a local file too): never a live channel."""
        return target is not None and not target.live

    def remembered(self, target: Optional[Target]) -> Optional[Pick]:
        """The subtitles picked for `target` last, if any."""
        key = self._key_for(target)
        return self._setup.picks.get(key) if key else None

    @staticmethod
    def row_text(pick: Pick) -> str:
        return pick.name + REMEMBERED_SUFFIX

    def open(self) -> None:
        """Search OpenSubtitles…: the page, for what plays."""
        target = self._current()
        if not self.available(target):
            return
        self._target = target
        if not self._key():
            self._page.show_key_page(key=str(getattr(self._config, "opensubtitles_api_key", "")))
            return
        assert target is not None
        self._search(self.query_for(target))

    def load_remembered(self, target: Target) -> None:
        """The title's pick again: from the folder, or downloaded again when it is gone."""
        pick = self.remembered(target)
        if pick is None or self._downloading:
            return
        file = SubtitleFile(pick.file_id, pick.language, pick.name, pick.name)
        self._download(target, file, saved=pick.path)

    def query_for(self, target: Target) -> SearchQuery:
        context = target.context or {}
        series = target.subtitle if target.section == "series" else ""
        return search_query(
            target.section, target.title, series_title=series, item=target.item, context=context
        )

    # ---- the page --------------------------------------------------------------------------------

    def visible(self) -> bool:
        return self._page.isVisible()

    def close(self) -> None:
        self._page.dismiss()

    def follow(self, target: Optional[Target] = None) -> None:
        """What plays may have changed (a swap, another title; `target`: what opens now, else
        the current one): the page is for its title only."""
        now = target if target is not None else self._current()
        if self.visible() and not _same(now, self._target):
            self.close()

    def handle(self, action: Action, payload: str = "") -> bool:
        """The keys while the page shows: its own, but those that still reach the player."""
        if not self.visible():
            return False
        if self._page.handle(action, payload):
            return True
        return action not in _PASS_ON

    # ---- searching -------------------------------------------------------------------------------

    def _key(self) -> str:
        return api_key(str(getattr(self._config, "opensubtitles_api_key", "") or ""))

    def _credentials(self) -> Credentials:
        config = self._config
        return Credentials(
            self._key(),
            str(getattr(config, "opensubtitles_username", "") or ""),
            str(getattr(config, "opensubtitles_password", "") or ""),
        )

    def client(self) -> Optional[OpenSubtitlesClient]:
        """The client for the key and sign-in the config has now (None: no key). One whose
        sign-in changed is replaced, and the old one signs out in the background."""
        credentials = self._credentials()
        if not credentials.api_key:
            return None
        old = self._client
        if old is None or old.credentials != credentials:
            self._client = OpenSubtitlesClient(credentials, http=self._setup.http)
            if old is not None and old.signed_in():
                self._setup.jobs.run_worker(LogoutWorker(old), lambda *_: None)
        return self._client

    def _preferred(self) -> list[str]:
        last = str(getattr(self._config, "opensubtitles_language", "") or "")
        return [code for code in dict.fromkeys((last, system_language())) if code]

    def _search(self, query: SearchQuery) -> None:
        self._page.show_search(query.label(), SEARCHING_TEXT)
        if not query.text:
            self._page.set_status(NOTHING_TO_SEARCH_TEXT, "error")
            return
        client = self.client()
        if client is None:
            self._page.show_key_page()
            return
        self._generation += 1
        worker = SearchWorker(client, query, self._preferred())
        self._setup.jobs.run_worker(worker, self._on_found, self._on_failed, self._generation)

    def _on_search(self, text: str) -> None:
        """Search (Enter in the field): what it says, as "Title (2019)" or "Series S01E02"."""
        self._search(parse_query(text))

    def _on_save_key(self, key: str) -> None:
        config = self._config
        config.opensubtitles_api_key = key
        save = getattr(config, "save_config", None)
        if callable(save):
            try:
                save()
            except Exception as e:  # it applies for this run anyway
                logger.warning("Couldn't save the OpenSubtitles key: %s", safe_message(e))
        target = self._target
        if target is not None and _same(self._current(), target):
            self._search(self.query_for(target))
        else:
            self.close()

    def _on_found(self, generation: int, result: object) -> None:
        if generation != self._generation or not self.visible():
            return
        if isinstance(result, Problem):
            self._show_problem(result)
            return
        if not isinstance(result, SearchResult):
            return
        if not result.files:
            label = result.query.label()
            self._page.set_results([], {}, NOTHING_FOUND_TEXT.format(query=label), kind="error")
            return
        prefer_hi = bool(getattr(self._config, "opensubtitles_hearing_impaired", False))
        groups = group_languages(result.files, self._preferred())
        files = {
            code: ranked((f for f in result.files if f.language == code), prefer_hi=prefer_hi)
            for code, _count in groups
        }
        rows = [LanguageRow(code, language_name(code), count) for code, count in groups]
        status = found_text(len(result.files), len(groups))
        left = remaining_text(self._remaining)
        self._page.set_results(rows, files, f"{status} · {left}" if left else status)

    def _on_failed(self, _context: object, message: str) -> None:
        logger.info("An OpenSubtitles job failed: %s", message)
        self._downloading = False
        if self.visible():
            self._page.set_status(FAILED_TEXT, "error")

    def _show_problem(self, problem: Problem) -> None:
        if problem.kind in (NO_KEY, BAD_KEY):
            key = str(getattr(self._config, "opensubtitles_api_key", "") or "")
            self._page.show_key_page(problem.text, key=key)
        else:
            self._page.set_status(problem.text, "error")

    # ---- downloading -----------------------------------------------------------------------------

    def _on_file(self, file: SubtitleFile) -> None:
        target = self._target
        if target is None or self._downloading:
            return
        self._page.set_busy(DOWNLOADING_TEXT.format(name=file.release))
        self._download(target, file)

    def _download(self, target: Target, file: SubtitleFile, *, saved: str = "") -> None:
        client = self.client()
        if client is None and not saved:
            self._page.show_key_page()
            return
        self._downloading = True
        folder = self._setup.picks.folder
        worker = DownloadWorker(client, file, folder, saved=saved)
        self._setup.jobs.run_worker(worker, self._on_downloaded, self._on_failed, (target, file))

    def _on_downloaded(self, context: tuple[Target, SubtitleFile], result: object) -> None:
        self._downloading = False
        target, file = context
        if isinstance(result, Problem):
            if self.visible():
                self._show_problem(result)
            else:
                self._toast(result.text, "error", ERROR_MS)
            return
        if not isinstance(result, Downloaded):
            return
        self._keep(target, file, result)
        shown = _same(self._current(), target) and self._playing() and self._load(result.path)
        if self._page.isVisible() and _same(self._target, target):
            self._page.dismiss()
        if not shown:
            if _same(self._current(), target) and self._playing():
                return  # it could not be loaded: the panel's own toast said so
            self._toast(SAVED_ONLY_TEXT.format(name=result.name), "info", INFO_MS)
            return
        text = f"Subtitles: {result.name}"
        left = remaining_text(result.remaining, result.reset) if not result.reused else ""
        self._toast(f"{text} · {left}" if left else text, "info", INFO_MS)

    def _keep(self, target: Target, file: SubtitleFile, result: Downloaded) -> None:
        """The title's pick, and the viewer's language (and whether it was for the hearing
        impaired) for the next search."""
        if not result.reused:
            self._remaining = result.remaining
            config = self._config
            config.opensubtitles_language = result.language or file.language
            config.opensubtitles_hearing_impaired = file.hearing_impaired
        key = self._key_for(target)
        if key:
            pick = Pick(result.file_id, result.name, result.path, result.language)
            self._setup.picks.set(key, pick)

    def _key_for(self, target: Optional[Target]) -> str:
        if not self.available(target):
            return ""
        assert target is not None
        provider = "" if target.local else self._provider(target.item)
        return pick_key(
            provider, target.item, episode=target.section == "series", context=target.context
        )
