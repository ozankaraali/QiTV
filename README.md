# QiTV - IPTV and STB Client

## Please always download this free software from [RELEASES](https://github.com/ozankaraali/QiTV/releases) instead of using others' distributions, which may have malware.

A cross-platform IPTV and STB client built with Python and Qt, using a bundled standalone MPV player with the uosc interface.

## Installation

Download the application for your platform from [RELEASES](https://github.com/ozankaraali/QiTV/releases). Internal playback includes MPV and does not require an installed MPV or VLC. The native runtime targets Windows 10 or newer, macOS 13 or newer (separate Intel and Apple Silicon downloads), and Linux x86-64 with glibc 2.35 or newer and X11/XWayland.

### Running from source

Source installs require Python 3.14. Python 3.15 is not yet supported by the Qt/theme dependencies. Building the private player also requires native build tools; see below.

Install [uv](https://docs.astral.sh/uv/getting-started/installation/), then:

```bash
git clone https://github.com/ozankaraali/QiTV/
cd QiTV
uv sync --frozen
uv run --frozen python scripts/prepare_mpv.py
uv run --frozen python main.py
```

`uv sync` creates `.venv` and selects Python 3.14 from the project's requirements, installing the interpreter when needed. It does not depend on which interpreter your shell calls `python3`.

`python3` is a command name, not a guarantee that the latest Python is installed. Use `uv run python` to run QiTV with the project's selected interpreter.

The one-time native preparation builds pinned MPV, FFmpeg, libraries, and the uosc helper from source. Install C/C++ build tools, CMake, Ninja, NASM, Git, and Go 1.21 or newer first; the recipe selects its pinned Go toolchain automatically.

- **macOS:** Xcode 15 or newer command-line tools, plus `brew install cmake ninja nasm autoconf autoconf-archive automake libtool pkgconf`.
- **Linux:** a C/C++ compiler, `cmake ninja-build pkg-config nasm autoconf autoconf-archive automake libtool libltdl-dev`, and X11/OpenGL/ALSA/PulseAudio development headers. The exact Ubuntu package list is in [the build workflow](.github/workflows/main.yml).
- **Windows:** an x64 Visual Studio developer shell, LLVM (`clang`, `clang++`, `lld-link`, `llvm-rc`), CMake, Ninja, and NASM on PATH.

The recipe writes `native/mpv/` and a matching `native/mpv-sources-<target>.tar.gz`. PyInstaller packages this private runtime; it never searches the build machine for a replacement MPV installation. See [native redistribution instructions](native/REDISTRIBUTION.txt) for licenses and rebuilding.

Subsequent preparation reuses that runtime/source pair when the native inputs and toolchain still match. The cache identity includes the target, source pins, build recipe, patches, bundled MPV assets, compiler/SDK versions, build flags, and CI runner image. Reuse validates cached file hashes, executable permissions, and the corresponding-source archive checksum. Python application and packaging dependency changes alone do not invalidate it. Build tools remain necessary to identify the toolchain; use `uv run --frozen python scripts/prepare_mpv.py --rebuild` to force compilation.

### Linux Desktop Integration

After downloading the release binary or cloning the repo, you can install QiTV into your application menu so it persists across reboots:

**Using the release binary:**
```bash
chmod +x qitv-linux
git clone https://github.com/ozankaraali/QiTV/  # needed for install script and icon
cd QiTV
./scripts/install-linux.sh /path/to/qitv-linux
```

**Using the development (venv) setup:**
```bash
# After the initial setup above:
./scripts/install-linux.sh
```

This installs a desktop entry and icon so QiTV appears in your application menu. The desktop entry uses absolute paths, so it works after rebooting without needing to re-activate the virtual environment.

**Troubleshooting on Linux:**
- If the release binary fails with Qt/xcb plugin errors, install xcb-cursor: `sudo apt install libxcb-cursor0` (Debian/Ubuntu) or `sudo dnf install xcb-util-cursor` (Fedora).
- Make sure `~/.local/bin` is in your PATH. Add `export PATH="$HOME/.local/bin:$PATH"` to your `~/.bashrc` if needed.

## Usage

You could use this software as a IPTV player or as a STB client. It bundles [a list of publicly available IPTV channels](https://github.com/iptv-org/iptv) from around the world for you to start quickly using or test the application. You can delete that playlist entry if you want from your computer after registering your playlists / STB player details.
For further usage you need to enter your M3U Playlist or IPTV provider's STB player details to "Settings". When you save, if your authentication works, you will directly see the channel lists on the left side. Select a channel and it will begin shortly.

### Player modes

- **Internal (Bundled MPV):** QiTV launches its private MPV executable in MPV's own window, with bundled [uosc](https://github.com/tomasklaen/uosc) controls. This is not an embedded Qt video surface. Personal MPV configuration and scripts are not loaded.
- **VLC (External):** launches your installed VLC.
- **MPV (User Configuration):** launches your installed MPV with its normal configuration and scripts.

In the internal player, use the timeline to seek, right-click for the uosc menu, Space to pause, F or double-click for fullscreen, and Alt+P for picture-in-picture. QiTV manages resume positions and stops its private player when switching modes or closing the application.

On macOS, internal playback uses borderless fullscreen on the current desktop rather than a separate native fullscreen Space, keeping fullscreen/PiP transitions within MPV's window.

uosc subtitle downloads go to QiTV's configuration directory under `subtitles/`. On Linux, clipboard actions additionally require `xclip` or `xsel` on X11, or `wl-clipboard` on Wayland. Thumbnail previews and yt-dlp stream-quality selection are not bundled.

### Automatic Content Refresh

Saving provider connection changes reloads its content even when the provider name stays the same. Applying a provider or successfully verifying it, then saving, also requests a refresh. Unsaved provider edits are discarded when Settings is closed.

Channel, movie, and series catalogs are cached for six hours. QiTV checks freshness when opening a catalog and every five minutes while browsing a catalog or category. An open series keeps its cached seasons and episodes until you return to the catalog, so Back does not re-fetch them. Failed background refreshes leave the previous list available.

Refresh Content remains available to force an immediate reload. Logos and posters load independently and do not disable Back or list selection.

Large channel lists populate progressively, keeping Back and selection available while rows are added. Navigating away cancels the pending population; sorting, search, and selection are restored when the list is complete.

### Portable Mode

By default, QiTV stores configuration and cache files in system-specific directories:
- **Windows**: `%APPDATA%\qitv`
- **macOS**: `~/Library/Application Support/qitv`
- **Linux**: `~/.config/qitv`

To enable **portable mode** (useful for USB drives or keeping everything in one folder), simply create an empty file named `portable.txt` in the same directory as the QiTV executable or script. When portable mode is enabled, all configuration and cache files will be stored in the program directory instead.

###  Exporting Content to M3U

QiTV allows you to export content to M3U format for use in VLC or other media players. Click the "Export" button to access the export menu with these options:

- **Export Cached Content**: Quickly exports only the content you've already browsed and loaded into the cache. This is fast but may not include all episodes if you haven't navigated through all seasons.

- **Export Complete (Fetch All)**: For STB series content, this option fetches all seasons and episodes before exporting. It shows a progress dialog and may take some time depending on the number of series, but ensures a complete export with all episodes.

- **Export All Live Channels**: For STB providers, exports all available live TV channels from the cache.

Note: The exported M3U files contain stream URLs that VLC and most media players can handle directly.

![Screenshot 2024-05-20 at 20 43 47](https://github.com/ozankaraali/QiTV/assets/19486728/5f8dc256-d359-44e1-a995-4bfc3c3be74a)


## Disclaimer

This application bundles [a list of publicly available IPTV channels](https://github.com/iptv-org/iptv) from around the world. The channels are not hosted by this application or respective repository. The application simply creates a convenient way to browse a publicly available media database. The developer of this application has no affiliation with the content providers. The content provided can be removed at any time and we have no control over it. The developer assumes no liability and is not responsible for any legal issues caused by the misuse of this application.

QiTV does not host IPTV video content. The repository includes a synthetic playback-test fixture and an open-sourced [iptv-org](https://github.com/iptv-org/iptv) playlist for quick startup; users can delete that playlist entry from their computer. If any links/channels in this application infringe on your rights as a copyright holder, they may be removed by sending a [pull request](https://github.com/iptv-org/iptv/pulls) or opening an [issue](https://github.com/iptv-org/iptv/issues/new?assignees=freearhey&labels=removal+request&template=--removal-request.yml&title=Remove%3A+). However, note that we have **no control** over the destination of the link, and just removing the link from the playlist will not remove its contents from the web. Note that linking does not directly infringe copyright because no copy is made on the site providing the link, and thus this is **not** a valid reason to send a DMCA notice to GitHub. To remove this content from the web, you should contact the web host that's actually hosting the content (**not** GitHub, nor the maintainers of this repository).


## Contributing

Pull requests are welcome. For major changes, please open an issue first to discuss what you would like to change.

This project is in early phase. If you want to change any function, feel free to do. You could refactor, propose architecture changes, design assets, add new features, provide CI/CD things and build for other platforms. Basically, all changes that can improve this software are welcome.

Dependency versions are maintained in `pyproject.toml` and `uv.lock`. Use `uv sync --frozen --dev` for the locked development environment. After updating dependencies, regenerate the hash-pinned runtime installation file rather than editing it by hand:

```bash
uv export --frozen --no-dev --no-emit-project --output-file requirements.txt
```

The native playback smoke uses a small synthetic H.264/AAC fixture and MPV's `fast` rendering profile for software-rendered CI environments. Linux smokes force a 60 Hz refresh rate to accommodate Xvfb. It verifies playback, window controls and packaging isolation, not GPU performance or picture quality. These CI rendering overrides are not applied to normal internal playback.

CI caches `native/mpv/` together with its matching source archive under an exact, target-specific key. Caches are saved only after source and frozen playback checks and packaging succeed; cache hits still run those checks. Missing, evicted, or invalid caches trigger a source build rather than using an incompatible runtime. GitHub can evict inactive caches, and cache entries are immutable: delete a damaged entry if it repeatedly fails integrity checks so a successful rebuild can replace it.

## Acknowledgements

### MPV, FFmpeg, and uosc
QiTV ships a separate [MPV](https://mpv.io/) executable built with FFmpeg and native media libraries. The combined player is distributed under GPLv3-or-later; component licenses and build provenance accompany it. Every release includes the matching `mpv-sources-<target>.tar.gz` archives.

The bundled [uosc](https://github.com/tomasklaen/uosc) interface and Ziggy helper are LGPLv2.1, with additional notices for their dependencies and fonts. Their source archive and licenses are included in `assets/mpv/uosc/`. See [the uosc notice](assets/mpv/uosc/NOTICE.txt) and [native redistribution instructions](native/REDISTRIBUTION.txt).

### PySide6
PySide6 is available under both Open Source (LGPLv3/GPLv3) and commercial license. Using PyPi is the recommended installation source, because the content of the wheels is valid for both cases. For more information, refer to the <a href=https://www.qt.io/licensing/>Qt Licensing page</a>.
## License

This software licensed under [MIT](https://github.com/ozankaraali/QiTV/blob/main/LICENSE).
