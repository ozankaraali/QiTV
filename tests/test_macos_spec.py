"""The macOS bundle's Info.plist (qitv-macos.spec's info_plist), read with ast: the spec is
never run (PyInstaller's names aren't there)."""

import ast
from pathlib import Path
import unittest

from services import file_types
from services.open_paths import VIDEO_EXTENSIONS

SPEC = Path(__file__).resolve().parent.parent / "qitv-macos.spec"
EXTENSIONS = "public.filename-extension"
MIME = "public.mime-type"

# The bundle's imports: {UTI: (conforms to, tags)}.
IMPORTS = {
    # macOS 13 (the oldest QiTV runs on) doesn't declare these: the identifiers other apps use
    "org.matroska.mkv": (["public.movie"], {EXTENSIONS: ["mkv"], MIME: ["video/x-matroska"]}),
    "org.xiph.ogg-video": (["public.movie"], {EXTENSIONS: ["ogv"], MIME: ["video/ogg"]}),
    # macOS 13 declares these only as imports itself (CoreTypes; QuickTime Player for m2ts and
    # mts): QiTV's say exactly the same, so whichever import macOS takes, nothing changes
    "org.webmproject.webm": (
        ["public.movie"],
        {EXTENSIONS: ["webm"], MIME: ["video/webm", "audio/webm"]},
    ),
    "com.adobe.flash.video": (
        ["public.movie"],
        {EXTENSIONS: ["flv", "f4v", "f4p", "f4a", "f4b"], MIME: ["video/x-flv"]},
    ),
    "com.microsoft.windows-media-wmv": (
        ["public.movie", "com.microsoft.advanced-systems-format"],
        {EXTENSIONS: ["wmv"], MIME: ["video/x-ms-wmv"]},
    ),
    "public.avchd-mpeg-2-transport-stream": (
        ["public.mpeg-2-transport-stream"],
        {EXTENSIONS: ["mts", "m2ts"]},
    ),
}


def _bundle_call():
    tree = ast.parse(SPEC.read_text(encoding="utf-8"))
    return next(
        node
        for node in ast.walk(tree)
        if isinstance(node, ast.Call) and getattr(node.func, "id", "") == "BUNDLE"
    )


def _keyword(call, name):
    return next(k.value for k in call.keywords if k.arg == name)


def info_plist():
    """The info_plist's keys with literal values (APP_VERSION and other names left out)."""
    plist = _keyword(_bundle_call(), "info_plist")
    found = {}
    for key, value in zip(plist.keys, plist.values):
        try:
            found[ast.literal_eval(key)] = ast.literal_eval(value)
        except ValueError:
            pass  # a name the spec computes
    return found


class MacBundleTypesTests(unittest.TestCase):
    def setUp(self):
        self.plist = info_plist()

    def test_document_types_alternate_and_complete(self):
        types = self.plist["CFBundleDocumentTypes"]
        self.assertEqual(len(types), 1)
        video = types[0]
        self.assertEqual(video["CFBundleTypeName"], "Video")
        self.assertEqual(video["CFBundleTypeRole"], "Viewer")
        self.assertEqual(video["LSHandlerRank"], "Alternate")  # installing takes no default
        claimed = video["LSItemContentTypes"]
        self.assertEqual(len(claimed), len(set(claimed)))
        self.assertEqual(set(file_types.MAC_UTIS), VIDEO_EXTENSIONS)
        for ext in sorted(VIDEO_EXTENSIONS):
            for uti in file_types.MAC_UTIS[ext]:
                self.assertIn(uti, claimed, ext)
        # nothing more than the video types (no public.movie: that's every movie there is)
        self.assertEqual(set(claimed), {u for utis in file_types.MAC_UTIS.values() for u in utis})
        self.assertIn("public.mpeg-2-transport-stream", claimed)  # .ts: Open With
        for ext in file_types.MAC_DEFAULT_VIDEO:  # what the Settings button makes default
            self.assertIn(file_types.MAC_UTIS[ext][0], claimed)

    def test_imports_every_type_macos_may_not_declare(self):
        imported = {d["UTTypeIdentifier"]: d for d in self.plist["UTImportedTypeDeclarations"]}
        self.assertEqual(imported.keys(), IMPORTS.keys())
        for uti, (conforms, tags) in IMPORTS.items():
            with self.subTest(uti=uti):
                declared = imported[uti]
                self.assertTrue(declared["UTTypeDescription"])
                self.assertEqual(declared["UTTypeConformsTo"], conforms)
                self.assertEqual(declared["UTTypeTagSpecification"], tags)
                # a movie (a transport stream is one: UTCoreTypes.h)
                self.assertTrue({"public.movie", "public.mpeg-2-transport-stream"} & set(conforms))
                extensions = tags["public.filename-extension"]
                videos = [ext for ext in extensions if ext in VIDEO_EXTENSIONS]
                self.assertTrue(videos)
                for ext in videos:
                    self.assertEqual(file_types.MAC_UTIS[ext][0], uti)
        self.assertNotIn("UTExportedTypeDeclarations", self.plist)  # QiTV owns no type

    def test_the_bundle_is_the_one_launch_services_is_told(self):
        bundle_id = ast.literal_eval(_keyword(_bundle_call(), "bundle_identifier"))
        self.assertEqual(bundle_id, file_types.MAC_BUNDLE_ID)
        self.assertEqual(self.plist["CFBundleIdentifier"], file_types.MAC_BUNDLE_ID)


if __name__ == "__main__":
    unittest.main()
