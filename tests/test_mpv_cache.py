import json
import os
from pathlib import Path
import tempfile
import unittest

from scripts.prepare_mpv import (
    cached_runtime_valid,
    host_target,
    native_cache_key,
    runtime_inventory,
    sha256,
)


class NativeCacheTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        for name in (
            'scripts/prepare_mpv.py',
            'native/mpv-manifest.json',
            'native/REDISTRIBUTION.txt',
            'native/patches/mpv/fix.patch',
            'assets/mpv/uosc/main.lua',
        ):
            path = self.root / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(name, encoding='utf-8')
        self.target = host_target()
        self.toolchain = {'compiler': 'version-one', 'sdk': 'sdk-one'}
        self.key = native_cache_key(self.root, self.target, self.toolchain)
        self.runtime = self.root / 'native' / 'mpv'
        self.sources = self.root / 'native' / f'mpv-sources-{self.target}.tar.gz'
        self.sources.write_bytes(b'corresponding native sources')
        suffix = '.exe' if self.target.startswith('windows') else ''
        self.player = self.runtime / 'bin' / f'mpv{suffix}'
        self.helper = self.runtime / 'bin' / f'ziggy{suffix}'
        self.license = self.runtime / 'licenses' / 'COPYING'
        for path in (self.player, self.helper, self.license):
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(path.name.encode())
        self.player.chmod(0o755)
        self.helper.chmod(0o755)
        self.metadata = {
            'schema': 1,
            'target': self.target,
            'cache_key': self.key,
            'redistributable': True,
            'executable': self.player.relative_to(self.runtime).as_posix(),
            'uosc': {'executable': self.helper.relative_to(self.runtime).as_posix()},
            'source_archive': self.sources.name,
            'source_sha256': sha256(self.sources),
            'files': runtime_inventory(self.runtime),
        }
        self.manifest = self.runtime / 'bundle.json'
        self.manifest.write_text(json.dumps(self.metadata), encoding='utf-8')

    def reusable(self):
        return cached_runtime_valid(self.runtime, self.sources, self.target, self.key)

    def test_native_changes_invalidate_but_application_changes_do_not(self):
        # Neither Python/UI edits nor generated native outputs should force a native rebuild.
        (self.root / 'main.py').write_text('changed application', encoding='utf-8')
        (self.root / 'pyproject.toml').write_text('changed packaging', encoding='utf-8')
        self.assertEqual(self.key, native_cache_key(self.root, self.target, self.toolchain))
        self.assertNotEqual(
            self.key, native_cache_key(self.root, self.target + '-other', self.toolchain)
        )
        self.assertNotEqual(
            self.key,
            native_cache_key(self.root, self.target, {'compiler': 'version-two', 'sdk': 'sdk-one'}),
        )
        patch = self.root / 'native' / 'patches' / 'mpv' / 'fix.patch'
        patch.write_text('changed native patch', encoding='utf-8')
        self.assertNotEqual(self.key, native_cache_key(self.root, self.target, self.toolchain))

    def test_renamed_native_input_invalidates_even_with_identical_contents(self):
        source = self.root / 'assets' / 'mpv' / 'uosc' / 'main.lua'
        source.rename(source.with_name('renamed.lua'))
        self.assertNotEqual(self.key, native_cache_key(self.root, self.target, self.toolchain))

    def test_runtime_is_reused_only_with_its_intact_source_archive(self):
        self.assertTrue(self.reusable())
        original = self.sources.read_bytes()
        self.sources.write_bytes(b'other sources')
        self.assertFalse(self.reusable())
        self.sources.write_bytes(original)
        self.assertTrue(self.reusable())
        self.sources.unlink()
        self.assertFalse(self.reusable())

    def test_modified_binaries_or_missing_licenses_are_not_reused(self):
        original = self.player.read_bytes()
        self.player.write_bytes(b'changed executable')
        self.assertFalse(self.reusable())
        self.player.write_bytes(original)
        self.assertTrue(self.reusable())
        self.license.unlink()
        self.assertFalse(self.reusable())

    def test_wrong_identity_or_partial_metadata_is_a_cache_miss(self):
        self.assertFalse(cached_runtime_valid(self.runtime, self.sources, self.target, 'old-key'))
        self.metadata['target'] = 'another-platform'
        self.manifest.write_text(json.dumps(self.metadata), encoding='utf-8')
        self.assertFalse(self.reusable())
        self.manifest.write_text('{', encoding='utf-8')
        self.assertFalse(self.reusable())

    @unittest.skipIf(os.name == 'nt', 'POSIX executable permissions and symlinks')
    def test_nonexecutable_or_symlinked_binaries_are_not_reused(self):
        self.player.chmod(0o644)
        self.assertFalse(self.reusable())
        self.player.chmod(0o755)
        self.assertTrue(self.reusable())
        outside = self.root / 'external-player'
        self.player.rename(outside)
        self.player.symlink_to(outside)
        self.assertFalse(self.reusable())


if __name__ == '__main__':
    unittest.main()
