"""Exercise the real spawned recorder and native MPV using synthetic local media."""

from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import io
import math
import multiprocessing
from pathlib import Path
import threading
import time
from types import SimpleNamespace

from PySide6.QtCore import QProcess, QTimer
from PySide6.QtGui import QImage


def _fixtures(fixture, directory):
    import av

    with av.open(str(fixture)) as source:
        first = None
        last = 0
        for packet in source.demux():
            if packet.dts is None:
                continue
            pts = packet.pts if packet.pts is not None else packet.dts
            start = min(packet.dts, pts) * packet.time_base
            end = (max(packet.dts, pts) + packet.duration) * packet.time_base
            first = start if first is None else min(first, start)
            last = max(last, end)
        if first is None or last <= first:
            raise ValueError('The smoke fixture has no timed media packets')
        # Container duration omits encoder priming/padding. Preserve the complete
        # packet clock span so repeated AAC/B-frame packets never overlap.
        duration = last - first
        repeats = math.ceil(12 / duration)
        payload = io.BytesIO()
        with av.open(payload, 'w', format='mpegts') as output:
            streams = {
                stream.index: output.add_stream_from_template(stream, opaque=True)
                for stream in source.streams
                if stream.type in ('video', 'audio')
            }
            for repeat in range(repeats):
                with av.open(str(fixture)) as current:
                    for packet in current.demux():
                        if packet.dts is None or packet.stream.index not in streams:
                            continue
                        offset = round(repeat * duration / packet.time_base)
                        packet.dts += offset
                        if packet.pts is not None:
                            packet.pts += offset
                        packet.stream = streams[packet.stream.index]
                        output.mux(packet)
        epoch = payload.getvalue()
    audio_path = directory / 'native audio replacement.m4a'
    with av.open(str(fixture)) as source, av.open(str(audio_path), 'w') as output:
        audio = source.streams.audio[0]
        destination = output.add_stream_from_template(audio, opaque=True)
        for packet in source.demux(audio):
            if packet.dts is not None:
                packet.stream = destination
                output.mux(packet)
    # Independently muxed epochs intentionally restart both audio/video timestamps.
    return epoch * 3, float(duration * repeats * 3), audio_path, av.library_versions


