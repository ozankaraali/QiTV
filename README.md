# QiTV - IPTV and STB Client

## Please always download this free software from [RELEASES](https://github.com/ozankaraali/QiTV/releases) instead of using others' distributions, which may have malware.

A cross-platform IPTV and STB player client. This time in Python with QT and LibVLC.

## Installation

You could download the software from [RELEASES](https://github.com/ozankaraali/QiTV/releases).

Alternatively, you could do:

```
git clone https://github.com/ozankaraali/QiTV/
cd QiTV
python3 -m venv venv
source venv/bin/activate
pip install -r requirements.txt
python main.py
```

## Usage

You could use this software as a IPTV player or as a STB client. It bundles [a list of publicly available IPTV channels](https://github.com/iptv-org/iptv) from around the world for you to start quickly using or test the application. You can delete that playlist entry if you want from your computer after registering your playlists / STB player details.
For further usage you need to enter your M3U Playlist or IPTV provider's STB player details to "Settings". When you save, if your authentication works, you will directly see the channel lists on the left side. Select a channel and it will begin shortly.

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

No video files are stored in this repository, the application bundles open-sourced [iptv-org](https://github.com/iptv-org/iptv) playlist for quick startup, users can delete that playlist entry if they want to from their computer. If any links/channels in this application infringe on your rights as a copyright holder, they may be removed by sending a [pull request](https://github.com/iptv-org/iptv/pulls) or opening an [issue](https://github.com/iptv-org/iptv/issues/new?assignees=freearhey&labels=removal+request&template=--removal-request.yml&title=Remove%3A+). However, note that we have **no control** over the destination of the link, and just removing the link from the playlist will not remove its contents from the web. Note that linking does not directly infringe copyright because no copy is made on the site providing the link, and thus this is **not** a valid reason to send a DMCA notice to GitHub. To remove this content from the web, you should contact the web host that's actually hosting the content (**not** GitHub, nor the maintainers of this repository).


## Contributing

Pull requests are welcome. For major changes, please open an issue first to discuss what you would like to change.

This project is in early phase. If you want to change any function, feel free to do. You could refactor, propose architecture changes, design assets, add new features, provide CI/CD things and build for other platforms. Basically, all changes that can improve this software are welcome.

## Acknowledgements

### LibVLC
This software uses code of <a href=https://www.videolan.org/vlc/libvlc.html>LibVLC</a> licensed under the <a href=https://www.gnu.org/licenses/lgpl-2.1.html>LGPLv2.1</a> and its source can be downloaded <a href=https://github.com/ozankaraali/QiTV>here</a>

### PySide6
PySide6 is available under both Open Source (LGPLv3/GPLv3) and commercial license. Using PyPi is the recommended installation source, because the content of the wheels is valid for both cases. For more information, refer to the <a href=https://www.qt.io/licensing/>Qt Licensing page</a>.
## License

This software licensed under [MIT](https://github.com/ozankaraali/QiTV/blob/main/LICENSE).
