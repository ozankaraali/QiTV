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
        popen.assert_called_once_with(["open", "-a", "/Applications/VLC.app", "http://x/s"])

    def test_vlc_linux_launches_found_binary(self):
        with (
            mock.patch("platform.system", return_value="Linux"),
            mock.patch("shutil.which", return_value="/usr/bin/vlc"),
            mock.patch("subprocess.Popen") as popen,
        ):
            result = external_players.launch_vlc("http://x/s")
        self.assertEqual(result, (True, ""))
        popen.assert_called_once_with(["/usr/bin/vlc", "--started-from-file", "http://x/s"])

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

    def test_mpv_subprocess_fallback(self):
        with (
            mock.patch.dict(sys.modules, {"mpv": None}),
            mock.patch("platform.system", return_value="Linux"),
            mock.patch("shutil.which", return_value="/usr/bin/mpv"),
            mock.patch("subprocess.Popen") as popen,
        ):
            result = external_players.launch_mpv("http://x/s")
        self.assertEqual(result, (True, ""))
        popen.assert_called_once_with(["/usr/bin/mpv", "http://x/s"])

    def test_mpv_python_player_reused(self):
        fake = mock.MagicMock()
        with mock.patch.dict(sys.modules, {"mpv": fake}):
            self.assertEqual(external_players.launch_mpv("a"), (True, ""))
            self.assertEqual(external_players.launch_mpv("b"), (True, ""))
        fake.MPV.assert_called_once()
        fake.MPV.return_value.play.assert_has_calls([mock.call("a"), mock.call("b")])


if __name__ == "__main__":
    unittest.main()
