"""Finite source/frozen smoke of real bundled MPV; only serves synthetic localhost media."""

import argparse
import json
import os
from pathlib import Path
import re
import sys
import tempfile
import time
from types import SimpleNamespace
from typing import Any

if __package__ in (None, '') and not getattr(sys, 'frozen', False):
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

# This changes only the smoke's Qt application, never production MPV settings.
os.environ.setdefault('QT_QPA_PLATFORM', 'offscreen')

from PySide6.QtCore import QProcess, QTimer
from PySide6.QtNetwork import QHostAddress, QLocalSocket, QTcpServer
from PySide6.QtWidgets import QApplication

from mpv_player import MpvPlayer
from services.mpv_runtime import get_bundled_mpv_path


class HeadlessMpvPlayer(MpvPlayer):
    def _mpv_arguments(self):
        return super()._mpv_arguments() + ['--vo=null', '--ao=null', '--hwdec=no']


class LocalMediaServer(QTcpServer):
    def __init__(self, fixture, fail, parent=None):
        super().__init__(parent)
        self.media = fixture.read_bytes()
        self.fail = fail
        self.requests = []
        self.buffers = {}
        self.newConnection.connect(self._accept)
        if not self.listen(QHostAddress.SpecialAddress.LocalHost, 0):
            raise RuntimeError(self.errorString())
        self.url = f'http://127.0.0.1:{self.serverPort()}/fixture.mp4'

    def _accept(self):
        while self.hasPendingConnections():
            socket = self.nextPendingConnection()
            self.buffers[socket] = bytearray()
            socket.readyRead.connect(lambda socket=socket: self._read(socket))
            socket.disconnected.connect(lambda socket=socket: self._discard(socket))

    def _discard(self, socket):
        self.buffers.pop(socket, None)
        socket.deleteLater()

    def _read(self, socket):
        buffer = self.buffers.get(socket)
        if buffer is None:
            return
        buffer.extend(bytes(socket.readAll()))
        if len(buffer) > 16384:
            self.fail('Oversized localhost HTTP request')
            socket.abort()
            return
        if b'\r\n\r\n' not in buffer:
            return
        self.buffers.pop(socket)
        lines = buffer.decode('iso-8859-1').split('\r\n')
        method, path, _ = lines[0].split(' ', 2)
        headers = dict(line.split(':', 1) for line in lines[1:] if ':' in line)
        headers = {key.lower(): value.strip() for key, value in headers.items()}
        user_agent = headers.get('user-agent', '')
        self.requests.append({'method': method, 'path': path, 'user_agent': user_agent})
        if method != 'GET' or not re.search(r'mpv|lavf', user_agent, re.I):
            self.fail(f'Application HTTP preflight/non-MPV request: {method} {user_agent!r}')
            socket.write(b'HTTP/1.1 405 Method Not Allowed\r\nContent-Length: 0\r\n\r\n')
            socket.disconnectFromHost()
            return
        if path != '/fixture.mp4':
            self.fail(f'Unexpected media URL: {path}')
            socket.abort()
            return
        start, end = 0, len(self.media) - 1
        status = '200 OK'
        content_range = ''
        if 'range' in headers:
            match = re.fullmatch(r'bytes=(\d+)-(\d*)', headers['range'])
            if not match:
                self.fail(f'Unsupported media byte range: {headers["range"]}')
                socket.abort()
                return
            start = int(match[1])
            end = min(int(match[2]) if match[2] else end, end)
            if start > end:
                socket.write(b'HTTP/1.1 416 Range Not Satisfiable\r\nContent-Length: 0\r\n\r\n')
                socket.disconnectFromHost()
                return
            status = '206 Partial Content'
            content_range = f'Content-Range: bytes {start}-{end}/{len(self.media)}\r\n'
        body = self.media[start : end + 1]
        response = (
            f'HTTP/1.1 {status}\r\nContent-Type: video/mp4\r\n'
            f'Content-Length: {len(body)}\r\nAccept-Ranges: bytes\r\n'
            f'{content_range}Connection: close\r\n\r\n'
        ).encode('ascii')
        socket.write(response + body)
        socket.disconnectFromHost()


