"""Drive the phone remote's real page in headless Chrome against an in-process remote hub.

    uv run --with playwright python scripts/remote_page_check.py --scenario all
    uv run --with playwright python scripts/remote_page_check.py --scenario hold --shots DIR

Nothing shows on screen: Chrome runs headless and the hub listens on 127.0.0.1 only. Each
scenario prints PASS or FAIL; the exit code is 1 if any failed. Needs Google Chrome installed
(or --chrome PATH) and Playwright (`--with playwright`). Scenarios: vectors (the page's Noise
against tests/fixtures/noise_vectors.json), pair-home, hold, hidden-releases; with --relay (a
local `wrangler dev` of relay/, e.g. http://127.0.0.1:8787): pair-relay, hop, hop-back-on-loss,
two-tvs.
"""

from __future__ import annotations

import argparse
import asyncio
import json
from pathlib import Path
import sys
import threading
import time
from typing import Any, Callable, Optional
from urllib.parse import urlsplit, urlunsplit

ROOT = Path(__file__).resolve().parent.parent
sys.path[:0] = [str(ROOT), str(ROOT / "tests")]

from services.remote import net  # noqa: E402
from services.remote.hub import RemoteHub, web_dir  # noqa: E402
from services.remote.protocol import Code, Opened, Paired, Press, Status  # noqa: E402

CHROME = "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome"
VECTORS = ROOT / "tests" / "fixtures" / "noise_vectors.json"


class Recorder:
    def __init__(self) -> None:
        self.events: list[object] = []
        self._lock = threading.Lock()

    def __call__(self, event: object) -> None:
        with self._lock:
            self.events.append(event)

    def mark(self) -> int:
        with self._lock:
            return len(self.events)

    def wait(
        self,
        kind: type,
        timeout: float = 5.0,
        where: Callable[[Any], bool] = lambda e: True,
        after: int = 0,
    ) -> Any:
        end = time.monotonic() + timeout
        while time.monotonic() < end:
            with self._lock:
                found = [e for e in self.events[after:] if isinstance(e, kind) and where(e)]
            if found:
                return found[0]
            time.sleep(0.01)
        raise AssertionError(f"no {kind.__name__} after #{after}")

    def of(self, kind: type, after: int = 0) -> list:
        with self._lock:
            return [e for e in self.events[after:] if isinstance(e, kind)]


class Rig:
    """A hub on 127.0.0.1 and a headless Chrome page (a phone-sized viewport)."""

    def __init__(
        self, playwright: Any, chrome: str, shots: Optional[Path], relay: Optional[str] = None
    ) -> None:
        self.events = Recorder()
        self.relay = relay
        self.hubs: list[RemoteHub] = []
        self.hub = self.add_hub("Living room", self.events)
        self.base = f"http://127.0.0.1:{self.hub.port}/"
        self.browser = playwright.chromium.launch(executable_path=chrome, headless=True)
        self.context = self.browser.new_context(
            viewport={"width": 390, "height": 844}, device_scale_factor=2, has_touch=False
        )
        self.page = self.context.new_page()
        self.errors: list[str] = []
        self.page.on("pageerror", lambda e: self.errors.append(str(e)))
        self.page.on("console", self._console)
        self.shots = shots

    def add_hub(self, name: str, events: Recorder) -> RemoteHub:
        hub = RemoteHub(events, web_dir(), ports=[0], host="127.0.0.1", tv_name=name)
        config = {"version": 1, "enabled": True, "relay": bool(self.relay)}
        if self.relay:
            config["relay_url"] = self.relay
        hub.start(config)
        if not hub.ready.wait(5):
            raise RuntimeError("the hub did not start")
        if self.relay:
            events.wait(Status, timeout=15, where=lambda e: e.relay == "online")
        self.hubs.append(hub)
        return hub

    def close(self) -> None:
        self.browser.close()
        for hub in self.hubs:
            hub.stop()

    def _console(self, message: Any) -> None:
        # A socket to an address that is gone is expected (hop-back-on-loss); other errors fail.
        expected = ("WebSocket connection to", "ERR_CONNECTION_REFUSED")
        if message.type == "error" and not any(text in message.text for text in expected):
            self.errors.append(message.text)

    def pair_url(self, hub: Optional[RemoteHub] = None, events: Optional[Recorder] = None) -> str:
        hub, events = hub or self.hub, events or self.events
        mark = events.mark()
        hub.set_pairing(True)
        url = events.wait(Code, where=lambda e: bool(e.url), after=mark).url
        if self.relay and url.startswith(self.relay):
            return url
        parts = urlsplit(url)  # the QR names the LAN address; this hub listens on loopback
        return urlunsplit((parts.scheme, f"127.0.0.1:{hub.port}", parts.path, "", parts.fragment))

    def pair(self) -> None:
        mark = self.events.mark()
        self.page.goto(self.pair_url())
        self.events.wait(Opened, after=mark)
        self.page.wait_for_selector("[data-screen=remote]:not([hidden])", timeout=5000)

    def shot(self, name: str) -> None:
        if self.shots is not None:
            self.shots.mkdir(parents=True, exist_ok=True)
            self.page.screenshot(path=str(self.shots / f"{name}.png"))


