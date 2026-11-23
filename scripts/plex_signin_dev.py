"""A Plex token for development: Plex's PIN sign-in, the way QiTV signs in (no QiTV window).

    uv run python scripts/plex_signin_dev.py                       # the server on this computer
    uv run python scripts/plex_signin_dev.py --server http://host:32400

It prints a 4-character code and the page to enter it at (plex.tv/link), then looks every two
seconds until the code is entered there, or it expires (15 minutes: it ends with status 1).
The server the token is for is the one at `--server` (its id, from its /identity, is matched
among the account's servers). The server's own access token, its id and name, its address and
connections, and the client identifier QiTV signed in as are saved to
~/.config/qitv-dev/plex.json, readable by this user only. The token is never printed, nor an
answer's body or an error's text (only its kind).
"""

from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import sys
import time

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from services import plex  # noqa: E402
from services.config_manager import get_app_version  # noqa: E402
from services.lazy_import import lazy_module  # noqa: E402

requests = lazy_module("requests")

SAVED = Path.home() / ".config" / "qitv-dev" / "plex.json"
POLL_S = 2.0


def _client_id() -> str:
    """The client identifier signed in as before (the same device at plex.tv), else a new one."""
    try:
        kept = json.loads(SAVED.read_text(encoding="utf-8"))
        client_id = str(kept.get("client_id") or "")
        if client_id:
            return client_id
    except OSError, ValueError, AttributeError:
        pass
    return plex.new_client_id()


def _machine_id(server: str, client_id: str, version: str) -> str:
    response = requests.get(
        server.rstrip("/") + "/identity",
        headers=plex.client_headers(client_id, version),
        timeout=10,
    )
    response.raise_for_status()
    return str((response.json().get("MediaContainer") or {}).get("machineIdentifier") or "")


def _save(record: dict) -> None:
    SAVED.parent.mkdir(parents=True, exist_ok=True)
    os.chmod(SAVED.parent, 0o700)
    part = SAVED.with_suffix(".json.part")
    fd = os.open(part, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        json.dump(record, f, indent=2)
    os.chmod(part, 0o600)
    os.replace(part, SAVED)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    parser.add_argument("--server", default="http://127.0.0.1:32400")
    args = parser.parse_args()
    version = get_app_version()
    client_id = _client_id()
    try:
        machine = _machine_id(args.server, client_id, version)
    except Exception as error:
        print(f"The server at {args.server} did not answer ({type(error).__name__}).")
        return 1
    if not machine:
        print("The server gave no id.")
        return 1
    account = plex.PlexAccount(client_id, version)
    try:
        pin = account.create_pin()
        print(f"Code: {pin.code}", flush=True)
        print(f"Enter it at https://{plex.LINK_PAGE} (it lasts {pin.expires_in // 60} minutes).")
        print("Waiting…", flush=True)
        ends = time.monotonic() + pin.expires_in
        token = ""
        while not token:
            if time.monotonic() > ends:
                print("The code expired.")
                return 1
            time.sleep(POLL_S)
            try:
                token = account.check_pin(pin.id)
            except plex.SignInNeeded:
                print("The code expired.")
                return 1
            except Exception as error:  # a moment off the network: look again
                print(f"(no answer: {type(error).__name__})", flush=True)
        found = next((s for s in account.servers(token) if s.machine_id == machine), None)
    except Exception as error:
        print(f"The sign-in failed ({type(error).__name__}).")
        return 1
    finally:
        account.close()
    if found is None or not found.access_token:
        print("The account has no access to that server.")
        return 1
    _save(
        {
            "client_id": client_id,
            "server_name": found.name,
            "machine_id": found.machine_id,
            "url": args.server.rstrip("/"),
            "token": found.access_token,
            "connections": [c.uri for c in found.connections],
            "saved": int(time.time()),
        }
    )
    print(f"Signed in. Saved to {SAVED} (this user only).")
    return 0


if __name__ == "__main__":
    sys.exit(main())
