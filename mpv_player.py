"""Asynchronous, isolated standalone MPV playback (no media preflight)."""

from dataclasses import dataclass, field
import json
import math
from pathlib import Path
import shutil
import sys
import tempfile
import time
import uuid

from PySide6.QtCore import QObject, QProcess, QProcessEnvironment, QTimer, Signal
from PySide6.QtNetwork import QLocalSocket
import certifi

from services.mpv_runtime import get_bundled_mpv_path, get_resource_root


@dataclass
class _Playback:
    url: str
    content_id: str
    is_live: bool | None
    resume: int
    entry_id: int | None = None
    position: int = 0
    duration: int = 0
    loaded: bool = False
    cancelled: bool = False
    stopping: bool = False
    stop_deadline: float = 0.0
    observers: dict = field(default_factory=dict)


class MpvPlayer(QObject):
    playing = Signal()
    stopped = Signal()
    errorOccurred = Signal(str)
    mediaEnded = Signal(str)
    positionChanged = Signal(str, int, int)
    backRequested = Signal()
    forwardRequested = Signal()
    channelNextRequested = Signal()
    channelPrevRequested = Signal()
    shutdownFinished = Signal()

    _START_TIMEOUT = 10.0
    _REQUEST_TIMEOUT = 5.0
    _MAX_FRAME = 1024 * 1024

    def __init__(self, config_manager, parent=None):
        super().__init__(parent)
        self.config_manager = config_manager
        self._process = None
        self._socket = None
        self._directory = None
        self._endpoint = ""
        self._buffer = bytearray()
        self._requests = {}
        self._request_id = 0
        self._observer_id = 0
        self._active = None
        self._pending = None
        self._ready = False
        self._closing = False
        self._failed = False
        self._started_at = 0.0
        self._quit_at = None
        self._quit_stage = 0
        self._settings_checked_at = 0.0
        self._remote_mode = None
        self._tick_timer = QTimer(self)
        self._tick_timer.setInterval(100)
        self._tick_timer.timeout.connect(self._tick)
        self._position_timer = QTimer(self)
        self._position_timer.setInterval(1000)
        self._position_timer.timeout.connect(self._flush_position)

    def play(self, url, *, is_live=None, content_id=None, resume_position=None):
        self._pending = _Playback(
            str(url),
            str(content_id or ""),
            is_live,
            max(0, int(resume_position or 0)),
        )
        if self._active:
            self._active.cancelled = True
            self._flush_position()
        if self._closing:
            return  # Restart only after the previous child has actually exited.
        if not self.is_running():
            self._start()
        elif self._ready:
            self._advance()

    def stop(self):
        self._pending = None
        if self._active:
            self._active.cancelled = True
            self._flush_position()
            if self._ready and not self._closing:
                self._advance()

    def shutdown(self):
        self._pending = None
        if self._closing:
            return
        if not self.is_running():
            self.shutdownFinished.emit()
            return
        self._closing = True
        self._quit_at = time.monotonic()
        self._quit_stage = 0
        if self._active:
            self._active.cancelled = True
            self._flush_position()
        if self._ready:
            self._snapshot(self._begin_quit)
        else:
            self._begin_quit()

    def is_running(self):
        return self._process is not None and self._process.state() != QProcess.NotRunning

    def toggle_pause(self):
        self._control(["cycle", "pause"])

    def toggle_mute(self):
        self._control(["cycle", "mute"])

    def toggle_fullscreen(self):
        self._control(["script-message-to", "qitv", "fullscreen"])

    def toggle_pip(self):
        self._control(["script-message-to", "qitv", "pip"])

    def _control(self, command):
        if self._ready and not self._closing:
            self._send(command)

    def _start(self):
        root = get_resource_root()
        script = root / "assets" / "mpv" / "qitv.lua"
        try:
            executable = get_bundled_mpv_path()
            if not script.is_file():
                raise FileNotFoundError("QiTV's MPV control script is missing. Reinstall QiTV.")
            helper = (
                root
                / "native"
                / "mpv"
                / "bin"
                / ("ziggy.exe" if sys.platform == "win32" else "ziggy")
            )
            for asset in (
                helper,
                root / "assets/mpv/uosc/main.lua",
                root / "assets/mpv/uosc/input.conf",
                root / "assets/mpv/fonts/uosc_icons.otf",
                root / "assets/mpv/fonts/uosc_textures.ttf",
            ):
                if not asset.is_file():
                    raise FileNotFoundError(
                        f"QiTV's bundled uosc interface is incomplete ({asset.name}). "
                        "Prepare the native runtime again or reinstall QiTV."
                    )
            (Path(self.config_manager.get_config_dir()) / "subtitles").mkdir(
                parents=True, exist_ok=True
            )
            # Watch-later output, including explicit MPV commands, stays private.
            self._directory = tempfile.mkdtemp(
                prefix="qitv-", dir=None if sys.platform == "win32" else "/tmp"
            )
            if sys.platform == "win32":
                self._endpoint = "qitv-" + uuid.uuid4().hex
            else:
                # macOS's sockaddr_un path limit is only 104 bytes. mkdtemp is 0700.
                self._endpoint = str(Path(self._directory) / "ipc")
        except (OSError, RuntimeError) as exc:
            self._pending = None
            self.errorOccurred.emit(str(exc))  # Local installation errors, never media URLs.
            self.stopped.emit()
            return
        self._ready = self._closing = self._failed = False
        self._quit_at = None
        self._remote_mode = None
        self._buffer.clear()
        self._requests.clear()
        process = QProcess(self)
        self._process = process
        environment = QProcessEnvironment.systemEnvironment()
        environment.insert("MPV_UOSC_ZIGGY", str(helper))
        environment.insert("SSL_CERT_FILE", certifi.where())
        process.setProcessEnvironment(environment)
        # Never collect MPV diagnostics containing URL paths, queries or credentials.
        process.setStandardOutputFile(QProcess.nullDevice())
        process.setStandardErrorFile(QProcess.nullDevice())
        process.setProgram(executable)
        process.setArguments(self._mpv_arguments())
        process.started.connect(lambda: self._process_started(process))
        process.finished.connect(lambda code, status: self._finished(process, code, status))
        process.errorOccurred.connect(lambda error: self._process_error(process, error))
        self._started_at = time.monotonic()
        self._tick_timer.start()
        process.start()

    def _mpv_arguments(self):
        root = get_resource_root()
        assert self._directory is not None
        controls = (
            "menu,gap,<video,audio>subtitles,<has_many_audio>audio,<has_many_video>video,"
            "<has_many_edition>editions,gap,space,<video,audio>speed,space,shuffle,"
            "loop-playlist,loop-file,gap,prev,items,next,gap,fullscreen"
        )
        # uosc's ! prefix keeps downloaded subtitles in managed storage for local files too.
        subtitles = "!" + str(Path(self.config_manager.get_config_dir()) / "subtitles")
        arguments = [
            "--no-config",
            "--load-scripts=no",
            "--ytdl=no",
            "--osc=no",
            "--osd-bar=no",
            "--border=no",
            "--title-bar=no",
            "--input-conf=" + str(root / "assets/mpv/uosc/input.conf"),
            "--osd-fonts-dir=" + str(root / "assets/mpv/fonts"),
            "--script=" + str(root / "assets/mpv/uosc"),
            # -append accepts one literal value, including commas and Unicode.
            "--script-opts-append=uosc-controls=" + controls,
            "--script-opts-append=uosc-subtitles_directory=" + subtitles,
            "--input-default-bindings=yes",
            "--input-terminal=no",
            "--terminal=no",
            "--idle=yes",
            "--force-window=no",
            "--keep-open=no",
            "--save-position-on-quit=no",
            "--resume-playback=no",
            "--tls-ca-file=" + certifi.where(),
            "--stop-playback-on-init-failure=yes",
            "--watch-later-directory=" + str(Path(self._directory) / "watch_later"),
            "--title=QiTV Player",
            "--hwdec=auto-safe",
            "--input-ipc-server=" + self._endpoint,
            "--script=" + str(root / "assets" / "mpv" / "qitv.lua"),
        ]
        if sys.platform == "darwin":
            # Keep fullscreen/PiP on this desktop, without asynchronous Spaces transitions.
            arguments.append("--native-fs=no")
        return arguments

    def _process_started(self, process):
        if process is not self._process:
            return
        if self._closing:
            process.terminate()
        else:
            self._connect()

    def _connect(self):
        if self._socket is not None or self._closing:
            return
        socket = QLocalSocket(self)
        self._socket = socket
        socket.connected.connect(lambda: self._connected(socket))
        socket.readyRead.connect(lambda: self._read(socket))
        socket.disconnected.connect(lambda: self._disconnected(socket))
        socket.errorOccurred.connect(lambda error: self._socket_error(socket))
        socket.connectToServer(self._endpoint)

    def _drop_socket(self):
        socket, self._socket = self._socket, None
        if socket:
            socket.abort()
            socket.deleteLater()

    def _socket_error(self, socket):
        if socket is not self._socket:
            return
        if self._ready:
            self._disconnected(socket)
        else:
            self._drop_socket()  # Startup retries are bounded by _START_TIMEOUT.

    def _connected(self, socket):
        if socket is not self._socket or self._closing:
            return
        self._send(["client_name"], self._handshake)

    def _handshake(self, response):
        if response.get("error") != "success":
            self._fail(
                "MPV's control connection could not be initialized. Reinstall the bundled runtime."
            )
            return
        # A Lua acknowledgement proves that Lua support and our script are present.
        self._send(["script-message-to", "qitv", "hello", response["data"]])

    def _tick(self):
        now = time.monotonic()
        if self._closing:
            assert self._quit_at is not None
            elapsed = now - self._quit_at
            if elapsed >= 2.5 and self._quit_stage < 2:
                self._quit_stage = 2
                self._process.kill()
            elif elapsed >= 1.5 and self._quit_stage < 1:
                self._quit_stage = 1
                self._process.terminate()
            return
        if not self._ready:
            if now - self._started_at >= self._START_TIMEOUT:
                self._fail(
                    "Bundled MPV did not become ready within 10 seconds. Reinstall QiTV or prepare its native runtime again."
                )
            elif self._process.state() == QProcess.Running:
                self._connect()
            return
        if any(deadline < now for deadline, callback in self._requests.values()):
            self._fail(
                "MPV stopped responding to playback controls. Select the media again to restart it."
            )
            return
        if self._active and self._active.stopping and now > self._active.stop_deadline:
            self._fail(
                "MPV could not stop the previous media. Select the media again to restart it."
            )
            return
        if now - self._settings_checked_at >= 2:
            self._settings_checked_at = now
            remote = bool(getattr(self.config_manager, "keyboard_remote_mode", False))
            if remote != self._remote_mode:
                self._remote_mode = remote
                self._send(["script-message-to", "qitv", "remote", "yes" if remote else "no"])

    def _send(self, command, callback=None):
        if not self._socket or self._socket.state() != QLocalSocket.ConnectedState:
            return
        self._request_id += 1
        request_id = self._request_id
        payload = json.dumps(
            {"command": command, "request_id": request_id},
            ensure_ascii=False,
            separators=(",", ":"),
        )
        self._requests[request_id] = (time.monotonic() + self._REQUEST_TIMEOUT, callback)
        if self._socket.write((payload + "\n").encode("utf-8")) < 0:
            self._fail("MPV's control connection failed. Select the media again to restart it.")

    def _read(self, socket):
        if socket is not self._socket:
            return
        self._buffer.extend(bytes(socket.readAll()))
        while (end := self._buffer.find(b"\n")) >= 0:
            line = self._buffer[:end]
            del self._buffer[: end + 1]
            if len(line) > self._MAX_FRAME:
                self._fail("MPV sent an oversized control message. Reinstall the bundled runtime.")
                return
            try:
                message = json.loads(line)
            except ValueError, UnicodeDecodeError:
                self._fail("MPV sent an invalid control message. Reinstall the bundled runtime.")
                return
            if isinstance(message, dict):
                self._message(message)
            if socket is not self._socket:
                return
        if len(self._buffer) > self._MAX_FRAME:
            self._fail("MPV sent an oversized control message. Reinstall the bundled runtime.")

    def _message(self, message):
        request = self._requests.pop(message.get("request_id"), None)
        if request:
            if request[1]:
                request[1](message)
            return
        event = message.get("event")
        if event == "client-message":
            args = message.get("args", [])
            if args == ["qitv", "ready"] and not self._closing:
                self._ready = True
                self._position_timer.start()
                self._settings_checked_at = 0.0
                self._tick()
                self._advance()
            elif len(args) == 2 and args[0] == "qitv" and not self._closing:
                signal = {
                    "back": self.backRequested,
                    "forward": self.forwardRequested,
                    "next": self.channelNextRequested,
                    "previous": self.channelPrevRequested,
                }.get(args[1])
                if signal:
                    signal.emit()
            return
        item = self._active
        if item is None:
            return
        if event == "start-file":
            item.entry_id = message.get("playlist_entry_id")
        elif event == "file-loaded":
            if item.cancelled or self._closing:
                return
            item.loaded = True
            for name in ("time-pos", "duration"):
                self._observer_id += 1
                item.observers[self._observer_id] = name
                self._send(["observe_property", self._observer_id, name])
            self.playing.emit()
        elif event == "property-change":
            name = item.observers.get(message.get("id"))
            if name:
                self._sample(item, name, message.get("data"))
        elif event == "end-file" and message.get("playlist_entry_id") == item.entry_id:
            if message.get("reason") == "redirect" and not item.cancelled:
                item.entry_id = None
                return
            natural = message.get("reason") == "eof" and not item.cancelled and not self._closing
            failed = message.get("reason") == "error" and not item.cancelled and not self._closing
            self._retire(item)
            if failed:
                self.errorOccurred.emit(
                    "MPV could not open or decode this media. Check the provider URL, account access and network connection."
                )
            if natural and item.is_live is not True and item.content_id:
                self.mediaEnded.emit(item.content_id)
            if not self._closing:
                self._advance()

    def _sample(self, item, name, value):
        if isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value):
            value = min(2147483647, max(0, round(value * 1000)))
            if name == "time-pos":
                item.position = value
            elif name == "duration":
                item.duration = value

    def _flush_position(self):
        item = self._active
        if (
            item
            and item.content_id
            and item.loaded
            and item.is_live is not True
            and item.duration > 0
        ):
            self.positionChanged.emit(item.content_id, item.position, item.duration)

    def _snapshot(self, done):
        item = self._active
        if not item or not item.loaded or not self._ready:
            done()
            return
        remaining = {"time-pos", "duration"}

        def received(name, response):
            if self._active is item:
                self._sample(item, name, response.get("data"))
            remaining.discard(name)
            if not remaining:
                if self._active is item:
                    self._flush_position()
                done()

        for name in tuple(remaining):
            self._send(["get_property", name], lambda reply, name=name: received(name, reply))

    def _advance(self):
        if not self._ready or self._closing:
            return
        item = self._active
        if item:
            if item.cancelled and not item.stopping:
                item.stopping = True
                item.stop_deadline = time.monotonic() + self._REQUEST_TIMEOUT
                self._snapshot(lambda: self._stop_item(item))
            return
        if self._pending is None:
            return
        item, self._pending = self._pending, None
        self._active = item
        # Per-file options do not leak from live playback into a later VOD.
        options = {
            "pause": "no",
            "cache": "yes",
            "tls-verify": "yes" if getattr(self.config_manager, "ssl_verify", True) else "no",
        }
        if item.is_live is True:
            options.update({"cache-secs": "2", "demuxer-readahead-secs": "2"})
        if item.resume and item.is_live is not True:
            # MPV applies start after demuxer readiness, not a racy early seek.
            options["start"] = str(item.resume / 1000)
        self._send(
            ["loadfile", item.url, "replace", -1, options],
            lambda reply: self._loaded_command(item, reply),
        )

    def _loaded_command(self, item, response):
        if self._active is not item or item.cancelled or self._closing:
            return
        if response.get("error") != "success":
            self._retire(item)
            self.errorOccurred.emit(
                "MPV rejected this media. Check the provider URL and bundled runtime."
            )
            self._advance()

    def _stop_item(self, item):
        if self._active is not item or self._closing:
            return
        # loadfile/stop return before unloading. An idle query covers cancellation
        # before start-file; otherwise end-file is the serialization boundary.
        self._send(
            ["stop"],
            lambda reply: self._send(
                ["get_property", "idle-active"], lambda response: self._stopped_idle(item, response)
            ),
        )

    def _stopped_idle(self, item, response):
        if self._active is item and response.get("data") is True:
            self._retire(item)
            self._advance()

    def _retire(self, item):
        if self._active is not item:
            return
        self._flush_position()
        for observer in item.observers:
            self._send(["unobserve_property", observer])
        self._active = None
        if self._pending is None:
            self.stopped.emit()

    def _begin_quit(self):
        if self._ready:
            self._send(["quit"])
        elif self._process and self._process.state() == QProcess.Running:
            self._process.terminate()

    def _fail(self, message):
        if self._failed or self._closing:
            return
        self._failed = True
        self.errorOccurred.emit(message)
        self.shutdown()

    def _disconnected(self, socket):
        if socket is not self._socket:
            return
        self._read(socket)
        if socket is not self._socket:
            return
        self._drop_socket()
        if self._closing:
            return
        # A normal window close also closes IPC. Give finished a turn before
        # treating a still-running child as an IPC failure.
        process = self._process
        QTimer.singleShot(150, lambda: self._check_disconnected(process))

    def _check_disconnected(self, process):
        if (
            process is self._process
            and self.is_running()
            and not self._closing
            and self._socket is None
        ):
            self._fail("MPV's control connection was lost. Select the media again to restart it.")

    def _process_error(self, process, error):
        if process is not self._process:
            return
        if error == QProcess.FailedToStart:
            self._fail(
                "Bundled MPV could not start. Reinstall QiTV or run uv run scripts/prepare_mpv.py for this platform."
            )
            self._finished(process, -1, QProcess.CrashExit)

    def _finished(self, process, code, status):
        if process is not self._process:
            return
        was_closing = self._closing
        self._closing = True
        if not was_closing:
            self._pending = None
        if self._socket:
            self._read(self._socket)
        if (code != 0 or status == QProcess.CrashExit) and not was_closing and not self._failed:
            self.errorOccurred.emit(
                "MPV exited unexpectedly. Select the media again; if it repeats, reinstall the bundled runtime."
            )
        if self._active:
            self._retire(self._active)
        else:
            self.stopped.emit()
        self._tick_timer.stop()
        self._position_timer.stop()
        self._drop_socket()
        self._requests.clear()
        self._buffer.clear()
        self._process = None
        process.deleteLater()
        if self._directory:
            shutil.rmtree(self._directory, ignore_errors=True)
            self._directory = None
        self._ready = self._closing = False
        self._quit_at = None
        self.shutdownFinished.emit()
        if self._pending:
            self._start()
