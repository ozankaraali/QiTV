import ssl
import unittest

import certifi

from services.authorities import use_bundled_authorities

BUILD_MACHINES = "/usr/local/etc/openssl@3/cert.pem"  # where the Mac build's OpenSSL looks


def paths(cafile):
    """ssl.get_default_verify_paths() as a computer gives it: cafile None when it has no file
    where OpenSSL looks."""
    return ssl.DefaultVerifyPaths(
        cafile, None, "SSL_CERT_FILE", BUILD_MACHINES, "SSL_CERT_DIR", "/usr/local/etc/openssl@3"
    )


class BundledAuthoritiesTests(unittest.TestCase):
    def test_a_computer_without_the_build_machines_file_trusts_the_bundled_authorities(self):
        # A Mac without Homebrew: the relay, logos and model downloads trusted no certificate.
        environ: dict[str, str] = {}
        chosen = use_bundled_authorities(environ, paths(None))
        self.assertEqual(chosen, certifi.where())
        self.assertEqual(environ, {"SSL_CERT_FILE": certifi.where()})

    def test_the_computers_own_authorities_stay(self):
        environ: dict[str, str] = {}
        self.assertIsNone(use_bundled_authorities(environ, paths(BUILD_MACHINES)))
        self.assertEqual(environ, {})

    def test_a_file_the_user_named_stays(self):
        environ = {"SSL_CERT_FILE": "/their/own.pem"}
        self.assertIsNone(use_bundled_authorities(environ, paths(None)))
        self.assertEqual(environ, {"SSL_CERT_FILE": "/their/own.pem"})


if __name__ == "__main__":
    unittest.main()
