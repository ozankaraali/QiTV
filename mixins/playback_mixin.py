"""Playback, auto-play, resume, VLC/MPV launch, and watch history methods."""

from datetime import datetime
import logging
import os
import platform
import shutil
import subprocess
from typing import TYPE_CHECKING, Any, Dict, List, Optional
from urllib.parse import quote as url_quote

from PySide6.QtCore import Qt, QThread, QTimer
from PySide6.QtWidgets import (
    QDialog,
    QHBoxLayout,
    QListWidget,
    QListWidgetItem,
    QMessageBox,
    QPushButton,
    QVBoxLayout,
)
import requests
from urlobject import URLObject

from widgets.autoplay_dialogs import (
    EpisodeAutoPlayDialog,
    MovieSuggestionDialog,
    NoCategoryMoviesDialog,
    ResumeCountdownDialog,
    SeriesCompleteDialog,
)

if TYPE_CHECKING:
    from config_manager import ConfigManager
    from provider_manager import ProviderManager
    from video_player import VideoPlayer

logger = logging.getLogger(__name__)


class PlaybackMixin:
    """Mixin providing playback, auto-play, resume, and watch history functionality."""

    # Provided by ChannelList at runtime
    provider_manager: "ProviderManager"
    config_manager: "ConfigManager"
    player: "VideoPlayer"
    content_type: str
    link: Optional[str]
    current_category: Optional[Dict[str, Any]]
    current_series: Optional[Dict[str, Any]]
    current_season: Optional[Dict[str, Any]]
    _bg_jobs: List[Any]
    _current_content_id: Optional[str]
    _current_playing_item: Optional[Dict[str, Any]]
    _current_playing_type: Optional[str]
    _current_episode_index: int
    _current_episode_list: List[Dict[str, Any]]
    _current_seasons_list: List[Dict[str, Any]]
    _current_category_movies: List[Dict[str, Any]]
    _autoplay_dialog: Optional[QDialog]
    _external_mpv_player: Optional[Any]
    _pending_link_ctx: Optional[Dict[str, Any]]

    # Methods provided by other mixins / ChannelList
    def lock_ui_before_loading(self) -> None: ...
    def unlock_ui_after_loading(self) -> None: ...
    def _on_link_created(self, payload: Any) -> None: ...
    def _on_link_error(self, msg: str) -> None: ...
    def sanitize_url(self, url: str) -> str: ...
    def set_provider(self, force_update: bool = False) -> None: ...

    def play_item(self, item_data, is_episode=False, item_type=None):
        # Track current playing item for auto-play
        self._current_playing_item = item_data
        self._current_playing_type = item_type

        # Track episode context for auto-play
        if item_type == "episode":
            self._track_episode_context(item_data)
        elif item_type == "movie":
            self._track_movie_context(item_data)

        # Generate content ID for position tracking
        provider_name = self.provider_manager.current_provider.get("name", "")
        self._current_content_id = self.config_manager._generate_content_id(
            item_data, provider_name
        )

        # Add to watch history
        self.config_manager.add_to_watch_history(
            item_data, item_type or "channel", item_data.get("cmd", ""), provider_name
        )

        if self.provider_manager.current_provider["type"] == "STB":
            from workers import LinkCreatorWorker

            # Create link in a worker thread, then play
            selected_provider = self.provider_manager.current_provider
            headers = self.provider_manager.headers
            base_url = selected_provider.get("url", "")
            cmd = item_data.get("cmd")
            series_param = item_data.get("series") if is_episode else None

            self.lock_ui_before_loading()
            thread = QThread()
            prefer_https = selected_provider.get("prefer_https", self.config_manager.prefer_https)
            verify_ssl = selected_provider.get("ssl_verify", self.config_manager.ssl_verify)
            worker = LinkCreatorWorker(
                base_url=base_url,
                headers=headers,
                content_type=self.content_type,
                cmd=cmd,
                is_episode=is_episode,
                series_param=series_param,
                verify_ssl=verify_ssl,
                prefer_https=prefer_https,
            )
            worker.moveToThread(thread)

            # Store context for UI handler
            self._pending_link_ctx = {
                "item_data": item_data,
                "item_type": item_type or "channel",
                "is_episode": is_episode,
            }

            def _cleanup():
                try:
                    self._bg_jobs.remove((thread, worker))
                except ValueError:
                    pass

            thread.started.connect(worker.run)
            worker.finished.connect(self._on_link_created, Qt.QueuedConnection)
            worker.error.connect(self._on_link_error, Qt.QueuedConnection)
            worker.finished.connect(thread.quit)
            worker.error.connect(thread.quit)
            thread.finished.connect(worker.deleteLater)
            thread.finished.connect(thread.deleteLater)
            thread.finished.connect(_cleanup)
            thread.start()
            self._bg_jobs.append((thread, worker))
        else:
            cmd = item_data.get("cmd")
            self.link = cmd
            self._play_content_with_resume_check(cmd, item_data)
            # Save last watched
            self.save_last_watched(item_data, item_type or "m3ucontent", cmd)

    def _track_episode_context(self, item_data: Dict[str, Any]):
        """Track episode context for auto-play navigation."""
        # Build episode list from current season
        if self.current_season:
            self._current_episode_list = self._build_episode_list_from_season(self.current_season)

            # Find current episode index
            current_num = str(item_data.get("number", ""))
            for idx, ep in enumerate(self._current_episode_list):
                if str(ep.get("number", "")) == current_num:
                    self._current_episode_index = idx
                    break

        # Track seasons for next-season navigation
        if self.current_series:
            content_data = self.provider_manager.current_provider_content.get("series", {})
            series_id = self.current_series.get("id")
            if series_id:
                cached_seasons = content_data.get("seasons", {}).get(str(series_id), [])
                if cached_seasons:
                    self._current_seasons_list = cached_seasons

    def _track_movie_context(self, item_data: Dict[str, Any]):
        """Track movie context for auto-play suggestions."""
        # Get movies from current category
        if self.current_category:
            category_id = self.current_category.get("id")
            content_data = self.provider_manager.current_provider_content.get("vod", {})
            contents = content_data.get("contents", {})
            if isinstance(contents, dict) and category_id in contents:
                self._current_category_movies = contents[category_id]
            elif isinstance(contents, list):
                self._current_category_movies = contents

    def _play_content_with_resume_check(self, url: str, item_data: Dict[str, Any]):
        """Play content, checking for resume position first."""
        # Check for saved position
        content_id = self._current_content_id
        if not content_id:
            self._play_content(url)
            return

        saved_position = self.config_manager.get_playback_position(content_id)

        if saved_position and saved_position > 5000:  # More than 5 seconds
            # Check if near the end (don't resume if >90% watched)
            positions = self.config_manager.playback_positions or {}
            entry = positions.get(content_id, {})
            duration = entry.get("duration", 0)

            if duration > 0:
                ratio = saved_position / duration
                if ratio >= 0.90:
                    # Near end, start from beginning
                    self._play_content(url)
                    return

            # Show resume dialog
            content_name = item_data.get("name") or item_data.get("ename") or "Content"

            def on_resume():
                self._play_content_with_position(url, saved_position)

            def on_start_over():
                self.config_manager.clear_playback_position(content_id)
                self._play_content(url)

            self._show_resume_dialog(content_name, saved_position, on_resume, on_start_over)
        else:
            self._play_content(url)

    def _play_content_with_position(self, url: str, resume_position: int):
        """Play content with resume position."""
        if self.config_manager.play_in_vlc:
            self._launch_vlc(url)
        elif self.config_manager.play_in_mpv:
            self._launch_mpv(url)
        else:
            provider_type = self.provider_manager.current_provider.get("type", "").upper()
            if provider_type in ("STB", "XTREAM"):
                is_live = self.content_type == "itv"
            else:
                is_live = None
            self.player.play_video(
                url,
                is_live=is_live,
                content_id=self._current_content_id,
                resume_position=resume_position,
            )

    def save_last_watched(self, item_data, item_type, link):
        """Save the last watched item to config"""
        self.config_manager.last_watched = {
            "item_data": item_data,
            "item_type": item_type,
            "link": link,
            "timestamp": datetime.now().isoformat(),
            "provider_name": self.provider_manager.current_provider.get("name", ""),
        }
        self.config_manager.save_config()

    def resume_last_watched(self):
        """Resume playing the last watched item"""
        last_watched = self.config_manager.last_watched
        if not last_watched:
            QMessageBox.information(self, "No History", "No previously watched content found.")
            return

        # Check if the provider matches
        current_provider_name = self.provider_manager.current_provider.get("name", "")
        if last_watched.get("provider_name") != current_provider_name:
            reply = QMessageBox.question(
                self,
                "Different Provider",
                f"Last watched content was from provider '{last_watched.get('provider_name')}'. Continue anyway?",
                QMessageBox.Yes | QMessageBox.No,
                QMessageBox.No,
            )
            if reply == QMessageBox.No:
                return
            # Switch provider automatically and resume after switching
            target_name = last_watched.get("provider_name", "")
            if target_name:
                # If provider exists, switch to it
                names = [p.get("name", "") for p in self.provider_manager.providers]
                if target_name in names:
                    self._pending_resume = last_watched
                    self.config_manager.selected_provider_name = target_name
                    self.config_manager.save_config()
                    # Trigger provider switch; playback continues in set_provider_finished
                    QTimer.singleShot(0, lambda: self.set_provider())
                    return

        # Play the last watched item
        item_data = last_watched.get("item_data")
        item_type = last_watched.get("item_type")
        is_episode = item_type == "episode"

        # For STB providers, always recreate the link (tokens expire)
        # For M3U/stream providers, can use stored link directly
        current_provider_type = self.provider_manager.current_provider.get("type", "")
        if current_provider_type == "STB":
            # Recreate link with fresh token
            self.play_item(item_data, is_episode=is_episode, item_type=item_type)
        elif last_watched.get("link"):
            # Use stored link for non-STB providers
            self.link = last_watched["link"]
            self._play_content(self.link)
        else:
            # Fallback: recreate the link
            self.play_item(item_data, is_episode=is_episode, item_type=item_type)

    def create_link(self, item, is_episode=False):
        try:
            selected_provider = self.provider_manager.current_provider
            headers = self.provider_manager.headers
            url = selected_provider.get("url", "")
            url = URLObject(url)
            scheme = url.scheme
            if self.config_manager.prefer_https and scheme == "http":
                scheme = "https"
            url = f"{scheme}://{url.netloc}"
            cmd = item.get("cmd")
            if is_episode:
                # For episodes, we need to pass 'series' parameter
                series_param = item.get("series")  # This should be the episode number
                fetchurl = (
                    f"{url}/server/load.php?type={'vod' if self.content_type == 'series' else self.content_type}&action=create_link"
                    f"&cmd={url_quote(cmd)}&series={series_param}&JsHttpRequest=1-xml"
                )
            else:
                fetchurl = (
                    f"{url}/server/load.php?type={self.content_type}&action=create_link"
                    f"&cmd={url_quote(cmd)}&JsHttpRequest=1-xml"
                )
            verify_ssl = selected_provider.get("ssl_verify", self.config_manager.ssl_verify)
            response = requests.get(fetchurl, headers=headers, timeout=5, verify=verify_ssl)
            if response.status_code != 200 or not response.content:
                logger.warning(
                    f"Error creating link: status code {response.status_code}, response content empty"
                )
                return None
            result = response.json()
            link = result["js"]["cmd"].split(" ")[-1]
            link = self.sanitize_url(link)
            self.link = link
            return link
        except Exception as e:
            logger.warning(f"Error creating link: {e}")
            return None

    def _play_content(self, url):
        """Play content in VLC, MPV, or built-in player based on checkbox state."""
        if self.config_manager.play_in_vlc:
            self._launch_vlc(url)
        elif self.config_manager.play_in_mpv:
            self._launch_mpv(url)
        else:
            # Use built-in player
            # Determine is_live hint based on provider type
            # STB/XTREAM APIs explicitly separate live (itv) from VOD content
            # M3U playlists don't distinguish, so let VLC auto-detect
            provider_type = self.provider_manager.current_provider.get("type", "").upper()
            if provider_type in ("STB", "XTREAM"):
                is_live = self.content_type == "itv"
            else:
                # M3UPLAYLIST, M3USTREAM, etc. - let VLC detect via seekability
                is_live = None
            self.player.play_video(url, is_live=is_live, content_id=self._current_content_id)

    def _launch_vlc(self, url):
        """Launch VLC with platform-specific handling."""
        vlc_cmd = None

        if platform.system() == "Darwin":
            # macOS: use 'open -a VLC' which reuses existing instance
            vlc_paths = [
                "/Applications/VLC.app",
                os.path.expanduser("~/Applications/VLC.app"),
            ]
            for path in vlc_paths:
                if os.path.exists(path):
                    try:
                        subprocess.Popen(["open", "-a", path, url])
                        return True
                    except Exception as e:
                        QMessageBox.warning(
                            self, "VLC Launch Failed", f"Failed to launch VLC: {str(e)}"
                        )
                        return False

        elif platform.system() == "Windows":
            # Windows: find VLC executable
            vlc_cmd = shutil.which("vlc")
            if not vlc_cmd:
                possible_paths = [
                    os.path.join(
                        os.environ.get("ProgramFiles", r"C:\Program Files"),
                        "VideoLAN",
                        "VLC",
                        "vlc.exe",
                    ),
                    os.path.join(
                        os.environ.get("ProgramFiles(x86)", r"C:\Program Files (x86)"),
                        "VideoLAN",
                        "VLC",
                        "vlc.exe",
                    ),
                ]
                for path in possible_paths:
                    if os.path.exists(path):
                        vlc_cmd = path
                        break
        else:
            # Linux
            vlc_cmd = shutil.which("vlc")

        if not vlc_cmd:
            QMessageBox.warning(
                self,
                "VLC Not Found",
                "VLC Media Player is not installed.\n\n"
                "Please install VLC:\n"
                "• macOS: Download from https://www.videolan.org/vlc/\n"
                "• Linux: Use your package manager (apt install vlc)\n"
                "• Windows: Download from https://www.videolan.org/vlc/",
            )
            return False

        try:
            # --started-from-file sends URL to existing VLC instance (replaces current)
            subprocess.Popen([vlc_cmd, "--started-from-file", url])
            return True
        except Exception as e:
            QMessageBox.warning(self, "VLC Launch Failed", f"Failed to launch VLC: {str(e)}")
            return False

    def _launch_mpv(self, url):
        """Launch MPV - try python-mpv first, fall back to subprocess."""
        # Try python-mpv for single-instance behavior
        try:
            import mpv

            if self._external_mpv_player is None:
                self._external_mpv_player = mpv.MPV(
                    input_default_bindings=True,
                    input_vo_keyboard=True,
                    osc=True,
                )

            self._external_mpv_player.play(url)
            return True
        except (ImportError, OSError, Exception):
            # python-mpv not available or failed, fall back to subprocess
            pass

        # Subprocess fallback with platform-specific paths
        mpv_cmd = None

        if platform.system() == "Darwin":
            # macOS: check app bundle and brew paths
            macos_paths = [
                "/Applications/mpv.app/Contents/MacOS/mpv",
                os.path.expanduser("~/Applications/mpv.app/Contents/MacOS/mpv"),
                "/opt/homebrew/bin/mpv",
                "/usr/local/bin/mpv",
            ]
            for path in macos_paths:
                if os.path.exists(path):
                    mpv_cmd = path
                    break
            if not mpv_cmd:
                mpv_cmd = shutil.which("mpv")

        elif platform.system() == "Windows":
            mpv_cmd = shutil.which("mpv")
            if not mpv_cmd:
                # Check Windows Registry App Paths (set by mpv-install.bat)
                # Try both HKEY_LOCAL_MACHINE and HKEY_CURRENT_USER
                try:
                    import winreg

                    for hkey in [winreg.HKEY_LOCAL_MACHINE, winreg.HKEY_CURRENT_USER]:  # type: ignore[attr-defined]
                        try:
                            with winreg.OpenKey(  # type: ignore[attr-defined]
                                hkey,
                                r"SOFTWARE\Microsoft\Windows\CurrentVersion\App Paths\mpv.exe",
                            ) as key:
                                reg_path = winreg.QueryValue(key, None)  # type: ignore[attr-defined]
                                if reg_path and os.path.exists(reg_path):
                                    mpv_cmd = reg_path
                                    break
                        except Exception:
                            continue
                except Exception:
                    pass
            if not mpv_cmd:
                user_profile = os.environ.get("USERPROFILE", "")
                possible_paths = [
                    # Common installation paths
                    os.path.join(
                        os.environ.get("ProgramFiles", r"C:\Program Files"),
                        "mpv",
                        "mpv.exe",
                    ),
                    os.path.join(
                        os.environ.get("ProgramFiles(x86)", r"C:\Program Files (x86)"),
                        "mpv",
                        "mpv.exe",
                    ),
                    os.path.join(os.environ.get("LOCALAPPDATA", ""), "Programs", "mpv", "mpv.exe"),
                    # Scoop package manager
                    os.path.join(user_profile, "scoop", "apps", "mpv", "current", "mpv.exe"),
                    # Chocolatey package manager
                    os.path.join(
                        os.environ.get("ProgramData", r"C:\ProgramData"),
                        "chocolatey",
                        "bin",
                        "mpv.exe",
                    ),
                    # Portable/extracted mpv (common locations)
                    r"C:\mpv\mpv.exe",
                    os.path.join(user_profile, "mpv", "mpv.exe"),
                    os.path.join(user_profile, "Downloads", "mpv", "mpv.exe"),
                    os.path.join(user_profile, "Desktop", "mpv", "mpv.exe"),
                    # Winget default location
                    os.path.join(
                        os.environ.get("LOCALAPPDATA", ""),
                        "Microsoft",
                        "WinGet",
                        "Packages",
                        "mpv.net_Mpv.net",
                        "mpv",
                        "mpv.exe",
                    ),
                ]
                for path in possible_paths:
                    if path and os.path.exists(path):
                        mpv_cmd = path
                        break
        else:
            # Linux
            mpv_cmd = shutil.which("mpv")

        if not mpv_cmd:
            QMessageBox.warning(
                self,
                "MPV Not Found",
                "MPV Media Player is not installed.\n\n"
                "Please install MPV:\n"
                "• macOS: brew install mpv\n"
                "• Linux: Use your package manager (apt install mpv)\n"
                "• Windows: Download from https://mpv.io/installation/",
            )
            return False

        try:
            subprocess.Popen([mpv_cmd, url])
            return True
        except Exception as e:
            QMessageBox.warning(self, "MPV Launch Failed", f"Failed to launch MPV: {str(e)}")
            return False

    def open_in_vlc(self):
        # Invoke user's VLC player to open the current stream
        if self.link:
            logger.warning(f"Opening VLC for link: {self.link}")
            try:
                if platform.system() == "Windows":
                    vlc_path = shutil.which("vlc")  # Try to find VLC in PATH
                    if not vlc_path:
                        program_files = os.environ.get("ProgramFiles", "C:\\Program Files")
                        vlc_path = os.path.join(program_files, "VideoLAN", "VLC", "vlc.exe")
                    # Use VLC's directory as cwd to avoid DLL conflicts with bundled libvlc
                    vlc_dir = os.path.dirname(vlc_path)
                    subprocess.Popen([vlc_path, self.link], cwd=vlc_dir)
                elif platform.system() == "Darwin":  # macOS
                    vlc_path = shutil.which("vlc")  # Try to find VLC in PATH
                    if not vlc_path:
                        common_paths = [
                            "/Applications/VLC.app/Contents/MacOS/VLC",
                            "~/Applications/VLC.app/Contents/MacOS/VLC",
                        ]
                        for path in common_paths:
                            expanded_path = os.path.expanduser(path)
                            if os.path.exists(expanded_path):
                                vlc_path = expanded_path
                                break
                    if not vlc_path:
                        raise FileNotFoundError("VLC not found")
                    vlc_dir = os.path.dirname(vlc_path)
                    subprocess.Popen([vlc_path, self.link], cwd=vlc_dir)
                else:  # Assuming Linux or other Unix-like OS
                    vlc_path = shutil.which("vlc")  # Try to find VLC in PATH
                    if not vlc_path:
                        raise FileNotFoundError("VLC not found")
                    vlc_dir = os.path.dirname(vlc_path)
                    subprocess.Popen([vlc_path, self.link], cwd=vlc_dir)
                # when VLC opens, stop running video on self.player
                self.player.stop_video()
            except FileNotFoundError as fnf_error:
                logger.warning("VLC not found: %s", fnf_error)
            except Exception as e:
                logger.warning(f"Error opening VLC: {e}")

    def open_file(self):
        from PySide6.QtWidgets import QFileDialog

        file_path, _ = QFileDialog.getOpenFileName(self)
        if file_path:
            self._play_content(file_path)

    # --- Auto-Play Methods ---

    def on_media_ended(self):
        """Handle media ended signal - trigger auto-play if enabled."""
        # Don't auto-play if using external player
        if self.config_manager.play_in_vlc or self.config_manager.play_in_mpv:
            return

        if not self._current_playing_item or not self._current_playing_type:
            return

        if self._current_playing_type == "episode":
            if self.config_manager.auto_play_episodes:
                self._show_next_episode_dialog()
        elif self._current_playing_type == "movie":
            if self.config_manager.auto_play_movies:
                self._show_next_movie_dialog()

    def on_position_changed(self, position_ms: int, duration_ms: int):
        """Handle position changed signal - save playback position."""
        if self._current_content_id and position_ms > 0 and duration_ms > 0:
            self.config_manager.save_playback_position(
                self._current_content_id, position_ms, duration_ms
            )

    def _show_next_episode_dialog(self):
        """Show dialog for auto-playing next episode."""
        next_episode = self._get_next_episode()
        if not next_episode:
            # Try next season
            next_season_episode = self._get_first_episode_of_next_season()
            if next_season_episode:
                next_episode = next_season_episode
            else:
                # Series complete
                series_name = (
                    self.current_series.get("name", "Series") if self.current_series else "Series"
                )
                dialog = SeriesCompleteDialog(self, series_name)
                dialog.show()
                return

        episode_name = next_episode.get("ename") or next_episode.get("name") or "Next Episode"

        self._autoplay_dialog = EpisodeAutoPlayDialog(
            self,
            next_episode_name=episode_name,
            countdown_seconds=self.config_manager.DEFAULT_OPTION_EPISODE_COUNTDOWN,
        )
        self._autoplay_dialog.countdownFinished.connect(
            lambda: self._play_next_episode(next_episode)
        )
        self._autoplay_dialog.cancelled.connect(self._cancel_autoplay)
        self._autoplay_dialog.show()

    def _show_next_movie_dialog(self):
        """Show dialog for suggesting next movie from same category."""
        next_movie = self._get_next_movie_from_category()
        if not next_movie:
            category_name = self.current_category.get("title", "") if self.current_category else ""
            dialog = NoCategoryMoviesDialog(self, category_name)
            dialog.show()
            return

        movie_name = next_movie.get("name") or "Next Movie"
        category_name = self.current_category.get("title", "") if self.current_category else ""

        self._autoplay_dialog = MovieSuggestionDialog(
            self,
            movie_name=movie_name,
            category_name=category_name,
            countdown_seconds=self.config_manager.DEFAULT_OPTION_MOVIE_COUNTDOWN,
        )
        self._autoplay_dialog.countdownFinished.connect(lambda: self._play_next_movie(next_movie))
        self._autoplay_dialog.cancelled.connect(self._cancel_autoplay)
        self._autoplay_dialog.show()

    def _cancel_autoplay(self):
        """Cancel auto-play dialog."""
        self._autoplay_dialog = None

    def _get_next_episode(self) -> Optional[Dict[str, Any]]:
        """Get the next episode in the current season."""
        if self._current_episode_index < 0 or not self._current_episode_list:
            return None

        next_index = self._current_episode_index + 1
        if next_index < len(self._current_episode_list):
            return self._current_episode_list[next_index]
        return None

    def _get_first_episode_of_next_season(self) -> Optional[Dict[str, Any]]:
        """Get the first episode of the next season."""
        if not self.current_season or not self._current_seasons_list:
            return None

        # Find current season index
        current_season_id = self.current_season.get("id")
        current_season_idx = -1
        for idx, season in enumerate(self._current_seasons_list):
            if season.get("id") == current_season_id:
                current_season_idx = idx
                break

        if current_season_idx < 0:
            return None

        next_season_idx = current_season_idx + 1
        if next_season_idx >= len(self._current_seasons_list):
            return None

        # Load next season's episodes
        next_season = self._current_seasons_list[next_season_idx]
        episodes = next_season.get("episodes", [])
        if not episodes:
            return None

        # Build episode item from first episode
        first_ep = episodes[0]
        provider_type = self.provider_manager.current_provider.get("type", "").upper()

        if provider_type == "XTREAM":
            episode_item = self._build_xtream_episode_item(first_ep, next_season)
        else:
            # STB episodes
            episode_item = self._build_stb_episode_item(first_ep, next_season)

        # Update current season for playback context
        self.current_season = next_season
        self._current_episode_list = self._build_episode_list_from_season(next_season)
        self._current_episode_index = 0

        return episode_item

    def _build_xtream_episode_item(self, episode_data: dict, season_data: dict) -> Dict[str, Any]:
        """Build an episode item dict for Xtream provider."""
        provider = self.provider_manager.current_provider
        resolved_base = self.provider_manager.current_provider_content.get("series", {}).get(
            "resolved_base", ""
        ) or provider.get("url", "")

        username = provider.get("username", "")
        password = provider.get("password", "")

        ep_id = episode_data.get("id")
        container_ext = episode_data.get("container_extension", "mkv")
        cmd = f"{resolved_base}/series/{username}/{password}/{ep_id}.{container_ext}"

        return {
            "id": ep_id,
            "name": episode_data.get("title") or f"Episode {episode_data.get('episode_num', '')}",
            "ename": episode_data.get("title") or f"Episode {episode_data.get('episode_num', '')}",
            "number": str(episode_data.get("episode_num", "")),
            "cmd": cmd,
            "series": episode_data.get("episode_num"),
            "info": episode_data.get("info", {}),
        }

    def _build_stb_episode_item(self, episode_num: int, season_data: dict) -> Dict[str, Any]:
        """Build an episode item dict for STB provider."""
        if self.current_series:
            episode_item = self.current_series.copy()
        else:
            episode_item = {}

        episode_item["number"] = str(episode_num)
        episode_item["ename"] = f"Episode {episode_num}"
        episode_item["cmd"] = season_data.get("cmd")
        episode_item["series"] = episode_num
        return episode_item

    def _build_episode_list_from_season(self, season_data: dict) -> List[Dict[str, Any]]:
        """Build a list of episode items from season data."""
        episodes = season_data.get("episodes", [])
        provider_type = self.provider_manager.current_provider.get("type", "").upper()

        episode_list = []
        for ep in episodes:
            if provider_type == "XTREAM":
                episode_list.append(self._build_xtream_episode_item(ep, season_data))
            else:
                # STB uses simple episode numbers
                if isinstance(ep, int):
                    episode_list.append(self._build_stb_episode_item(ep, season_data))
                elif isinstance(ep, dict):
                    ep_num = ep.get("episode_num") or ep.get("series") or 1
                    episode_list.append(self._build_stb_episode_item(int(ep_num), season_data))

        return episode_list

    def _play_next_episode(self, episode_data: Dict[str, Any]):
        """Play the next episode."""
        self._autoplay_dialog = None

        # Update episode index
        for idx, ep in enumerate(self._current_episode_list):
            if ep.get("id") == episode_data.get("id") or (
                ep.get("number") == episode_data.get("number")
                and ep.get("cmd") == episode_data.get("cmd")
            ):
                self._current_episode_index = idx
                break

        # Play the episode
        self.play_item(episode_data, is_episode=True, item_type="episode")

    def _get_next_movie_from_category(self) -> Optional[Dict[str, Any]]:
        """Get the next unwatched movie from the same category."""
        if not self._current_category_movies or not self._current_playing_item:
            return None

        current_id = self._current_playing_item.get("id")
        provider_name = self.provider_manager.current_provider.get("name", "")

        # Find unwatched movies, excluding current
        unwatched = []
        for movie in self._current_category_movies:
            if movie.get("id") == current_id:
                continue
            content_id = self.config_manager._generate_content_id(movie, provider_name)
            status = self.config_manager.get_watched_status(content_id)
            if status != "watched":
                unwatched.append(movie)

        if unwatched:
            # Return the first unwatched movie
            return unwatched[0]

        # If all watched, just pick the next one after current
        for idx, movie in enumerate(self._current_category_movies):
            if movie.get("id") == current_id:
                next_idx = (idx + 1) % len(self._current_category_movies)
                if self._current_category_movies[next_idx].get("id") != current_id:
                    return self._current_category_movies[next_idx]
                break

        return None

    def _play_next_movie(self, movie_data: Dict[str, Any]):
        """Play the next movie."""
        self._autoplay_dialog = None
        self.play_item(movie_data, item_type="movie")

    def _format_position_time(self, ms: int) -> str:
        """Format milliseconds as HH:MM:SS or MM:SS."""
        seconds = ms // 1000
        minutes, seconds = divmod(seconds, 60)
        hours, minutes = divmod(minutes, 60)
        if hours > 0:
            return f"{hours:02d}:{minutes:02d}:{seconds:02d}"
        return f"{minutes:02d}:{seconds:02d}"

    def _show_resume_dialog(self, content_name: str, position_ms: int, on_resume, on_start_over):
        """Show dialog for resuming playback."""
        position_text = self._format_position_time(position_ms)

        dialog = ResumeCountdownDialog(
            self,
            content_name=content_name,
            resume_position_text=position_text,
            countdown_seconds=self.config_manager.DEFAULT_OPTION_RESUME_COUNTDOWN,
        )
        dialog.countdownFinished.connect(on_resume)
        dialog.resumeClicked.connect(on_resume)
        dialog.startOverClicked.connect(on_start_over)
        dialog.exec()

    def show_watch_history(self):
        """Show watch history dialog."""
        history = self.config_manager.watch_history or []
        if not history:
            QMessageBox.information(self, "Watch History", "No watch history available.")
            return

        dialog = QDialog(self)
        dialog.setWindowTitle("Watch History")
        dialog.setMinimumSize(500, 400)

        layout = QVBoxLayout(dialog)

        history_list = QListWidget()
        for entry in history:
            name = entry.get("name", "Unknown")
            item_type = entry.get("item_type", "")
            provider = entry.get("provider_name", "")
            timestamp = entry.get("timestamp", "")[:10]  # Just date

            # Get progress info
            content_id = entry.get("content_id", "")
            status = self.config_manager.get_watched_status(content_id)
            status_text = {
                "watched": "\u2713",
                "partial": "\u25d0",
                "unwatched": "",
            }.get(status, "")

            display_text = f"{status_text} {name} [{item_type}] - {provider} ({timestamp})"
            list_item = QListWidgetItem(display_text)
            list_item.setData(Qt.UserRole, entry)
            history_list.addItem(list_item)

        layout.addWidget(history_list)

        # Buttons
        button_layout = QHBoxLayout()

        play_button = QPushButton("Play")

        def on_play():
            selected = history_list.selectedItems()
            if selected:
                entry = selected[0].data(Qt.UserRole)
                dialog.accept()
                self._play_from_history(entry)

        play_button.clicked.connect(on_play)
        button_layout.addWidget(play_button)

        clear_button = QPushButton("Clear History")

        def on_clear():
            self.config_manager.watch_history = []
            self.config_manager.save_config()
            dialog.accept()

        clear_button.clicked.connect(on_clear)
        button_layout.addWidget(clear_button)

        close_button = QPushButton("Close")
        close_button.clicked.connect(dialog.reject)
        button_layout.addWidget(close_button)

        layout.addLayout(button_layout)

        # Double-click to play
        history_list.itemDoubleClicked.connect(lambda item: on_play())

        dialog.exec()

    def _play_from_history(self, entry: Dict[str, Any]):
        """Play an item from watch history."""
        item_data = entry.get("item_data")
        item_type = entry.get("item_type")
        provider_name = entry.get("provider_name", "")

        # Check if we need to switch providers
        current_provider_name = self.provider_manager.current_provider.get("name", "")
        if provider_name and provider_name != current_provider_name:
            # Check if provider exists
            names = [p.get("name", "") for p in self.provider_manager.providers]
            if provider_name in names:
                self._pending_resume = entry
                self.config_manager.selected_provider_name = provider_name
                self.config_manager.save_config()
                QTimer.singleShot(0, lambda: self.set_provider())
                return
            else:
                QMessageBox.warning(
                    self,
                    "Provider Not Found",
                    f"Provider '{provider_name}' is no longer available.",
                )
                return

        # Play the item
        is_episode = item_type == "episode"
        self.play_item(item_data, is_episode=is_episode, item_type=item_type)
