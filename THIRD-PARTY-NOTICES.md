# Third-party notices

QiTV is licensed under the MIT license (see `LICENSE`). The packaged apps (the downloads on the
Releases page) also contain the software below, each under its own license. This file, `LICENSE`
and the `licenses/` folder are inside every packaged app.

## VLC (libVLC and its plugins)

- libVLC and libVLCcore: GNU Lesser General Public License, version 2.1 or later
  (LGPL-2.1-or-later). Some VLC plugins: GNU General Public License, version 2 or later
  (GPL-2.0-or-later).
- QiTV ships VLC's own build unchanged: on macOS from VLC.app, on Windows from VLC's installer
  (VideoLAN), on Linux from the distribution's VLC packages.
- Source code: <https://www.videolan.org/vlc/> and <https://code.videolan.org/videolan/vlc>.
- License texts: `licenses/vlc/COPYING` (GPL-2.0) and `licenses/vlc/COPYING.LIB` (LGPL-2.1),
  VLC's own copies from its 3.0.20 source.

## Qt and Qt for Python (PySide6, shiboken6)

- Their packages state "LGPL-3.0-only OR GPL-2.0-only OR GPL-3.0-only"; QiTV uses them under the
  GNU Lesser General Public License, version 3 (LGPLv3).
- Source code: <https://download.qt.io/official_releases/QtForPython/> and
  <https://code.qt.io/>.
- License texts: `licenses/qt/LGPL-3.0.txt` and `licenses/qt/GPL-3.0.txt` (the LGPLv3 adds
  permissions to the GPLv3, so both apply).

## Python libraries

Each license as the installed package's metadata states it.

QiTV's own dependencies:

| Library | License |
| --- | --- |
| aiohttp | Apache-2.0 AND MIT |
| m3u8 | MIT |
| mpv | GPLv2+ or LGPLv2.1+ |
| orjson | MPL-2.0 AND (Apache-2.0 OR MIT) |
| packaging | Apache-2.0 OR BSD-2-Clause |
| pycryptodomex | BSD, Public Domain |
| PyQtDarkTheme-fork | MIT |
| PySide6 | LGPL-3.0-only OR GPL-2.0-only OR GPL-3.0-only |
| python-vlc | LGPL-2.1+ |
| requests | Apache-2.0 |
| tzlocal | MIT |
| urllib3 | MIT |
| URLObject | Public Domain |

The libraries they need (as `uv.lock` resolves them):

| Library | License |
| --- | --- |
| aiodns | MIT |
| aiohappyeyeballs | PSF-2.0 |
| aiosignal | Apache 2.0 |
| attrs | MIT |
| brotli | MIT |
| certifi | MPL-2.0 |
| cffi | MIT-0 |
| charset-normalizer | MIT |
| darkdetect | BSD-3-Clause |
| frozenlist | Apache-2.0 |
| idna | BSD-3-Clause |
| multidict | Apache License 2.0 |
| propcache | Apache-2.0 |
| pycares | MIT |
| pycparser | BSD-3-Clause |
| PySide6_Addons | LGPL-3.0-only OR GPL-2.0-only OR GPL-3.0-only |
| PySide6_Essentials | LGPL-3.0-only OR GPL-2.0-only OR GPL-3.0-only |
| shiboken6 | LGPL-3.0-only OR GPL-2.0-only OR GPL-3.0-only |
| tzdata (Windows only) | Apache-2.0 |
| yarl | Apache-2.0 |

QiTV 1.14.x bundled MPV (GPLv3); 2.0 does not. The `mpv` library above only lets QiTV hand a
stream to an MPV player you installed yourself ("Play in MPV").
