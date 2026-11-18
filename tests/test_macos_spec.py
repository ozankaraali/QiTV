"""The macOS bundle's Info.plist (qitv-macos.spec's info_plist), read with ast: the spec is
never run (PyInstaller's names aren't there)."""

import ast
from pathlib import Path
import unittest

from services import file_types
from services.open_paths import MUSIC_EXTENSIONS, VIDEO_EXTENSIONS

SPEC = Path(__file__).resolve().parent.parent / "qitv-macos.spec"
EXTENSIONS = "public.filename-extension"
MIME = "public.mime-type"
OSTYPE = "com.apple.ostype"
PLAYLISTS = frozenset({"m3u", "m3u8"})
# Each document type: its name and the extensions it claims (Open With lists QiTV for them).
DOCUMENT_TYPES = {"Video": VIDEO_EXTENSIONS, "Audio": MUSIC_EXTENSIONS, "Playlist": PLAYLISTS}

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
    # music: macOS 13 declares flac and wav only as imports itself (CoreTypes), so these copy
    # it exactly; it declares no Ogg or Opus type, and these are the identifiers other apps
    # import for them
    "org.xiph.flac": (["public.audio"], {EXTENSIONS: ["flac"], MIME: ["audio/flac"]}),
    "com.microsoft.waveform-audio": (
        ["public.audio"],
        {
            OSTYPE: [".WAV", "WAVE"],
            MIME: ["audio/vnd.wave", "audio/wav", "audio/wave", "audio/x-wav"],
            EXTENSIONS: ["wav", "wave", "bwf"],
        },
    ),
    "org.xiph.ogg-audio": (["public.audio"], {EXTENSIONS: ["ogg", "oga"], MIME: ["audio/ogg"]}),
    "org.xiph.opus": (["public.audio"], {EXTENSIONS: ["opus"]}),
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

    def document_types(self):
        return {kind["CFBundleTypeName"]: kind for kind in self.plist["CFBundleDocumentTypes"]}

    def test_document_types_alternate_and_complete(self):
        types = self.document_types()
        self.assertEqual(types.keys(), DOCUMENT_TYPES.keys())
        self.assertEqual(len(self.plist["CFBundleDocumentTypes"]), len(DOCUMENT_TYPES))
        self.assertEqual(set(file_types.MAC_UTIS), VIDEO_EXTENSIONS | MUSIC_EXTENSIONS | PLAYLISTS)
        for name, extensions in DOCUMENT_TYPES.items():
            with self.subTest(name=name):
                declared = types[name]
                self.assertEqual(declared["CFBundleTypeRole"], "Viewer")
                # installing takes no default
                self.assertEqual(declared["LSHandlerRank"], "Alternate")
                claimed = declared["LSItemContentTypes"]
                self.assertEqual(len(claimed), len(set(claimed)))
                # its types and nothing more (no public.movie or public.audio: that's every
                # movie, or every sound, there is)
                own = {uti for ext in extensions for uti in file_types.MAC_UTIS[ext]}
                self.assertEqual(set(claimed), own)
        self.assertIn("public.mpeg-2-transport-stream", types["Video"]["LSItemContentTypes"])
        for kind, name in (("videos", "Video"), ("music", "Audio")):
            # what each Settings button makes the default
            for ext in file_types._MAC_DEFAULTS[kind]:
                self.assertIn(file_types.MAC_UTIS[ext][0], types[name]["LSItemContentTypes"])

    def test_playlists_are_open_with_only(self):
        playlist = self.document_types()["Playlist"]
        self.assertEqual(playlist["LSItemContentTypes"], ["public.m3u-playlist"])  # macOS's own
        self.assertEqual(playlist["LSHandlerRank"], "Alternate")
        made_default = {
            file_types.MAC_UTIS[ext][0]
            for short_list in file_types._MAC_DEFAULTS.values()
            for ext in short_list
        }
        self.assertNotIn("public.m3u-playlist", made_default)

    def test_imports_every_type_macos_may_not_declare(self):
        imported = {d["UTTypeIdentifier"]: d for d in self.plist["UTImportedTypeDeclarations"]}
        self.assertEqual(imported.keys(), IMPORTS.keys())
        for uti, (conforms, tags) in IMPORTS.items():
            with self.subTest(uti=uti):
                declared = imported[uti]
                self.assertTrue(declared["UTTypeDescription"])
                self.assertEqual(declared["UTTypeConformsTo"], conforms)
                self.assertEqual(declared["UTTypeTagSpecification"], tags)
                # a movie (a transport stream is one: UTCoreTypes.h), or a sound
                kinds = {"public.movie", "public.mpeg-2-transport-stream", "public.audio"}
                self.assertTrue(kinds & set(conforms))
                extensions = tags["public.filename-extension"]
                own = [ext for ext in extensions if ext in VIDEO_EXTENSIONS | MUSIC_EXTENSIONS]
                self.assertTrue(own)
                for ext in own:
                    self.assertEqual(file_types.MAC_UTIS[ext][0], uti)
        self.assertNotIn("UTExportedTypeDeclarations", self.plist)  # QiTV owns no type

    def test_the_bundle_is_the_one_launch_services_is_told(self):
        bundle_id = ast.literal_eval(_keyword(_bundle_call(), "bundle_identifier"))
        self.assertEqual(bundle_id, file_types.MAC_BUNDLE_ID)
        self.assertEqual(self.plist["CFBundleIdentifier"], file_types.MAC_BUNDLE_ID)


if __name__ == "__main__":
    unittest.main()
