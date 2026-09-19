import unittest
from unittest.mock import patch

from update_checker import get_download_url_for_platform


class UpdateAssetTests(unittest.TestCase):
    def test_intel_mac_selects_binary_not_arm_build_or_source_sidecar(self):
        assets = [
            {"name": "qitv-macos-arm64.zip", "browser_download_url": "arm"},
            {"name": "qitv-macos-intel.zip.sha256", "browser_download_url": "checksum"},
            {"name": "qitv-macos-intel-sources.tar.gz", "browser_download_url": "sources"},
            {"name": "qitv-macos-intel.zip", "browser_download_url": "intel", "size": 123},
        ]
        with patch("update_checker.platform.system", return_value="Darwin"):
            with patch("update_checker.platform.machine", return_value="x86_64"):
                self.assertEqual(get_download_url_for_platform(assets), ("intel", 123))

    def test_missing_arm_asset_does_not_fall_back_to_intel(self):
        assets = [{"name": "qitv-macos-intel.zip", "browser_download_url": "intel"}]
        with patch("update_checker.platform.system", return_value="Darwin"):
            with patch("update_checker.platform.machine", return_value="arm64"):
                self.assertEqual(get_download_url_for_platform(assets), (None, 0))


if __name__ == "__main__":
    unittest.main()
