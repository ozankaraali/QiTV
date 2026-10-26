import sys
import unittest
from unittest import mock

from services import external_players


class ExternalPlayersTest(unittest.TestCase):
    def setUp(self):
        patcher = mock.patch.object(external_players, "_external_mpv_player", None)
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_vlc_macos_opens_app(self):
        def exists(path):
            return path == "/Applications/VLC.app"

        with (
            mock.patch("platform.system", return_value="Darwin"),
            mock.patch("os.path.exists", side_effect=exists),
            mock.patch("subprocess.Popen") as popen,
        ):
            result = external_players.launch_vlc("http://x/s")
        self.assertEqual(result, (True, ""))
        popen.assert_called_once_with(
            ["open", "-a", "/Applications/VLC.app", "--", "http://x/s"], env=mock.ANY
        )

    def test_vlc_linux_launches_found_binary(self):
        with (
            mock.patch("platform.system", return_value="Linux"),
            mock.patch("shutil.which", return_value="/usr/bin/vlc"),
            mock.patch("subprocess.Popen") as popen,
        ):
            result = external_players.launch_vlc("http://x/s")
        self.assertEqual(result, (True, ""))
        popen.assert_called_once_with(
            ["/usr/bin/vlc", "--started-from-file", "--", "http://x/s"], env=mock.ANY
        )

    def test_vlc_linux_missing(self):
        with (
            mock.patch("platform.system", return_value="Linux"),
            mock.patch("shutil.which", return_value=None),
            mock.patch("subprocess.Popen") as popen,
        ):
            ok, msg = external_players.launch_vlc("http://x/s")
        self.assertFalse(ok)
        self.assertIn("VLC Media Player is not installed", msg)
        popen.assert_not_called()

    def test_vlc_popen_failure_reported(self):
        with (
            mock.patch("platform.system", return_value="Linux"),
            mock.patch("shutil.which", return_value="/usr/bin/vlc"),
            mock.patch("subprocess.Popen", side_effect=OSError("boom")),
        ):
            ok, msg = external_players.launch_vlc("http://x/s")
        self.assertFalse(ok)
        self.assertEqual(msg, "Failed to launch VLC: boom")

    def test_mpv_missing_everywhere(self):
        with (
            mock.patch.dict(sys.modules, {"mpv": None}),
            mock.patch("platform.system", return_value="Linux"),
            mock.patch("shutil.which", return_value=None),
            mock.patch("os.path.exists", return_value=False),
            mock.patch("subprocess.Popen") as popen,
        ):
            ok, msg = external_players.launch_mpv("http://x/s")
        self.assertFalse(ok)
        self.assertIn("MPV Media Player is not installed", msg)
        popen.assert_not_called()

    def test_vlc_asks_as_the_channel_does_in_a_window_of_its_own(self):  # 2.8
        http = {"user_agent": "MAG200 stbapp", "referrer": "http://site.test/"}
        with (
            mock.patch("platform.system", return_value="Linux"),
            mock.patch("shutil.which", return_value="/usr/bin/vlc"),
            mock.patch("subprocess.Popen") as popen,
        ):
            self.assertEqual(external_players.launch_vlc("http://x/s", **http), (True, ""))
        popen.assert_called_once_with(
            [
                "/usr/bin/vlc",
                "--no-one-instance",  # a running VLC would drop the options
                "--http-user-agent=MAG200 stbapp",
                "--http-referrer=http://site.test/",
                "--",
                "http://x/s",
            ],
            env=mock.ANY,
        )

        def exists(path):
            return path == "/Applications/VLC.app"

        with (
            mock.patch("platform.system", return_value="Darwin"),
            mock.patch("os.path.exists", side_effect=exists),
            mock.patch("subprocess.Popen") as popen,
        ):
            external_players.launch_vlc("http://x/s", user_agent="MAG200 stbapp")
        popen.assert_called_once_with(
            [
                "/Applications/VLC.app/Contents/MacOS/VLC",  # `open` passes no options
                "--http-user-agent=MAG200 stbapp",
                "--",
                "http://x/s",
            ],
            env=mock.ANY,
        )

    def test_mpv_asks_as_the_channel_does(self):
        with (
            mock.patch.dict(sys.modules, {"mpv": None}),
            mock.patch("platform.system", return_value="Linux"),
            mock.patch("shutil.which", return_value="/usr/bin/mpv"),
            mock.patch("subprocess.Popen") as popen,
        ):
            external_players.launch_mpv("http://x/s", user_agent="MAG200 stbapp", referrer="r")
        popen.assert_called_once_with(
            ["/usr/bin/mpv", "--user-agent=MAG200 stbapp", "--referrer=r", "--", "http://x/s"],
            env=mock.ANY,
        )

    def test_python_mpv_is_told_each_streams_agent(self):
        played = []

        class Player(dict):
            def __init__(self, **_options):
                super().__init__({"user-agent": "libmpv", "referrer": ""})

            def play(self, url):
                played.append((url, self["user-agent"], self["referrer"]))

        fake = mock.Mock(MPV=Player)
        with mock.patch.dict(sys.modules, {"mpv": fake}):
            external_players.launch_mpv("http://x/1", user_agent="MAG200 stbapp", referrer="r")
            external_players.launch_mpv("http://x/2")  # another stream: its own (none)
        self.assertEqual(
            played, [("http://x/1", "MAG200 stbapp", "r"), ("http://x/2", "libmpv", "")]
        )

    def test_mpv_subprocess_fallback(self):
        with (
            mock.patch.dict(sys.modules, {"mpv": None}),
            mock.patch("platform.system", return_value="Linux"),
            mock.patch("shutil.which", return_value="/usr/bin/mpv"),
            mock.patch("subprocess.Popen") as popen,
        ):
            result = external_players.launch_mpv("http://x/s")
        self.assertEqual(result, (True, ""))
        popen.assert_called_once_with(["/usr/bin/mpv", "--", "http://x/s"], env=mock.ANY)

    def test_url_that_looks_like_an_option_is_not_one(self):
        # An untrusted URL beginning with "-" became a player option.
        url = "--script=/tmp/evil.lua"
        with (
            mock.patch.dict(sys.modules, {"mpv": None}),
            mock.patch("platform.system", return_value="Linux"),
            mock.patch("shutil.which", side_effect=lambda name: f"/usr/bin/{name}"),
            mock.patch("subprocess.Popen") as popen,
        ):
            external_players.launch_mpv(url)
            external_players.launch_vlc(url)
        self.assertEqual(popen.call_count, 2)
        for call in popen.call_args_list:
            argv = call.args[0]
            self.assertEqual(argv[-2:], ["--", url])

    def test_a_player_never_sees_the_apps_own_vlc(self):
        # VLC_PLUGIN_PATH made an installed VLC scan the bundle's plugins as well.
        environ = {
            "PATH": "/usr/bin",
            "VLC_PLUGIN_PATH": "/tmp/_MEI1/vlc/plugins",
            "PYTHON_VLC_LIB_PATH": "/tmp/_MEI1/vlc/libvlc.so.5",
            "PYTHON_VLC_MODULE_PATH": "/tmp/_MEI1/vlc/plugins",
        }
        for system in ("Linux", "Windows", "Darwin"):
            with self.subTest(system):
                env = external_players.player_env(environ, system=system, frozen=True)
                self.assertEqual(env, {"PATH": "/usr/bin"})
        self.assertIn("VLC_PLUGIN_PATH", environ)  # the app's own environment is kept

    def test_a_frozen_linux_app_gives_a_player_the_original_library_path(self):
        # PyInstaller's documented pattern: LD_LIBRARY_PATH points into the bundle.
        bundle = {"LD_LIBRARY_PATH": "/tmp/_MEI1", "LD_LIBRARY_PATH_ORIG": "/opt/lib"}
        env = external_players.player_env(bundle, system="Linux", frozen=True)
        self.assertEqual(env["LD_LIBRARY_PATH"], "/opt/lib")
        env = external_players.player_env({"LD_LIBRARY_PATH": "/tmp/_MEI1"}, "Linux", True)
        self.assertNotIn("LD_LIBRARY_PATH", env)  # it was not set before the app started
        for system, frozen in (("Linux", False), ("Windows", True)):
            with self.subTest(system=system, frozen=frozen):
                env = external_players.player_env(bundle, system=system, frozen=frozen)
                self.assertEqual(env["LD_LIBRARY_PATH"], "/tmp/_MEI1")

    def test_players_start_with_that_environment(self):
        bundle = {"PATH": "/usr/bin", "VLC_PLUGIN_PATH": "/tmp/_MEI1/vlc/plugins"}
        with (
            mock.patch.dict("os.environ", bundle, clear=True),
            mock.patch.dict(sys.modules, {"mpv": None}),
            mock.patch("platform.system", return_value="Linux"),
            mock.patch("shutil.which", side_effect=lambda name: f"/usr/bin/{name}"),
            mock.patch("subprocess.Popen") as popen,
        ):
            external_players.launch_vlc("http://x/s")
            external_players.launch_mpv("http://x/s")
        self.assertEqual(popen.call_count, 2)
        for call in popen.call_args_list:
            self.assertEqual(call.kwargs["env"], {"PATH": "/usr/bin"})

    def test_find_vlc_names_the_player_launch_vlc_starts(self):
        with (
            mock.patch("platform.system", return_value="Darwin"),
            mock.patch("os.path.exists", side_effect=lambda p: p == "/Applications/VLC.app"),
        ):
            self.assertEqual(external_players.find_vlc(), "/Applications/VLC.app")
        with (
            mock.patch("platform.system", return_value="Linux"),
            mock.patch("shutil.which", return_value=None),
        ):
            self.assertIsNone(external_players.find_vlc())

    def test_find_mpv_prefers_python_mpv_then_the_executable(self):
        with mock.patch.dict(sys.modules, {"mpv": mock.MagicMock()}):
            self.assertEqual(external_players.find_mpv(), external_players.PYTHON_MPV)
        with (
            mock.patch.dict(sys.modules, {"mpv": None}),
            mock.patch("platform.system", return_value="Linux"),
            mock.patch("shutil.which", return_value="/usr/bin/mpv"),
        ):
            self.assertEqual(external_players.find_mpv(), "/usr/bin/mpv")
        with (
            mock.patch.dict(sys.modules, {"mpv": None}),
            mock.patch("platform.system", return_value="Linux"),
            mock.patch("shutil.which", return_value=None),
        ):
            self.assertIsNone(external_players.find_mpv())

    def test_the_missing_messages_start_with_one_line_for_a_toast(self):
        self.assertEqual(
            external_players.MPV_MISSING.partition("\n")[0], "MPV Media Player is not installed."
        )
        self.assertEqual(
            external_players.VLC_MISSING.partition("\n")[0], "VLC Media Player is not installed."
        )

    def test_mpv_python_player_reused(self):
        fake = mock.MagicMock()
        with mock.patch.dict(sys.modules, {"mpv": fake}):
            self.assertEqual(external_players.launch_mpv("a"), (True, ""))
            self.assertEqual(external_players.launch_mpv("b"), (True, ""))
        fake.MPV.assert_called_once()
        fake.MPV.return_value.play.assert_has_calls([mock.call("a"), mock.call("b")])


if __name__ == "__main__":
    unittest.main()
