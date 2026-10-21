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

## Python runtime

The apps contain the CPython 3.14.8 interpreter and standard library as `uv` installs it: the
python-build-standalone build of release 20261009. On macOS and Linux that build links the
libraries below into the interpreter (`libpython`); on Windows they are separate files, and the
app contains those of the modules QiTV uses. Tcl/Tk, and on Linux Berkeley DB and the X11
libraries, are separate modules QiTV does not use: they are not in the apps.

| Component | Version | License, as its source states it | Text | Platforms |
| --- | --- | --- | --- | --- |
| CPython | 3.14.8 | Python Software Foundation License Version 2 (PSF-2.0); its LICENSE also carries the older BeOpen, CNRI and CWI terms | `licenses/python/CPython.txt` (the installed Python's `lib/python3.14/LICENSE.txt`) | all |
| Software CPython incorporates (HACL\*, mimalloc and others) | 3.14.8 | each as listed | `licenses/python/CPython-incorporated-software.rst` (CPython 3.14.8's `Doc/license.rst`) | all |
| OpenSSL | 3.5.9 | Apache-2.0 | `licenses/python/OpenSSL.txt` (its `LICENSE.txt`) | all |
| libffi | 3.8.0; on Windows 3.4.7 (CPython's own copy) | MIT | `licenses/python/libffi.txt` (3.8.0's `LICENSE`; 3.4.7's has the same terms) | all |
| bzip2 | 1.0.8 | its own BSD-style license (SPDX: bzip2-1.0.6) | `licenses/python/bzip2.txt` (its `LICENSE`) | all |
| xz (liblzma) | 5.8.4 | 0BSD | `licenses/python/liblzma.txt` (its `COPYING.0BSD`) | all |
| SQLite | 3.53.1 | public domain: no license text (<https://sqlite.org/copyright.html>) | none | all |
| Expat | 2.9.0; on Windows 2.8.5 (CPython's own copy) | MIT | `licenses/python/expat.txt` (2.9.0's `COPYING`) | all |
| mpdecimal (libmpdec) | 4.0.0 | BSD-2-Clause | `licenses/python/mpdecimal.txt` (its `COPYRIGHT.txt`) | all |
| Zstandard | 1.5.7 | BSD-3-Clause (zstd is BSD or GPLv2; used under the BSD license) | `licenses/python/zstd.txt` (its `LICENSE`) | all |
| zlib | 1.3.2 | Zlib | `licenses/python/zlib.txt` (its `LICENSE`) | Linux (macOS uses the system's) |
| zlib-ng | 2.2.4 | Zlib | `licenses/python/zlib-ng.txt` (its `LICENSE.md`) | Windows |
| ncurses | 6.5 | X11 (MIT-style) | `licenses/python/ncurses.txt` (its `COPYING`) | Linux (macOS uses the system's) |
| libedit | 20240808-3.1 | BSD-3-Clause | `licenses/python/libedit.txt` (its `COPYING`) | Linux (macOS uses the system's) |
| libuuid | 1.0.3 | BSD-3-Clause | `licenses/python/libuuid.txt` (its `COPYING`, and the license its `uuid.h` states) | macOS, Linux |

- Source code: <https://www.python.org/downloads/source/>, and the build at
  <https://github.com/astral-sh/python-build-standalone> (release 20261009), which names each
  library's source and checksum.
- The texts come from the installed Python where it has them (`CPython.txt`) and otherwise from
  each project's own release of that version.

## The phone remote's page

The page QiTV serves to a phone (`services/remote/web/`) bundles these JavaScript libraries,
rebuilt from their npm releases by `scripts/remote_vendor.sh`.

| Component | Version | License | Text |
| --- | --- | --- | --- |
| noble-curves | 2.4.0 | MIT | `licenses/web/noble-curves.txt` |
| noble-ciphers | 2.4.0 | MIT | `licenses/web/noble-ciphers.txt` |
| noble-hashes | 2.4.0 | MIT | `licenses/web/noble-hashes.txt` |
| jsQR | 1.4.0 | Apache-2.0 | `licenses/web/jsQR.txt` |

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
| segno | BSD License |
| tzlocal | MIT |
| urllib3 | MIT |

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

## Online services

- **OpenSubtitles.com** finds and serves the subtitles of the Subtitles panel's "Search
  OpenSubtitles…" (<https://www.opensubtitles.com>). QiTV talks to its public REST API
  (<https://api.opensubtitles.com/api/v1>) with an API key and, when you sign in, your
  account; no OpenSubtitles code is part of QiTV. The subtitles come from its users, under
  the site's own terms.