class Smoke:
    PROPERTIES = (
        'path',
        'time-pos',
        'video-out-params',
        'video-frame-info',
        'audio-params',
        'pause',
        'mute',
        'fullscreen',
        'ontop',
    )

    def __init__(self, app, fixture):
        self.app = app
        self.started = time.monotonic()
        self.phase = 'starting'
        self.failure = None
        self.completed = False
        self.result: dict[str, Any] = {'frozen': bool(getattr(sys, 'frozen', False)), 'checks': []}
        self.positions = []
        self.ended = []
        self.playing_count = 0
        self.stopped_count = 0
        self.properties = {}
        self.pending = {}
        self.request_id = 0
        self.last_query = 0.0
        self.buffer = bytearray()
        self.processes = []
        self.server = LocalMediaServer(fixture, self.fail)
        self.player = HeadlessMpvPlayer(
            SimpleNamespace(keyboard_remote_mode=False, ssl_verify=True)
        )
        self.player.playing.connect(self._playing)
        self.player.stopped.connect(self._stopped)
        self.player.errorOccurred.connect(self.fail)
        self.player.positionChanged.connect(self._position)
        self.player.mediaEnded.connect(self._ended)
        self.player.shutdownFinished.connect(self._shutdown_finished)
        self.probe = QLocalSocket()
        self.probe.readyRead.connect(self._read_probe)
        self.timer = QTimer()
        self.timer.setInterval(50)
        self.timer.timeout.connect(self._tick)
        self.deadline = QTimer()
        self.deadline.setSingleShot(True)
        self.deadline.timeout.connect(lambda: self.fail(f'Timed out in phase {self.phase}'))
        self.kill_deadline = QTimer()
        self.kill_deadline.setSingleShot(True)
        self.kill_deadline.timeout.connect(self._force_exit)

    def start(self):
        executable = Path(get_bundled_mpv_path()).resolve()
        root = Path(getattr(sys, '_MEIPASS', Path(__file__).resolve().parents[1]))
        native_root = (root / 'native' / 'mpv').resolve()
        if not executable.is_relative_to(native_root):
            raise RuntimeError(f'Bundled MPV resolved outside native/mpv: {executable}')
        if not (root / 'assets' / 'mpv' / 'qitv.lua').is_file():
            raise RuntimeError('QiTV MPV Lua asset is missing from resource root')
        self.result['executable'] = str(executable)
        self.result['fixture'] = str(self.server.url)
        self.deadline.start(45000)
        self.timer.start()
        self.player.play(self.server.url, is_live=False, content_id='first', resume_position=2000)

    def _playing(self):
        self.playing_count += 1

    def _stopped(self):
        self.stopped_count += 1

    def _position(self, content_id, position, duration):
        if content_id not in {'first', 'second', 'stop', 'shutdown'}:
            self.fail(f'Unknown position identity: {content_id!r}')
        self.positions.append((content_id, position, duration))

    def _ended(self, content_id):
        self.ended.append(content_id)
        if content_id != 'second':
            self.fail(f'Replaced/stopped media emitted EOF: {content_id!r}')

    def _read_probe(self):
        self.buffer.extend(bytes(self.probe.readAll()))
        while b'\n' in self.buffer:
            line, _, self.buffer = self.buffer.partition(b'\n')
            if not line.strip():
                continue
            message = json.loads(line)
            name = self.pending.pop(message.get('request_id'), None)
            if name and message.get('error') == 'success':
                self.properties[name] = message.get('data')

    def _query(self):
        if self.probe.state() != QLocalSocket.LocalSocketState.ConnectedState:
            return
        for name in self.PROPERTIES:
            self.request_id += 1
            self.pending[self.request_id] = name
            self.probe.write(
                (
                    json.dumps(
                        {
                            'command': ['get_property', name],
                            'request_id': self.request_id,
                        }
                    )
                    + '\n'
                ).encode()
            )

    def _check(self, name):
        self.result['checks'].append(name)

    def _change(self, phase, action):
        self.phase = phase
        self.properties.clear()
        self.pending.clear()
        action()
        self.last_query = 0.0

    def _tick(self):
        try:
            self._advance()
        except Exception as exc:
            self.fail(f'{type(exc).__name__}: {exc}')

    def _advance(self):
        if self.failure or self.phase == 'shutting-down':
            return
        if not self.processes:
            self.processes = self.player.findChildren(QProcess)
        if self.probe.state() == QLocalSocket.LocalSocketState.UnconnectedState:
            for process in self.processes:
                endpoint = next(
                    (
                        arg.split('=', 1)[1]
                        for arg in process.arguments()
                        if arg.startswith('--input-ipc-server=')
                    ),
                    None,
                )
                if endpoint:
                    if Path(process.program()).resolve() != Path(self.result['executable']):
                        raise RuntimeError('Backend did not launch the bundled MPV executable')
                    self.probe.connectToServer(endpoint)
                    break
        now = time.monotonic()
        if now - self.last_query >= 0.2:
            self._query()
            self.last_query = now
        p = self.properties
        if self.phase == 'starting':
            if not (self.playing_count and p.get('video-frame-info') and p.get('audio-params')):
                return
            video = p.get('video-out-params') or {}
            audio = p['audio-params']
            if not (
                video.get('w', 0) > 0 and video.get('h', 0) > 0 and audio.get('samplerate', 0) > 0
            ):
                return
            if not any(
                key == 'first' and pos >= 1900 and duration >= 3000
                for key, pos, duration in self.positions
            ):
                return
            self.result['decoded_video'] = video
            self.result['decoded_audio'] = audio
            self._check('real_h264_aac_decode_and_resume_position')
            self._check('isolated_user_config_and_scripts')
            self._change('pausing', self.player.toggle_pause)
        elif self.phase == 'pausing' and p.get('pause') is True:
            self._check('public_pause_command')
            self._change('muting', self.player.toggle_mute)
        elif self.phase == 'muting' and p.get('mute') is True:
            self._check('public_mute_command')
            self._change('fullscreen', self.player.toggle_fullscreen)
        elif self.phase == 'fullscreen' and p.get('fullscreen') is True:
            self._check('public_fullscreen_command')
            # PiP needs a real window; Main verifies it on the visible GUI.
            self._change('resuming', self.player.toggle_pause)
        elif self.phase == 'resuming' and p.get('pause') is False:
            self._check('public_resume_command')
            self._change(
                'replacement',
                lambda: self.player.play(self.server.url, is_live=False, content_id='second'),
            )
        elif self.phase == 'replacement' and 'second' in self.ended:
            if self.ended != ['second']:
                raise RuntimeError(f'Unexpected EOF identities: {self.ended}')
            if not any(key == 'second' and 0 <= pos < 1500 for key, pos, _ in self.positions):
                raise RuntimeError('Replacement did not start with its own position identity')
            self._check('replacement_identity_and_natural_end')
            self._change(
                'playing-to-stop',
                lambda: self.player.play(self.server.url, is_live=False, content_id='stop'),
            )
        elif self.phase == 'playing-to-stop' and any(
            key == 'stop' and pos >= 300 for key, pos, _ in self.positions
        ):
            self.stop_position_count = len(self.positions)
            self.stop_signal_count = self.stopped_count
            self._change('stopping', self.player.stop)
        elif self.phase == 'stopping' and self.stopped_count > self.stop_signal_count:
            if not any(key == 'stop' for key, _, _ in self.positions[self.stop_position_count :]):
                raise RuntimeError('Stop did not flush the final relevant position')
            self._check('stop_flushes_position_without_eof')
            self._change(
                'playing-to-shutdown',
                lambda: self.player.play(self.server.url, is_live=False, content_id='shutdown'),
            )
        elif self.phase == 'playing-to-shutdown' and any(
            key == 'shutdown' and pos >= 300 for key, pos, _ in self.positions
        ):
            self.shutdown_position_count = len(self.positions)
            self._change('shutting-down', self.player.shutdown)
            self.kill_deadline.start(10000)

    def _shutdown_finished(self):
        if self.player.is_running():
            self.fail('shutdownFinished emitted while MPV is still running')
            return
        if not self.failure:
            if self.phase != 'shutting-down':
                self.fail(f'Unexpected MPV shutdown during {self.phase}')
                return
            final = self.positions[self.shutdown_position_count :]
            if not any(key == 'shutdown' for key, _, _ in final):
                self.fail('Shutdown did not flush the final relevant position')
                return
            if self.ended != ['second']:
                self.fail(f'Stop/shutdown emitted EOF: {self.ended}')
                return
            if not self.server.requests:
                self.fail('MPV never requested the localhost fixture')
                return
            self._check('bounded_shutdown_and_final_position')
            self._check('only_player_http_get_no_application_preflight')
            self.completed = True
        self.timer.stop()
        self.deadline.stop()
        self.kill_deadline.stop()
        self.app.exit(1 if self.failure else 0)

    def fail(self, message):
        if self.failure:
            return
        self.failure = str(message)
        self.timer.stop()
        self.deadline.stop()
        self.player.shutdown()
        self.kill_deadline.start(10000)
        if not self.player.is_running():
            QTimer.singleShot(0, lambda: self.app.exit(1))

    def _force_exit(self):
        self.failure = self.failure or 'MPV shutdown exceeded ten seconds'
        for process in self.player.findChildren(QProcess):
            if process.state() != QProcess.ProcessState.NotRunning:
                process.kill()
        QTimer.singleShot(1000, lambda: self.app.exit(1))

    def report(self):
        self.result.update(
            {
                'ok': self.failure is None,
                'failure': self.failure,
                'phase': self.phase,
                'duration_seconds': round(time.monotonic() - self.started, 3),
                'http_requests': self.server.requests,
                'position_events': self.positions,
                'end_events': self.ended,
                'playing_events': self.playing_count,
                'stopped_events': self.stopped_count,
                'processes_reaped': not self.player.is_running()
                and all(
                    process.state() == QProcess.ProcessState.NotRunning
                    for process in self.player.findChildren(QProcess)
                ),
            }
        )
        return self.result


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        '--fixture',
        type=Path,
        required=True,
        help='Synthetic local H.264/AAC MP4, at least 3 seconds',
    )
    parser.add_argument(
        '--report',
        type=Path,
        required=True,
        help='JSON result (also works for windowed frozen apps)',
    )
    args = parser.parse_args(argv)
    result: dict[str, Any] = {'ok': False}
    exit_code = 1
    previous_mpv_home = os.environ.get('MPV_HOME')
    previous_excepthook = sys.excepthook
    try:
        with tempfile.TemporaryDirectory(prefix='qitv-mpv-smoke-') as directory:
            home = Path(directory)
            (home / 'mpv.conf').write_text(
                'pause=yes\nmute=yes\nvid=no\naid=no\n', encoding='utf-8'
            )
            (home / 'scripts').mkdir()
            (home / 'scripts' / 'must-not-load.lua').write_text(
                "mp.commandv('quit', '73')\n", encoding='utf-8'
            )
            os.environ['MPV_HOME'] = directory
            app = QApplication.instance() or QApplication(['qitv-mpv-smoke'])
            app.setQuitOnLastWindowClosed(False)
            smoke = Smoke(app, args.fixture.resolve(strict=True))
            sys.excepthook = lambda kind, value, traceback: smoke.fail(f'{kind.__name__}: {value}')
            QTimer.singleShot(0, smoke.start)
            exit_code = app.exec()
            if not smoke.completed and not smoke.failure:
                smoke.failure = f'Application exited before completing phase {smoke.phase}'
            result = smoke.report()
            if not result['processes_reaped']:
                result['ok'] = False
                result['failure'] = result['failure'] or 'MPV process survived smoke'
                exit_code = 1
    except Exception as exc:
        result['ok'] = False
        result['failure'] = f'{type(exc).__name__}: {exc}'
    finally:
        sys.excepthook = previous_excepthook
        if previous_mpv_home is None:
            os.environ.pop('MPV_HOME', None)
        else:
            os.environ['MPV_HOME'] = previous_mpv_home
        args.report.parent.mkdir(parents=True, exist_ok=True)
        args.report.write_text(json.dumps(result, indent=2) + '\n', encoding='utf-8')
        if sys.stdout is not None:
            print(json.dumps(result, indent=2))
    return 0 if result.get('ok') else (exit_code or 1)


if __name__ == '__main__':
    raise SystemExit(main())
