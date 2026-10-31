"""A packaged QiTV loads the libVLC it ships, never an installed VLC (services/vlc_bundle.py)."""

from pathlib import Path
import tempfile
import unittest
from unittest import mock

from services import vlc_bundle
from services.vlc_bundle import (
    ENV_LIB,
    ENV_MODULES,
    ENV_PLUGINS,
    bundled_vlc,
    driver_paths,
    use_bundled_vlc,
)

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


class DriverPathTests(unittest.TestCase):
    """The bundled libva and libvdpau look for drivers where Ubuntu keeps them: a Linux app
    names the folders this distribution keeps them in."""

    def setUp(self):
        self.root = self.new_root()
        added = mock.patch.object(vlc_bundle, "ADDED", set())
        added.start()
        self.addCleanup(added.stop)

    def new_root(self) -> Path:
        return Path(self.enterContext(tempfile.TemporaryDirectory()))

    def driver(self, folder: str, name: str) -> None:
        path = self.root / folder / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(b"")

    def test_each_distributions_folders(self):
        layouts = {
            "Debian, Ubuntu": "usr/lib/x86_64-linux-gnu",
            "Fedora, openSUSE": "usr/lib64",
            "Arch": "usr/lib",
        }
        for distribution, lib in layouts.items():
            with self.subTest(distribution):
                self.root = self.new_root()
                self.driver(f"{lib}/dri", "radeonsi_drv_video.so")
                self.driver(f"{lib}/vdpau", "libvdpau_radeonsi.so.1")
                self.assertEqual(
                    driver_paths(self.root),
                    {
                        "LIBVA_DRIVERS_PATH": str(self.root / lib / "dri"),
                        "VDPAU_DRIVER_PATH": str(self.root / lib / "vdpau"),
                    },
                )

    def test_a_folder_without_drivers_is_passed_over(self):
        self.driver("usr/lib64/dri", "radeonsi_dri.so")  # OpenGL's driver, not VA-API's
        self.driver("usr/lib/dri", "iHD_drv_video.so")
        self.assertEqual(
            driver_paths(self.root), {"LIBVA_DRIVERS_PATH": str(self.root / "usr/lib/dri")}
        )
        self.assertEqual(driver_paths(self.root / "missing"), {})

    def use(self, system: str, environ: dict[str, str]) -> bool:
        make_bundle(self.root, system)
        drivers = {"LIBVA_DRIVERS_PATH": "/usr/lib64/dri", "VDPAU_DRIVER_PATH": "/usr/lib64/vdpau"}
        return use_bundled_vlc(
            top=str(self.root),
            system=system,
            environ=environ,
            load=lambda path: None,
            drivers=lambda: drivers,
        )

    def test_a_linux_app_names_them_unless_the_user_did(self):
        environ = {"VDPAU_DRIVER_PATH": "/opt/vdpau"}
        self.assertTrue(self.use("Linux", environ))
        self.assertEqual(environ["LIBVA_DRIVERS_PATH"], "/usr/lib64/dri")
        self.assertEqual(environ["VDPAU_DRIVER_PATH"], "/opt/vdpau")  # the user's own
        self.assertEqual(vlc_bundle.ADDED, {"LIBVA_DRIVERS_PATH"})

    def test_other_systems_name_none(self):
        for system in ("Darwin", "Windows"):
            with self.subTest(system):
                self.root = self.new_root()
                environ: dict[str, str] = {}
                self.assertTrue(self.use(system, environ))
                self.assertNotIn("LIBVA_DRIVERS_PATH", environ)
                self.assertEqual(vlc_bundle.ADDED, set())


if __name__ == "__main__":
    unittest.main()
