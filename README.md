# QiTV - IPTV and STB Client

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

![Screenshot 2024-05-20 at 20 43 47](https://github.com/ozankaraali/QiTV/assets/19486728/5f8dc256-d359-44e1-a995-4bfc3c3be74a)


## Disclaimer

This application bundles [a list of publicly available IPTV channels](https://github.com/iptv-org/iptv) from around the world. The channels are not hosted by this application or respective repository. The application simply creates a convenient way to browse a publicly available media database. The developer of this application has no affiliation with the content providers. The content provided can be removed at any time and we have no control over it. The developer assumes no liability and is not responsible for any legal issues caused by the misuse of this application.

No video files are stored in this repository, the application bundles open-sourced [iptv-org](https://github.com/iptv-org/iptv) playlist for quick startup, users can delete that playlist entry if they want to from their computer. If any links/channels in this application infringe on your rights as a copyright holder, they may be removed by sending a [pull request](https://github.com/iptv-org/iptv/pulls) or opening an [issue](https://github.com/iptv-org/iptv/issues/new?assignees=freearhey&labels=removal+request&template=--removal-request.yml&title=Remove%3A+). However, note that we have **no control** over the destination of the link, and just removing the link from the playlist will not remove its contents from the web. Note that linking does not directly infringe copyright because no copy is made on the site providing the link, and thus this is **not** a valid reason to send a DMCA notice to GitHub. To remove this content from the web, you should contact the web host that's actually hosting the content (**not** GitHub, nor the maintainers of this repository).


## Contributing

Pull requests are welcome. For major changes, please open an issue first to discuss what you would like to change.

This project is in early phase. If you want to change any function, feel free to do. You could refactor, propose architecture changes, design assets, add new features, provide CI/CD things and build for other platforms. Basically, all changes that can improve this software are welcome.

## License

This software licensed under [MIT](https://github.com/ozankaraali/QiTV/blob/main/LICENSE).