def aim(rig: Rig, selector: str) -> None:
    """Move to where a finger would press `selector`: its icon (a d-pad wedge's box is the
    whole pad, its centre is OK), else its middle."""
    icon = rig.page.locator(f"{selector} svg")
    target = icon if icon.count() else rig.page.locator(selector)
    box = target.first.bounding_box()
    assert box is not None, f"{selector} not on the page"
    rig.page.mouse.move(box["x"] + box["width"] / 2, box["y"] + box["height"] / 2)


def press_hold(rig: Rig, selector: str, ms: int) -> None:
    aim(rig, selector)
    rig.page.mouse.down()
    rig.page.wait_for_timeout(ms)
    rig.page.mouse.up()


# ---- scenarios -------------------------------------------------------------------------------

VECTOR_JS = """
async (vectors) => {
  const n = await import("/noise.js");
  const hex = (s) => Uint8Array.from(s.match(/../g) || [], (b) => parseInt(b, 16));
  const eq = (a, b) => a.length === b.length && a.every((x, i) => x === b[i]);
  const fails = [];
  for (const v of vectors) {
    const pattern = v.protocol_name.includes("KK") ? "KK" : "NKpsk0";
    const key = (name) => (name in v ? n.Keypair.fromPrivate(hex(v[name])) : null);
    const mk = (side, initiator) => new n.HandshakeState(pattern, {
      initiator, prologue: hex(v[side + "_prologue"]), s: key(side + "_static"),
      rs: v[side + "_remote_static"] ? hex(v[side + "_remote_static"]) : null,
      psk: v[side + "_psks"] ? hex(v[side + "_psks"][0]) : null, e: key(side + "_ephemeral"),
    });
    const init = mk("init", true), resp = mk("resp", false);
    let ciphers = null;
    v.messages.forEach((m, i) => {
      const fromInit = i % 2 === 0;
      const payload = hex(m.payload);
      let out;
      if (!ciphers) {
        const [w, r] = fromInit ? [init, resp] : [resp, init];
        out = w.writeMessage(payload);
        if (!eq(r.readMessage(out), payload)) fails.push(`${pattern} ${i}: read back`);
        if (init.finished && resp.finished) {
          const [i1, i2] = init.split(), [r1, r2] = resp.split();
          ciphers = { i: i1, r: r2, iIn: r1, rIn: i2 };
        }
      } else {
        const [w, r] = fromInit ? [ciphers.i, ciphers.iIn] : [ciphers.r, ciphers.rIn];
        out = w.encrypt(new Uint8Array(0), payload);
        if (!eq(r.decrypt(new Uint8Array(0), out), payload)) fails.push(`${pattern} ${i}: open`);
      }
      if (!eq(out, hex(m.ciphertext))) fails.push(`${pattern} message ${i} differs`);
    });
    if (!eq(init.handshakeHash, hex(v.handshake_hash))) fails.push(`${pattern} hash differs`);
  }
  return fails;
}
"""


def scenario_vectors(rig: Rig) -> None:
    rig.page.goto(rig.base)
    vectors = json.loads(VECTORS.read_text())["vectors"]
    fails = rig.page.evaluate(VECTOR_JS, vectors)
    assert fails == [], fails


def scenario_pair_home(rig: Rig) -> None:
    mark = rig.events.mark()
    rig.page.goto(rig.pair_url())
    paired = rig.events.wait(Paired, after=mark)
    rig.events.wait(Opened, after=mark)
    rig.page.wait_for_selector("[data-screen=remote]:not([hidden])", timeout=5000)
    assert rig.page.evaluate("location.hash") == "", "the code stayed in the address"
    assert paired.name.endswith("Chrome"), paired.name
    rig.shot("remote")
    # Reloading reconnects with the stored key, no code needed.
    mark = rig.events.mark()
    rig.page.goto(rig.base)
    rig.events.wait(Opened, after=mark)
    assert not rig.events.of(Paired, after=mark), "a reload paired again"


def scenario_hold(rig: Rig) -> None:
    rig.pair()
    mark = rig.events.mark()
    press_hold(rig, "[data-key=down]", 1200)
    up = rig.events.wait(Press, where=lambda e: not e.down, after=mark)
    downs = [e for e in rig.events.of(Press, after=mark) if e.down]
    assert [e.button for e in downs] == ["down"], downs
    assert up.button == "down" and up.steps in (22, 23), up


def scenario_hidden_releases(rig: Rig) -> None:
    rig.pair()
    aim(rig, "[data-key=down]")
    rig.page.mouse.down()
    mark = rig.events.mark()
    rig.page.wait_for_timeout(100)
    rig.page.evaluate(
        """() => { Object.defineProperty(document, "visibilityState", {value: "hidden",
             configurable: true}); document.dispatchEvent(new Event("visibilitychange")); }"""
    )
    started = time.monotonic()
    up = rig.events.wait(Press, where=lambda e: not e.down, after=mark, timeout=1.0)
    assert time.monotonic() - started < 0.5, "the release took too long"
    assert up.button == "down", up
    rig.page.mouse.up()


