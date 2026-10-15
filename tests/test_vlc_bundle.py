"""A packaged QiTV loads the libVLC it ships, never an installed VLC (services/vlc_bundle.py)."""

from pathlib import Path
import tempfile
import unittest

from services.vlc_bundle import ENV_LIB, ENV_MODULES, ENV_PLUGINS, bundled_vlc, use_bundled_vlc

# What each PyInstaller spec puts at the bundle's top (sys._MEIPASS).
LAYOUTS = {
    "Darwin": (["libvlc.dylib", "libvlccore.dylib"], "vlc/plugins"),
    "Windows": (["libvlc.dll", "libvlccore.dll"], "plugins"),
    "Linux": (["libvlc.so.5.6.1", "libvlccore.so.9.0.1"], "vlc/plugins"),
}


def make_bundle(top: Path, system: str) -> None:
    files, plugins = LAYOUTS[system]
    for name in files:
        (top / name).write_bytes(b"")
    (top / plugins).mkdir(parents=True)


class BundledVlcTests(unittest.TestCase):
    def setUp(self):
        self.top = Path(self.enterContext(tempfile.TemporaryDirectory()))

    def test_each_platforms_layout(self):
        for system, (files, plugins) in LAYOUTS.items():
            with self.subTest(system=system):
                top = self.top / system
                top.mkdir()
                make_bundle(top, system)
                core, lib, found = bundled_vlc(top, system)
                self.assertEqual((lib.name, core.name), tuple(files))
                self.assertEqual(found, top / plugins)

    def test_none_when_anything_is_missing(self):
        make_bundle(self.top, "Darwin")
        (self.top / "libvlccore.dylib").unlink()
        self.assertIsNone(bundled_vlc(self.top, "Darwin"))
        self.assertIsNone(bundled_vlc(self.top, "Windows"))


class UseBundledVlcTests(unittest.TestCase):
    def setUp(self):
        self.top = Path(self.enterContext(tempfile.TemporaryDirectory()))
        make_bundle(self.top, "Darwin")
        self.loaded = []
        self.environ = {"VLC_PLUGIN_PATH": "/Applications/VLC.app/Contents/MacOS/plugins"}

    def load(self, path):
        self.loaded.append(Path(path).name)

    def use(self, **kwargs):
        options = dict(top=str(self.top), system="Darwin", environ=self.environ, load=self.load)
        options.update(kwargs)
        return use_bundled_vlc(**options)

    def test_a_packaged_app_points_python_vlc_at_its_own_libvlc(self):
        self.assertTrue(self.use())
        self.assertEqual(self.loaded, ["libvlccore.dylib", "libvlc.dylib"])  # the core first
        self.assertEqual(self.environ[ENV_LIB], str(self.top / "libvlc.dylib"))
        plugins = str(self.top / "vlc" / "plugins")
        self.assertEqual(self.environ[ENV_MODULES], plugins)
        self.assertEqual(self.environ[ENV_PLUGINS], plugins)  # never an installed VLC's too

    def test_a_source_run_leaves_python_vlc_alone(self):
        self.assertFalse(self.use(top=None))
        self.assertEqual(self.loaded, [])
        self.assertNotIn(ENV_LIB, self.environ)

    def test_a_bundle_without_libvlc_leaves_python_vlc_alone(self):
        (self.top / "libvlc.dylib").unlink()
        with self.assertLogs("services.vlc_bundle", "WARNING"):
            self.assertFalse(self.use())
        self.assertNotIn(ENV_LIB, self.environ)

    def test_a_libvlc_that_does_not_load_is_not_handed_to_python_vlc(self):
        # python-vlc exits the process when PYTHON_VLC_LIB_PATH does not load.
        def fail(path):
            raise OSError("dlopen failed")

        with self.assertLogs("services.vlc_bundle", "WARNING"):
            self.assertFalse(self.use(load=fail))
        self.assertNotIn(ENV_LIB, self.environ)
        self.assertNotIn(ENV_MODULES, self.environ)


if __name__ == "__main__":
    unittest.main()
