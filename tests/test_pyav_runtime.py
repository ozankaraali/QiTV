import hashlib
import io
import json
import os
from pathlib import Path
import tarfile
import tempfile
import unittest
from unittest.mock import patch
import zipfile

from scripts import prepare_mpv as native, prepare_pyav as pyav


class PrivatePyAVCacheTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.target = 'linux-x86_64'
        self.toolchain = {'compiler': 'compiler-one', 'abi': 'cp314'}
        for name in pyav.INPUTS:
            path = self.root / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(name, encoding='utf-8')
        self.inputs = pyav.input_hashes(self.root)
        self.key = pyav.native_cache_key(self.root, self.target, self.toolchain)
        self.directory = self.root / 'native' / 'pyav'
        self.directory.mkdir()
        self.wheel = self.directory / 'av-18.1.0-cp314-cp314-linux_x86_64.whl'
        with zipfile.ZipFile(self.wheel, 'w') as wheel:
            wheel.writestr('av/__init__.py', 'private package')
        self.manifest = {'sources': {}}
        self.members = {'recipe/' + name: (self.root / name).read_bytes() for name in pyav.INPUTS}
        for name in pyav.source_names(self.target):
            source = ('preferred source for ' + name).encode()
            artifact = {
                'filename': name + '.tar.gz',
                'sha256': hashlib.sha256(source).hexdigest(),
                'size': len(source),
                'license': 'LICENSE',
            }
            self.manifest['sources'][name] = artifact
            self.members['sources/' + artifact['filename']] = source
            license_name = 'licenses/' + name + '/LICENSE'
            self.members[license_name] = ('upstream ' + name + ' license').encode()
            license_path = self.directory / license_name
            license_path.parent.mkdir(parents=True, exist_ok=True)
            license_path.write_bytes(self.members[license_name])
        self.sources = self.directory.parent / f'pyav-sources-{self.target}.tar.gz'
        self.write_sources()
        self.metadata = {
            'schema': 1,
            'target': self.target,
            'cache_key': self.key,
            'inputs': self.inputs,
            'wheel': self.wheel.name,
            'wheel_sha256': native.sha256(self.wheel),
            'source_archive': self.sources.name,
            'source_sha256': native.sha256(self.sources),
            'sources': self.manifest['sources'],
            'files': native.runtime_inventory(self.directory),
            'ffmpeg': {
                'configuration': '--disable-autodetect --disable-gpl --disable-nonfree --enable-version3',
                'license': 'LGPL version 3 or later',
                'version': '8.1.2',
            },
            'native_libraries': {name: name + '.so' for name in pyav.LIBRARIES},
            'binary_inspection': {'avcodec.so': {'dependencies': ['libc.so.6']}},
        }
        self.write_metadata()

    def write_sources(self):
        with tarfile.open(self.sources, 'w:gz') as archive:
            for name, data in self.members.items():
                member = tarfile.TarInfo(name)
                member.size = len(data)
                archive.addfile(member, io.BytesIO(data))

    def write_metadata(self):
        (self.directory / 'bundle.json').write_text(json.dumps(self.metadata), encoding='utf-8')

    def reusable(self):
        return pyav.cached_runtime_valid(
            self.directory, self.sources, self.target, self.key, self.manifest, self.inputs
        )

    def test_corrupt_wheel_or_sources_cannot_be_reused(self):
        self.assertTrue(self.reusable())
        original = self.wheel.read_bytes()
        self.wheel.write_bytes(original + b'corruption')
        self.assertFalse(self.reusable())
        self.wheel.write_bytes(original)
        self.sources.write_bytes(b'not the corresponding sources')
        self.assertFalse(self.reusable())
        self.sources.unlink()
        self.assertFalse(self.reusable())

    def test_source_license_absence_is_rejected_even_with_updated_outer_hash(self):
        del self.members['licenses/openssl/LICENSE']
        self.write_sources()
        self.metadata['source_sha256'] = native.sha256(self.sources)
        self.write_metadata()
        self.assertFalse(self.reusable())

    def test_changed_inner_source_is_rejected_even_with_updated_outer_hash(self):
        self.members['sources/ffmpeg.tar.gz'] = b'other source'
        self.write_sources()
        self.metadata['source_sha256'] = native.sha256(self.sources)
        self.write_metadata()
        self.assertFalse(self.reusable())

    def test_runtime_license_absence_is_rejected_even_with_updated_inventory(self):
        (self.directory / 'licenses' / 'ffmpeg' / 'LICENSE').unlink()
        self.metadata['files'] = native.runtime_inventory(self.directory)
        self.write_metadata()
        self.assertFalse(self.reusable())

    def test_missing_recipe_is_rejected_even_with_updated_outer_hash(self):
        del self.members['recipe/scripts/prepare_pyav.py']
        self.write_sources()
        self.metadata['source_sha256'] = native.sha256(self.sources)
        self.write_metadata()
        self.assertFalse(self.reusable())

    def test_native_inputs_and_toolchain_invalidate_but_ui_edits_do_not(self):
        (self.root / 'main.py').write_text('UI-only change', encoding='utf-8')
        self.assertEqual(self.key, pyav.native_cache_key(self.root, self.target, self.toolchain))
        for name in pyav.INPUTS:
            with self.subTest(input=name):
                path = self.root / name
                original = path.read_bytes()
                path.write_bytes(original + b' changed')
                self.assertNotEqual(
                    self.key, pyav.native_cache_key(self.root, self.target, self.toolchain)
                )
                path.write_bytes(original)
        self.assertNotEqual(
            self.key,
            pyav.native_cache_key(self.root, self.target, {**self.toolchain, 'abi': 'cp315'}),
        )
        self.assertNotEqual(
            self.key, pyav.native_cache_key(self.root, 'darwin-arm64', self.toolchain)
        )

    def test_gpl_configuration_cannot_hide_behind_lgpl_license_string(self):
        for option in ('--enable-gpl', '--enable-nonfree', '--enable-libx264', '--enable-libx265'):
            with self.subTest(option=option):
                info = {
                    **self.metadata['ffmpeg'],
                    'configuration': self.metadata['ffmpeg']['configuration'] + ' ' + option,
                }
                with self.assertRaisesRegex(ValueError, 'LGPL-only'):
                    pyav.validate_ffmpeg(info)
        self.metadata['ffmpeg']['configuration'] += ' --enable-libx264'
        self.write_metadata()
        self.assertFalse(self.reusable())

    def test_unpinned_or_autodetected_ffmpeg_is_rejected(self):
        for change in (
            {'version': '9.0.2'},
            {'configuration': '--disable-gpl --disable-nonfree --enable-version3'},
            {'license': 'GPL version 3 or later'},
        ):
            with self.subTest(change=change), self.assertRaises(ValueError):
                pyav.validate_ffmpeg({**self.metadata['ffmpeg'], **change})

    def test_missing_native_closure_is_rejected_before_import(self):
        with self.assertRaisesRegex(ValueError, 'Incomplete FFmpeg native closure'):
            pyav.inspect_wheel(self.wheel, self.target, self.root / 'extracted')
        del self.metadata['native_libraries']['avfilter']
        self.write_metadata()
        self.assertFalse(self.reusable())

    def test_wheel_cannot_escape_extraction_directory(self):
        with zipfile.ZipFile(self.wheel, 'w') as wheel:
            wheel.writestr('../outside', b'escape')
        with self.assertRaisesRegex(ValueError, 'Unsafe wheel member'):
            pyav.unpack_wheel(self.wheel, self.root / 'extracted')
        self.assertFalse((self.root / 'outside').exists())

    @unittest.skipIf(os.name == 'nt', 'POSIX symlink fixture')
    def test_symlinked_cached_wheel_is_not_reused(self):
        outside = self.root / self.wheel.name
        self.wheel.rename(outside)
        self.wheel.symlink_to(outside)
        self.assertFalse(self.reusable())


class NativeClosureInspectionTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.wheel = self.root / 'av.whl'

    def make_wheel(self, suffix):
        with zipfile.ZipFile(self.wheel, 'w') as wheel:
            for name in pyav.LIBRARIES:
                wheel.writestr('av/.libs/lib' + name + suffix, b'binary fixture')

    def test_macos14_binary_is_rejected_despite_wheel_name(self):
        self.make_wheel('.dylib')

        def inspect(command, env=None):
            if command[1] == '-L':
                return (
                    str(command[-1])
                    + ':\n\t/usr/lib/libSystem.B.dylib (compatibility version 1.0.0)\n'
                )
            return 'Load command 0\n      cmd LC_BUILD_VERSION\n    minos 14.0\nLoad command 1\n'

        with patch.object(pyav, 'output', side_effect=inspect):
            with self.assertRaisesRegex(ValueError, 'exceeds macOS 13'):
                pyav.inspect_wheel(self.wheel, 'darwin-arm64', self.root / 'extracted')

    def test_missing_transitive_elf_dependency_is_rejected(self):
        self.make_wheel('.so.1')
        dynamic = (
            ' 0x1 (NEEDED) Shared library: [libssl-private.so.3]\n'
            ' 0xf (RPATH) Library rpath: [$ORIGIN]\n'
        )
        with patch.object(pyav, 'output', return_value=dynamic):
            with self.assertRaisesRegex(ValueError, 'Unbundled ELF dependency'):
                pyav.inspect_wheel(self.wheel, 'linux-x86_64', self.root / 'extracted')

    def test_newer_glibc_symbols_are_rejected(self):
        self.make_wheel('.so.1')

        def inspect(command, env=None):
            if command[1] == '--dynamic':
                return '0x1 (NEEDED) Shared library: [libc.so.6]\n'
            return 'Name: GLIBC_2.38 Flags: none Version: 3\n'

        with patch.object(pyav, 'output', side_effect=inspect):
            with self.assertRaisesRegex(ValueError, 'exceeds glibc 2.35'):
                pyav.inspect_wheel(self.wheel, 'linux-x86_64', self.root / 'extracted')


if __name__ == '__main__':
    unittest.main()