def wait_url(rig: Rig, prefix: str, timeout: float = 10.0) -> None:
    # Playwright's sync API only learns of navigations inside its own calls: wait with it.
    try:
        rig.page.wait_for_url(lambda url: url.startswith(prefix), timeout=timeout * 1000)
    except Exception as error:
        raise AssertionError(f"the page is at {rig.page.url}, not {prefix}") from error


def scenario_pair_relay(rig: Rig) -> None:
    mark = rig.events.mark()
    rig.page.goto(rig.pair_url())
    paired = rig.events.wait(Paired, after=mark, timeout=10)
    relay = rig.events.wait(Opened, where=lambda e: e.door == "relay", after=mark)
    assert relay.device_id == paired.device_id
    # Same network (localhost): the page hops straight to QiTV's own page with a ticket.
    home = rig.events.wait(Opened, where=lambda e: e.door == "home", after=mark, timeout=10)
    assert home.device_id == paired.device_id
    wait_url(rig, rig.base)
    assert "#" not in rig.page.url, "the ticket stayed in the address"


def scenario_hop(rig: Rig) -> None:
    scenario_pair_relay(rig)
    mark = rig.events.mark()
    rig.page.goto(f"{rig.relay}/")  # the home-screen icon: the relay address, no code
    rig.events.wait(Opened, where=lambda e: e.door == "relay", after=mark, timeout=10)
    rig.events.wait(Opened, where=lambda e: e.door == "home", after=mark, timeout=10)
    wait_url(rig, rig.base)
    assert not rig.events.of(Paired, after=mark), "opening again paired again"


def scenario_hop_back_on_loss(rig: Rig) -> None:
    scenario_pair_relay(rig)
    core = rig.hub._core
    loop = rig.hub._loop
    assert loop is not None
    mark = rig.events.mark()
    asyncio.run_coroutine_threadsafe(core._runner.cleanup(), loop).result(5)  # the home door goes
    rig.events.wait(Opened, where=lambda e: e.door == "relay", after=mark, timeout=15)
    wait_url(rig, rig.relay or "", timeout=15)
    rig.page.wait_for_timeout(1000)
    assert rig.page.url.startswith(rig.relay or ""), "it hopped back to the dead address"


def scenario_two_tvs(rig: Rig) -> None:
    scenario_pair_relay(rig)
    other_events = Recorder()
    other = rig.add_hub("Bedroom", other_events)
    mark = other_events.mark()
    rig.page.goto(rig.pair_url(other, other_events))
    other_events.wait(Paired, after=mark, timeout=10)
    other_events.wait(Opened, where=lambda e: e.door == "home", after=mark, timeout=10)
    wait_url(rig, f"http://127.0.0.1:{other.port}/")  # its hop home is over before we leave
    rig.page.goto(f"{rig.relay}/#b.")  # the relay page without hopping; chooses below
    rig.page.wait_for_selector("#tvButton.chooser", timeout=10000)
    rig.page.click("#tvButton")
    rig.page.wait_for_selector("#menuSheet:not([hidden])")
    names = rig.page.locator("#menuList button").all_inner_texts()
    assert "Living room" in names and "Bedroom" in names, names
    mark = rig.events.mark()
    rig.page.locator("#menuList button", has_text="Living room").click()
    rig.events.wait(Opened, after=mark, timeout=10)


SCENARIOS: dict[str, Callable[[Rig], None]] = {
    "vectors": scenario_vectors,
    "pair-home": scenario_pair_home,
    "hold": scenario_hold,
    "hidden-releases": scenario_hidden_releases,
}
RELAY_SCENARIOS: dict[str, Callable[[Rig], None]] = {
    "pair-relay": scenario_pair_relay,
    "hop": scenario_hop,
    "hop-back-on-loss": scenario_hop_back_on_loss,
    "two-tvs": scenario_two_tvs,
}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    parser.add_argument("--scenario", default="all", choices=["all", *SCENARIOS, *RELAY_SCENARIOS])
    parser.add_argument("--relay", default=None, help="a local relay, e.g. http://127.0.0.1:8787")
    parser.add_argument("--chrome", default=CHROME)
    parser.add_argument("--shots", type=Path, default=None)
    args = parser.parse_args()
    from playwright.sync_api import sync_playwright

    every = {**SCENARIOS, **(RELAY_SCENARIOS if args.relay else {})}
    names = list(every) if args.scenario == "all" else [args.scenario]
    if args.relay:
        net.home_ipv4 = lambda: "127.0.0.1"  # the hubs listen on loopback: hop there
    failed = 0
    with sync_playwright() as playwright:
        for name in names:
            rig = Rig(playwright, args.chrome, args.shots, args.relay)
            try:
                every[name](rig)
                if rig.errors:
                    raise AssertionError(f"page errors: {rig.errors}")
                print(f"PASS {name}")
            except Exception as error:  # report every scenario, then fail at the end
                failed += 1
                print(f"FAIL {name}: {error}")
            finally:
                rig.close()
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
