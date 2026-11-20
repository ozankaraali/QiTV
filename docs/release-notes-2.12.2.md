# QiTV 2.12.2

QiTV 2.12.2 starts faster on Windows and Linux: the VLC that ships inside QiTV reads each of its
plugins once at start, not twice.

**Download QiTV from [qitv.app](https://qitv.app), which picks the right build for your
computer, or from [Releases](https://github.com/ozankaraali/QiTV/releases). Please don't use
others' distributions, which may have malware.**

## Windows and Linux

- **VLC reads its plugins once.** The VLC inside QiTV looks in its own plugin folder by itself,
  and QiTV pointed it at that same folder a second time, so every plugin was read twice at each
  start: about 360 plugins on Windows and 385 on Linux. Now each is read once.
- **macOS is unchanged.** Its VLC already keeps a plugin cache, written when the app is built.
- **Another VLC's plugins are still left out.** If you set `VLC_PLUGIN_PATH` yourself, for
  another VLC, QiTV still leaves it out, as before.
