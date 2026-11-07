"""The certificate authorities QiTV's own connections trust, chosen before anything connects.

A packaged QiTV's OpenSSL looks for the computer's authorities where the build machine keeps
them: the Mac build's in /usr/local/etc/openssl@3 (Homebrew's). A Mac without Homebrew has no
file there (nor, likely, a Linux distribution other than the build machine's), and then every
connection made with Python's default TLS context trusted no certificate at all:
the phone remote's relay, the channels' logos, AI caption model downloads. requests and the
film relay always used certifi's authorities (the bundle carries them), so the channel lists
loaded and nothing else did.

`use_bundled_authorities()` names certifi's file (SSL_CERT_FILE) when the computer has none
where OpenSSL looks, for this process and the helpers it starts. A computer that has its own,
and a file the user named, stay as they are. On Windows, which keeps its authorities in its own
store, certifi's are added to them. main.py calls this before anything imports aiohttp, which
makes its verifying context when imported. Only the standard library and certifi are used.
"""

from __future__ import annotations

import os
import ssl
from typing import MutableMapping, Optional

ENV_FILE = "SSL_CERT_FILE"


def use_bundled_authorities(
    environ: Optional[MutableMapping[str, str]] = None,
    paths: Optional[ssl.DefaultVerifyPaths] = None,
) -> Optional[str]:
    """certifi's file when it was named (the computer had none of its own), else None."""
    environ = os.environ if environ is None else environ
    if environ.get(ENV_FILE):
        return None  # the user's own choice
    paths = ssl.get_default_verify_paths() if paths is None else paths
    if paths.cafile is not None:
        return None
    try:
        import certifi

        bundled = certifi.where()
    except Exception:  # no certifi: as before
        return None
    environ[ENV_FILE] = bundled
    return bundled