class TimeshiftSmoke:
    PROPERTIES = (
        'path',
        'time-pos',
        'pause',
        'speed',
        'force-window',
        'current-vo',
        'video-out-params',
        'audio-params',
        'user-data/qitv-timeshift',
        'user-data/osc/margins',
    )

    def __init__(self, app, fixture, directory, render_path, *, player_type):
        self.app = app
        self.directory = Path(directory)
        self.directory.mkdir(parents=True, exist_ok=True)
        self.render_path = render_path
        self.capture_audio = False
        self.capture_path = render_path
        self.started = time.monotonic()
        self.phase = 'starting'
        self.phase_started = self.started
        self.failure = None
        self.completed = False
        self.properties = {}
        self.last_query = 0.0
        self.stop_source = threading.Event()
        self.requests = []
        self.result = {'checks': []}
        payload, duration, self.audio_path, libraries = _fixtures(fixture, self.directory)
        self.source_duration = duration
        self.result['libraries'] = libraries
        self.result['source_clock_resets'] = 2
        self.result['source_duration'] = duration
        owner = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass

            def do_GET(self):
                owner.requests.append(self.path)
                if self.path != '/live.ts':
                    self.send_error(404)
                    return
                self.send_response(200)
                self.send_header('Content-Type', 'video/mp2t')
                self.send_header('Connection', 'close')
                self.end_headers()
                rate = len(payload) / duration * 2
                started = time.monotonic()
                try:
                    for offset in range(0, len(payload), 188 * 32):
                        delay = max(0, started + offset / rate - time.monotonic())
                        if owner.stop_source.wait(delay):
                            return
                        self.wfile.write(payload[offset : offset + 188 * 32])
                        self.wfile.flush()
                    owner.stop_source.wait(60)
                except BrokenPipeError, ConnectionResetError, OSError:
                    pass

        self.server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
        self.server.daemon_threads = True
        self.server_thread = threading.Thread(
            target=lambda: self.server.serve_forever(poll_interval=0.05),
            daemon=True,
        )
        self.server_thread.start()
        self.url = f'http://127.0.0.1:{self.server.server_port}/live.ts'

        class ReaderPauseProbe(player_type):
            def _sample(self, item, name, value):
                super()._sample(item, name, value)
                if (
                    owner.phase == 'seek-loading'
                    and name == 'pause'
                    and value is True
                    and item is self._active
                    and item.buffered
                    and item.resume
                    and not item.cancelled
                ):
                    # Observe the real IPC transition; a GUI polling timer can
                    # miss the entire temporary pause on a fast local reader.
                    owner._check('temporary_reader_pause_observed')
                    owner._change('repeated-seek')
                    self.seek_buffer(3.5)

        self.player = ReaderPauseProbe(
            SimpleNamespace(
                keyboard_remote_mode=False,
                ssl_verify=True,
                timeshift_enabled=True,
                timeshift_max_mib=256,
                get_config_dir=lambda: str(self.directory),
            )
        )
        self.player.render = render_path is not None
        if render_path:
            render_path.parent.mkdir(parents=True, exist_ok=True)
            self.player.log_path = render_path.with_suffix('.log')
        self.player.errorOccurred.connect(self.fail)
        self.player.shutdownFinished.connect(self._shutdown_finished)
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
        self.deadline.start(75000)
        self.timer.start()
        self.player.play(self.url, is_live=True, content_id='buffered-local-fixture')

    def _change(self, phase):
        self.phase = phase
        self.phase_started = time.monotonic()
        self.properties.clear()
        self.last_query = 0.0

    def _check(self, name):
        self.result['checks'].append(name)

    def _query(self):
        phase, item = self.phase, self.player._active

        def received(name, response):
            if (
                self.phase == phase
                and self.player._active is item
                and response.get('error') == 'success'
            ):
                self.properties[name] = response.get('data')

        for name in self.PROPERTIES:
            self.player._send(
                ['get_property', name],
                lambda response, name=name: received(name, response),
            )

    def _tick(self):
        try:
            self._advance()
        except Exception as exc:
            self.fail(f'{type(exc).__name__}: {exc}')

    def _advance(self):
        if self.failure or self.phase == 'shutting-down':
            return
        now = time.monotonic()
        if self.player._ready and now - self.last_query >= 0.15:
            self._query()
            self.last_query = now
        item = self.player._active
        state = self.player._buffer_state
        p = self.properties
        settled = bool(item and item.loaded and not item.cancelled and not item.resume)
        if self.phase == 'starting':
            if not (
                settled
                and state.get('end', 0) >= 8
                and p.get('audio-params')
                and p.get('video-out-params')
                and p.get('pause') is False
                and (p.get('time-pos') or 0) > 0.1
            ):
                return
            recorder = self.player._recorder
            if recorder is None or recorder._child_pid is None:
                raise RuntimeError('Playback did not use a spawned recorder')
            self.result['recorder_pid'] = recorder._child_pid
            self.recording_directory = recorder._directory
            self.result['decoded_video'] = p['video-out-params']
            self.result['decoded_audio'] = p['audio-params']
            self._check('spawned_recorder_and_native_h264_aac_playback')
            self.player.set_speed(1.5)
            self._change('speed')
        elif self.phase == 'speed' and p.get('speed') == 1.5:
            self._check('buffered_playback_speed')
            self._change('seek-loading')
            self.player.seek_buffer(1.5)
        elif self.phase == 'repeated-seek':
            position = item.buffer_base + item.position / 1000 if item else 0
            if settled and p.get('pause') is False and 3.3 <= position <= 5.5:
                self._check('consecutive_rewind_preserves_playing_intent')
                self.player.toggle_pause()
                self._change('pausing')
        elif self.phase == 'pausing' and p.get('pause') is True:
            self.player.seek_buffer(2.5)
            self._change('paused-seek')
        elif self.phase == 'paused-seek':
            position = item.buffer_base + item.position / 1000 if item else 0
            if settled and p.get('pause') is True and 2.3 <= position <= 2.8:
                self._check('rewind_preserves_explicit_pause')
                self.player.go_live()
                self._change('go-live')
        elif self.phase == 'go-live':
            position = item.buffer_base + item.position / 1000 if item else 0
            if (
                settled
                and p.get('pause') is False
                and p.get('speed') == 1
                and 0 <= state.get('end', 0) - position <= 5
            ):
                self._check('go_live_resumes_at_live_edge_and_resets_speed')
                self._change('clock-resets')
        elif self.phase == 'clock-resets':
            if state.get('end', 0) < self.source_duration * 2 / 3 + 2:
                return
            if state.get('start', 0) > 0.5:
                raise RuntimeError('Clock restart unexpectedly discarded retained history')
            self.result['retained_after_resets'] = dict(state)
            self._check('continuous_recording_across_two_clock_resets')
            if self.render_path:
                self.player._control(['script-message-to', 'uosc', 'set-min-visibility', '1'])
                self._change('rendering')
            else:
                self._native_replace()
        elif self.phase == 'rendering':
            margins = p.get('user-data/osc/margins') or {}
            if now - self.phase_started < 1 or margins.get('t', 0) <= 0:
                return
            self.result['audio_render_state' if self.capture_audio else 'render_state'] = dict(p)
            if self.app.platformName() == 'xcb':
                screen = self.app.primaryScreen()
                if screen is None or not screen.grabWindow(0, 0, 0, 640, 480).save(
                    str(self.capture_path)
                ):
                    raise RuntimeError('Could not capture the native player window')
                self._change('captured')
            else:
                self._change('capturing')
                self.player._send(
                    ['screenshot-to-file', str(self.capture_path), 'window'],
                    self._captured,
                )
        elif self.phase == 'captured':
            image = QImage(str(self.capture_path))
            if image.isNull() or image.width() < 320 or image.height() < 240:
                raise RuntimeError('MPV did not render a native window capture')
            state_key = 'audio_render_state' if self.capture_audio else 'render_state'
            if self.result[state_key].get('current-vo') in (None, 'null'):
                raise RuntimeError('MPV did not initialize native video output')
            if self.capture_audio:
                self.result['rendered_audio_frame'] = str(self.capture_path)
                self._check('native_audio_controls_capture')
                self._shutdown()
            else:
                self.result['rendered_frame'] = str(self.capture_path)
                self._check('native_timeshift_video_and_uosc_capture')
                self._native_replace()
        elif self.phase == 'native-replacement':
            if (
                self.player._recorder is not None
                or not settled
                or p.get('path') != str(self.audio_path)
                or p.get('pause') is not False
                or not p.get('audio-params')
                or (p.get('time-pos') or 0) < 0.1
            ):
                return
            if (p.get('user-data/qitv-timeshift') or {}).get('active'):
                raise RuntimeError('Native replacement kept the old recording timeline')
            if p.get('force-window') not in ('yes', True):
                raise RuntimeError('Audio-only media has no native controls window')
            self._check('native_file_replacement_detaches_recorder')
            self._check('audio_only_media_retains_native_controls_window')
            if self.render_path:
                self.capture_audio = True
                self.capture_path = self.render_path.with_stem(self.render_path.stem + '-audio')
                self.player._control(['script-message-to', 'uosc', 'set-min-visibility', '1'])
                self._change('rendering')
            else:
                self._shutdown()

    def _shutdown(self):
        self._change('shutting-down')
        self.player.shutdown()
        self.kill_deadline.start(10000)

    def _captured(self, response):
        if response.get('error') != 'success':
            self.fail(f'Native capture failed: {response.get("error")}')
        else:
            self._change('captured')

    def _native_replace(self):
        # The same native loadfile operation used by uosc's Open file menu.
        self.player._control(['loadfile', str(self.audio_path), 'replace'])
        self._change('native-replacement')

    def _shutdown_finished(self):
        if self.player.is_running():
            self.fail('shutdownFinished preceded native teardown')
            return
        if not self.failure:
            if self.phase != 'shutting-down':
                self.fail(f'Unexpected shutdown during {self.phase}')
                return
            if self.requests != ['/live.ts']:
                self.fail(f'Time-shift reopened the upstream source: {self.requests}')
                return
            if self.recording_directory.exists():
                self.fail('Time-shift left a recording directory after shutdown')
                return
            self._check('single_upstream_connection_and_clean_shutdown')
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
        self.failure = self.failure or 'Native shutdown exceeded ten seconds'
        for process in self.player.findChildren(QProcess):
            if process.state() != QProcess.ProcessState.NotRunning:
                process.kill()
        QTimer.singleShot(1000, lambda: self.app.exit(1))

    def report(self):
        log_path = self.player.log_path
        if not self.failure and log_path and log_path.is_file():
            with log_path.open(encoding='utf-8', errors='replace') as log:
                for line in log:
                    if '][e][uosc]' in line or '][e][qitv]' in line:
                        self.failure = f'MPV interface script failed: {line.strip()}'
                        break
        self.stop_source.set()
        self.server.shutdown()
        self.server.server_close()
        self.server_thread.join(1)
        self.result.update(
            ok=self.failure is None and self.completed,
            failure=self.failure,
            phase=self.phase,
            duration_seconds=round(time.monotonic() - self.started, 3),
            http_requests=self.requests,
            processes_reaped=not self.player.is_running() and not multiprocessing.active_children(),
        )
        return self.result
