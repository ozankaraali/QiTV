"""Drive the phone remote's real page in headless Chrome against an in-process remote hub.

    uv run --with playwright python scripts/remote_page_check.py --scenario all
    uv run --with playwright python scripts/remote_page_check.py --scenario hold --shots DIR

Nothing shows on screen: Chrome runs headless and the hub listens on 127.0.0.1 only. Each
scenario prints PASS or FAIL; the exit code is 1 if any failed. Needs Google Chrome installed
(or --chrome PATH) and Playwright (`--with playwright`). Scenarios: vectors (the page's Noise
against tests/fixtures/noise_vectors.json), pair-home, hold, hidden-releases and the second
screen's; with --relay (a local `wrangler dev` of the QiTV relay, e.g. http://127.0.0.1:8787, or
the deployed one): pair-relay, hop, hop-once, hop-memory, hop-back-on-loss, relay-banner, two-tvs.
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
from services.remote.protocol import (  # noqa: E402
    Code,
    Get,
    Opened,
    OpenItem,
    Paired,
    Press,
    Query,
    Status,
)

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
    from services import i18n

    i18n.set_language("tr")  # QiTV in Turkish: the phone follows
    mark = rig.events.mark()
    rig.page.goto(rig.pair_url())
    paired = rig.events.wait(Paired, after=mark)
    rig.events.wait(Opened, after=mark)
    rig.page.wait_for_selector("[data-screen=remote]:not([hidden])", timeout=5000)
    assert rig.page.evaluate("location.hash") == "", "the code stayed in the address"
    assert paired.name.endswith("Chrome"), paired.name
    lang = rig.page.evaluate("document.documentElement.lang")
    assert lang == "tr", f"the page's language is {lang!r}, QiTV's is tr"
    guide = rig.page.inner_text("[data-tab=guide]")
    assert guide == i18n.translate("Guide") != "Guide", guide  # QiTV's words arrived
    i18n.set_language(None)
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
# ---- the second screen (the check answers as QiTV would, with invented content) ----------

NOW = time.time()


def programme(title: str, start_min: float, stop_min: float) -> dict:
    return {"title": title, "start": NOW + start_min * 60, "stop": NOW + stop_min * 60}


STATE = {
    "t": "state",
    "context": "player",
    "section": "live",
    "now": {
        "kind": "live",
        "num": "1",
        "name": "Harbourline News",
        "now": programme("Evening bulletin", -20, 25),
        "next": programme("Weather on the hour", 25, 30),
        "behind_s": 0,
    },
    "volume": 40,
    "muted": False,
    "recording": False,
    "pip": False,
    "multiview": False,
}
GUIDE = {
    "t": "guide",
    "list": "all",
    "page": 0,
    "pages": 1,
    "rows": [
        {
            "id": "h1",
            "num": "1",
            "name": "Harbourline News",
            "playing": True,
            "now": programme("Evening bulletin", -20, 25),
            "next": programme("Weather", 25, 30),
        },
        {
            "id": "h2",
            "num": "2",
            "name": "Fernvale Sports",
            "playing": False,
            "now": programme("Second half", -40, 10),
            "next": None,
        },
        {
            "id": "h3",
            "num": "3",
            "name": "Quillmoor Docs",
            "playing": False,
            "now": None,
            "next": None,
        },
    ],
}
RESULTS = {
    "t": "results",
    "q": "harb",
    "groups": [
        {"kind": "live", "items": [{"id": "r1", "title": "Harbourline News", "sub": ""}]},
        {"kind": "movies", "items": [{"id": "r2", "title": "Harbour Lights", "sub": ""}]},
    ],
}
LIBRARY = {
    "t": "library",
    "continue": [{"id": "c1", "title": "Harbour Lights", "sub": "1998", "progress": 0.4}],
    "mylist": [{"id": "m1", "title": "The Harbourmaster", "sub": "2011"}],
    "mylist_section": "movies",
}


def paired_sid(rig: Rig) -> str:
    mark = rig.events.mark()
    rig.pair()
    return rig.events.wait(Opened, after=mark).sid


def scenario_now_playing(rig: Rig) -> None:
    sid = paired_sid(rig)
    rig.hub.send(sid, STATE)
    rig.page.locator("#nowName", has_text="Harbourline News").wait_for()
    assert "Evening bulletin" in rig.page.inner_text("#nowShow")
    width = rig.page.evaluate("parseFloat(document.getElementById('nowProg').style.width)")
    assert 30 < width < 60, width
    assert rig.page.locator(".seg [data-section=live].on").count() == 1
    rig.shot("now-playing")
    mark = rig.events.mark()
    rig.page.click("#now")
    rig.events.wait(Press, where=lambda e: e.button == "info" and not e.down, after=mark)


def scenario_guide_tune(rig: Rig) -> None:
    sid = paired_sid(rig)
    mark = rig.events.mark()
    rig.page.click("[data-tab=guide]")
    ask = rig.events.wait(Get, where=lambda e: e.what == "guide", after=mark)
    assert (ask.list, ask.page) == ("all", 0), ask
    rig.hub.send(sid, GUIDE)
    rig.page.wait_for_selector("#guideRows .row >> nth=2")
    assert rig.page.locator("#guideRows .row.playing").count() == 1
    rig.shot("guide")
    mark = rig.events.mark()
    rig.page.locator("#guideRows .row", has_text="Fernvale Sports").click()
    assert rig.events.wait(OpenItem, after=mark).handle == "h2"
    mark = rig.events.mark()
    rig.page.click("#guideChips [data-list=favorites]")
    assert rig.events.wait(Get, after=mark).list == "favorites"


def scenario_search_mirror(rig: Rig) -> None:
    sid = paired_sid(rig)
    mark = rig.events.mark()
    rig.page.click("#searchButton")
    rig.events.wait(Press, where=lambda e: e.button == "search" and not e.down, after=mark)
    rig.page.wait_for_selector("[data-screen=search]:not([hidden])")
    assert rig.page.evaluate("document.activeElement.id") == "searchInput"
    mark = rig.events.mark()
    rig.page.keyboard.type("harb", delay=30)
    rig.events.wait(Query, where=lambda e: e.text == "harb", after=mark)
    rig.hub.send(sid, RESULTS)
    rig.page.wait_for_selector("#searchRows .row >> nth=1")
    groups = rig.page.locator("#searchRows .group").all_inner_texts()
    assert len(groups) == 2, groups
    rig.shot("search")
    mark = rig.events.mark()
    rig.page.locator("#searchRows .row", has_text="Harbour Lights").click()
    assert rig.events.wait(OpenItem, after=mark).handle == "r2"
    mark = rig.events.mark()
    rig.page.focus("#searchInput")
    rig.page.keyboard.press("Enter")
    rig.events.wait(Press, where=lambda e: e.button == "ok" and not e.down, after=mark)


def scenario_library_open(rig: Rig) -> None:
    sid = paired_sid(rig)
    mark = rig.events.mark()
    rig.page.click("[data-tab=library]")
    rig.events.wait(Get, where=lambda e: e.what == "library", after=mark)
    rig.hub.send(sid, LIBRARY)
    rig.page.wait_for_selector("#libraryRows .row >> nth=1")
    rig.shot("library")
    mark = rig.events.mark()
    rig.page.locator("#libraryRows .row", has_text="The Harbourmaster").click()
    assert rig.events.wait(OpenItem, after=mark).handle == "m1"


def scenario_more_numpad(rig: Rig) -> None:
    paired_sid(rig)
    rig.page.click("#moreButton")
    rig.page.wait_for_selector("#moreSheet:not([hidden])")
    rig.shot("more")
    mark = rig.events.mark()
    rig.page.click("#moreSheet [data-key=d7]")
    rig.events.wait(Press, where=lambda e: e.button == "d7" and not e.down, after=mark)
    rig.page.click("#moreSheet [data-close]")
    rig.page.wait_for_selector("#moreSheet", state="hidden")


def drag(rig: Rig, dx: float, dy: float, hold_ms: int) -> None:
    box = rig.page.locator("#touchpad").bounding_box()
    assert box is not None
    x, y = box["x"] + box["width"] / 2, box["y"] + box["height"] / 2
    rig.page.mouse.move(x, y)
    rig.page.mouse.down()
    rig.page.mouse.move(x + dx, y + dy, steps=4)
    rig.page.wait_for_timeout(hold_ms)
    rig.page.mouse.up()


def scenario_touchpad(rig: Rig) -> None:
    paired_sid(rig)
    rig.page.click("#menuButton")
    rig.page.locator("#menuList button", has_text="touchpad").click()
    rig.page.wait_for_selector("#touchpad:not([hidden])")
    rig.shot("touchpad")
    mark = rig.events.mark()
    drag(rig, 0, 60, 0)  # a flick down: one step
    up = rig.events.wait(Press, where=lambda e: not e.down, after=mark)
    assert (up.button, up.steps) == ("down", 1), up
    mark = rig.events.mark()
    drag(rig, 60, 0, 1200)  # a drag right that stays down: a held arrow from 250 ms
    up = rig.events.wait(Press, where=lambda e: not e.down, after=mark)
    assert up.button == "right" and up.steps >= 14, up  # held ~950 ms: steps_for(950) = 15
    mark = rig.events.mark()
    rig.page.click("#touchpad")  # a tap: OK
    rig.events.wait(Press, where=lambda e: e.button == "ok" and not e.down, after=mark)
    rig.page.reload()  # remembered on this phone
    rig.page.wait_for_selector("#touchpad:not([hidden])")


def scenario_forget_clean(rig: Rig) -> None:
    paired_sid(rig)
    rig.page.click("#menuButton")
    rig.page.locator("#menuList button", has_text="Forget this TV").click()
    rig.page.wait_for_selector("[data-screen=scan]:not([hidden])")
    rig.page.wait_for_timeout(1500)  # past any retry
    assert rig.page.locator("#banner").is_hidden(), rig.page.inner_text("#banner")


def scenario_hello_keeps_now(rig: Rig) -> None:
    sid = paired_sid(rig)
    rig.hub.send(sid, STATE)
    rig.page.locator("#nowName", has_text="Harbourline News").wait_for()
    rig.hub.send(
        sid, {"t": "hello", "proto": 1, "tv": "Living room", "door": "home", "strings": {}}
    )
    rig.page.wait_for_timeout(300)
    assert rig.page.inner_text("#nowName") == "Harbourline News", rig.page.inner_text("#nowName")


SCENARIOS.update(
    {
        "forget-clean": scenario_forget_clean,
        "hello-keeps-now": scenario_hello_keeps_now,
        "now-playing": scenario_now_playing,
        "guide-tune": scenario_guide_tune,
        "search-mirror": scenario_search_mirror,
        "library-open": scenario_library_open,
        "more-numpad": scenario_more_numpad,
        "touchpad": scenario_touchpad,
    }
)


def scenario_hop_once(rig: Rig) -> None:
    scenario_pair_relay(rig)
    for _ in range(2):  # the home-screen icon, opened twice more at home
        mark = rig.events.mark()
        rig.page.goto(f"{rig.relay}/")
        rig.events.wait(Opened, where=lambda e: e.door == "home", after=mark, timeout=10)
        wait_url(rig, rig.base)
    keys = rig.hub._core.store.devices[0].keys
    assert len(keys) == 2, [k.door for k in keys]  # its relay key and one home key, not more


def scenario_hop_memory(rig: Rig) -> None:
    net.home_ipv4 = lambda: "127.0.0.2"  # an address this phone can't reach (guest Wi-Fi)
    try:
        mark = rig.events.mark()
        rig.page.goto(rig.pair_url())
        rig.events.wait(Paired, after=mark, timeout=10)
        rig.page.wait_for_timeout(2500)  # it hopped and found nothing
        mark = rig.events.mark()
        rig.page.goto(f"{rig.relay}/")  # opened again: no second hop into the void
        rig.events.wait(Opened, where=lambda e: e.door == "relay", after=mark, timeout=10)
        rig.page.wait_for_timeout(2000)
        assert rig.page.url.startswith(rig.relay or ""), rig.page.url
        rig.page.click("#menuButton")
        assert rig.page.locator("#menuList button", has_text="Connect directly").count() == 1
    finally:
        net.home_ipv4 = lambda: "127.0.0.1"


def scenario_relay_banner(rig: Rig) -> None:
    scenario_pair_relay(rig)
    mark = rig.events.mark()
    rig.page.goto(f"{rig.relay}/#b.")
    rig.events.wait(Opened, where=lambda e: e.door == "relay", after=mark, timeout=10)
    rig.hub.stop()  # QiTV quits: the relay tells the phone it's offline
    rig.page.locator("#banner", has_text="reachable").wait_for(timeout=8000)
    rig.page.wait_for_timeout(1500)
    assert "reachable" in rig.page.inner_text("#banner"), rig.page.inner_text("#banner")


RELAY_SCENARIOS: dict[str, Callable[[Rig], None]] = {
    "hop-once": scenario_hop_once,
    "hop-memory": scenario_hop_memory,
    "relay-banner": scenario_relay_banner,
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
            relay = args.relay if name in RELAY_SCENARIOS else None  # home ones: no relay at all
            rig = Rig(playwright, args.chrome, args.shots, relay)
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
