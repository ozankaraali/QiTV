"""Drive the phone remote's real page in headless Chrome against an in-process remote hub.

    uv run --with playwright python scripts/remote_page_check.py --scenario all
    uv run --with playwright python scripts/remote_page_check.py --scenario hold --shots DIR

Nothing shows on screen: Chrome runs headless and the hub listens on 127.0.0.1 only. Each
scenario prints PASS or FAIL; the exit code is 1 if any failed. Needs Google Chrome installed
(or --chrome PATH) and Playwright (`--with playwright`). Scenarios: vectors (the page's Noise
against tests/fixtures/noise_vectors.json), pair-home, scan-again, hold, hidden-releases and the
second screen's; with --relay (a local `wrangler dev` of the QiTV relay, e.g. http://127.0.0.1:8787,
or the deployed one): pair-relay, hop, hop-once, hop-memory, hop-back-on-loss, relay-banner,
two-tvs, scan-again-relay, and page-versions when QITV_KEPT_PAGE names a page the relay keeps
beside the newest (a copy of its public/p/<id>/ at public/p/<another id>/, that id added to
public/pages.json's "kept").
"""

from __future__ import annotations

import argparse
import asyncio
from datetime import datetime
import json
import os
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
    Answered,
    CastTo,
    Code,
    Get,
    Opened,
    OpenItem,
    Paired,
    Position,
    Press,
    Query,
    RecAct,
    Schedule,
    Status,
    Timer,
    ToTv,
    WatchHere,
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


def scenario_scan_again(rig: Rig) -> None:
    """A code scanned with the camera while the page is open only changes the open page's
    address after the #, without loading it again: the page pairs from it all the same."""
    rig.pair()
    mark = rig.events.mark()
    rig.page.goto(rig.pair_url())  # the same page, another code: the browser stays on the page
    rig.events.wait(Paired, after=mark, timeout=10)
    rig.events.wait(Opened, after=mark)
    rig.page.wait_for_selector("[data-screen=remote]:not([hidden])", timeout=5000)
    assert rig.page.evaluate("location.hash") == "", "the code stayed in the address"


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


def scenario_page_versions(rig: Rig) -> None:
    """A QiTV carrying an older page the relay keeps: the phone moves to that page, and opens there
    next time; a page the relay doesn't keep leaves the phone where it is. QITV_KEPT_PAGE names a
    kept page beside the newest (this script's docstring says how to set one up)."""
    older = os.environ.get("QITV_KEPT_PAGE", "")
    assert rig.relay and older, "needs --relay and QITV_KEPT_PAGE"
    core = rig.hub._core
    core._page_id = older
    mark = rig.events.mark()
    rig.page.goto(rig.pair_url())  # pairs on the newest page, which moves to the older one
    rig.events.wait(Paired, after=mark, timeout=10)
    rig.events.wait(Opened, where=lambda e: e.door == "home", after=mark, timeout=15)
    tv = rig.page.evaluate("JSON.parse(localStorage.getItem('qitv-remote')).last")
    mark = rig.events.mark()
    rig.page.goto(f"{rig.relay}/#b.{tv}")  # the relay again, staying there: the older page at once
    wait_url(rig, f"{rig.relay}/p/{older}/")
    rig.events.wait(Opened, where=lambda e: e.door == "relay", after=mark, timeout=10)
    core._page_id = "f" * 16  # a page the relay doesn't keep (a development build's)
    mark = rig.events.mark()
    rig.page.goto(f"{rig.relay}/#b.{tv}")  # to the older page it remembers; there it stays
    rig.events.wait(Opened, where=lambda e: e.door == "relay", after=mark, timeout=10)
    rig.page.wait_for_timeout(1500)
    url = rig.page.evaluate("location.href")  # (Playwright's page.url lags a replace)
    assert url.startswith(f"{rig.relay}/p/{older}/"), url
    rig.page.wait_for_selector("[data-screen=remote]:not([hidden])")


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


def scenario_scan_again_relay(rig: Rig) -> None:
    """scan-again on the relay's page: the code reaches the page open at the relay's address."""
    scenario_pair_relay(rig)
    mark = rig.events.mark()
    rig.page.goto(f"{rig.relay}/#b.")  # the relay page without hopping
    rig.events.wait(Opened, where=lambda e: e.door == "relay", after=mark, timeout=10)
    mark = rig.events.mark()
    rig.page.goto(rig.pair_url())  # the same page, another code
    rig.events.wait(Paired, after=mark, timeout=10)


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
    "scan-again": scenario_scan_again,
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
    "list": "g2",
    "title": "All",
    "count": 3,
    "find": "",
    "page": 0,
    "pages": 1,
    "loading": False,
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
CHANNELS = {  # the Remote tab's list: the Guide's rows without programmes
    **GUIDE,
    "t": "channels",
    "rows": [{k: v for k, v in r.items() if k not in ("now", "next")} for r in GUIDE["rows"]],
}
RESULTS = {
    "t": "results",
    "q": "harb",
    "groups": [
        {"kind": "live", "items": [{"id": "r1", "title": "Harbourline News", "sub": ""}]},
        {"kind": "movies", "items": [{"id": "r2", "title": "Harbour Lights", "sub": ""}]},
    ],
}
GROUPS = {
    "t": "groups",
    "find": "",
    "page": 0,
    "pages": 1,
    "loading": False,
    "rows": [
        {"id": "g1", "title": "★ Favorites", "count": 1},
        {"title": "Provider A", "header": True},
        {"id": "g2", "title": "All", "count": 3},
        {"id": "g3", "title": "News", "count": 1},
    ],
}
GENRES = {
    "t": "genres",
    "section": "movies",
    "find": "",
    "page": 0,
    "pages": 1,
    "loading": False,
    "rows": [
        {"id": "g1", "title": "Continue", "count": 2},
        {"title": "Provider A", "header": True},
        {"id": "g2", "title": "Drama", "count": 60},
    ],
}


def titles_page(page: int, count: int, find: str = "") -> dict:
    items = [
        {"id": f"d{page}-{i}", "title": f"Drama {page}-{i}", "sub": "2001"} for i in range(count)
    ]
    if page == 0:
        items[0] = {"id": "c1", "title": "Harbour Lights", "sub": "1998", "progress": 0.4}
    head = {"t": "library", "section": "movies", "genre": "g2", "title": "Drama", "count": 60}
    return {**head, "find": find, "page": page, "pages": 2, "items": items, "loading": False}


def channels_page(page: int, pages: int, playing: int = -1) -> dict:
    rows = [
        {
            "id": f"c{page}-{i}",
            "num": str(page * 50 + i + 1),
            "name": f"Channel {page * 50 + i + 1}",
            "playing": i == playing,
            "now": None,
            "next": None,
        }
        for i in range(50)
    ]
    head = {"t": "guide", "list": "g2", "title": "All", "count": pages * 50, "find": ""}
    return {**head, "page": page, "pages": pages, "rows": rows, "loading": False}


def labelled(rig: Rig, field: str) -> None:
    """A find box is named by a label of its own, just before it, saying what its placeholder
    says: iOS AutoFill read the switch beside the Remote tab's ("Phone") as its label and
    offered phone numbers."""
    found = rig.page.evaluate(
        """(id) => { const input = document.getElementById(id);
          const label = input.previousElementSibling;
          return [label && label.tagName === 'LABEL' && label.htmlFor === id,
                  label ? label.textContent : '', input.placeholder]; }""",
        field,
    )
    named, text, placeholder = found
    assert named and text and text == placeholder, (field, found)


def answer(rig: Rig, sid: str, ask: Any, message: dict) -> None:
    """QiTV's answer to `ask`, carrying its number back as QiTV's do (an unnumbered message
    reads as QiTV's own resend, which the page takes only for what it shows)."""
    rig.hub.send(sid, {**message, "n": ask.n})


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


def scenario_back_to(rig: Rig) -> None:
    """2.6: while the TV offers where a channel come back to was left, the phone shows Resume
    with when; a tap asks the TV to go back; it goes once the TV no longer offers it."""
    sid = paired_sid(rig)
    rig.hub.send(sid, {**STATE, "back": "20:10"})
    rig.page.locator("#backTo", has_text="20:10").wait_for()
    assert "Resume" in rig.page.inner_text("#backTo"), rig.page.inner_text("#backTo")
    rig.shot("back-to")
    mark = rig.events.mark()
    rig.page.click("#backTo")
    rig.events.wait(Press, where=lambda e: e.button == "back_to" and not e.down, after=mark)
    rig.hub.send(sid, STATE)
    rig.page.locator("#backTo").wait_for(state="hidden")


def scenario_ask(rig: Rig) -> None:
    """2.6: QiTV's question on the phone that asked (stop a recording for its Watch?): a
    sheet over everything with the safe answer first; each answer carries the question's
    number back; Keep recording says nothing opened; a question gone on the TV closes."""
    sid = paired_sid(rig)
    rig.hub.send(sid, STATE)
    rig.page.locator("#nowName", has_text="Harbourline News").wait_for()
    question = "No stream is free: Harbourline News is recording. Stop the recording?"
    ask = {"t": "ask", "id": 7, "text": question, "yes": "Stop recording", "no": "Keep recording"}
    rig.hub.send(sid, ask)
    rig.page.locator("#askSheet").wait_for()
    assert rig.page.inner_text("#askText") == question, rig.page.inner_text("#askText")
    rig.shot("ask")
    mark = rig.events.mark()
    rig.page.click("#askYes")
    rig.events.wait(Answered, where=lambda e: e.n == 7 and e.yes, after=mark)
    rig.page.locator("#askSheet").wait_for(state="hidden")
    rig.hub.send(sid, {**ask, "id": 8})
    rig.page.locator("#askSheet").wait_for()
    mark = rig.events.mark()
    rig.page.click("#veil", position={"x": 20, "y": 20})  # tapped away: keep recording
    rig.events.wait(Answered, where=lambda e: e.n == 8 and not e.yes, after=mark)
    rig.hub.send(sid, {**ask, "id": 9})
    rig.page.locator("#askSheet").wait_for()
    rig.hub.send(sid, {"t": "unask", "id": 9})  # settled on the TV
    rig.page.locator("#askSheet").wait_for(state="hidden")


def scenario_rec_line(rig: Rig) -> None:
    """2.6: what records in the background shows under the now-playing card with its time;
    its menu goes back to the channel or stops the recording."""
    sid = paired_sid(rig)
    since = int(time.time()) - 3723
    rig.hub.send(sid, {**STATE, "rec": [{"id": "b1", "name": "Quayside Sport", "since": since}]})
    rig.page.locator("#recLine", has_text="Quayside Sport").wait_for()
    shown = rig.page.inner_text("#recTime")
    assert shown.startswith("1:02:"), shown
    rig.shot("rec-line")
    mark = rig.events.mark()
    rig.page.click("#recLine")
    rig.page.locator("#menuList button", has_text="Quayside Sport").click()
    rig.events.wait(RecAct, where=lambda e: e.slot == "b1" and e.act == "back", after=mark)
    rig.page.click("#recLine")
    rig.page.locator("#menuList button.danger").click()
    rig.events.wait(RecAct, where=lambda e: e.slot == "b1" and e.act == "stop", after=mark)
    rig.hub.send(sid, STATE)
    rig.page.locator("#recLine").wait_for(state="hidden")


def scenario_guide_tune(rig: Rig) -> None:
    sid = paired_sid(rig)
    mark = rig.events.mark()
    rig.page.click("[data-tab=guide]")  # where the TV's channel list is
    ask = rig.events.wait(Get, where=lambda e: e.what == "guide", after=mark)
    assert (ask.list, ask.page, ask.find) == ("", 0, ""), ask
    answer(rig, sid, ask, GUIDE)
    rig.page.wait_for_selector("#guideRows .row >> nth=2")
    assert rig.page.locator("#guideRows .row.playing").count() == 1
    assert rig.page.inner_text("#guideTitle").startswith("All"), rig.page.inner_text("#guideTitle")
    assert rig.page.locator("#guideUp").is_visible()
    rig.shot("guide")
    mark = rig.events.mark()  # ▶ tunes at once
    rig.page.locator("#guideRows .row", has_text="Fernvale Sports").locator(".side").click()
    assert rig.events.wait(OpenItem, after=mark).handle == "h2"
    mark = rig.events.mark()  # the row opens the channel's card, tuning nothing
    rig.page.locator("#guideRows .row", has_text="Fernvale Sports").locator(".row-tap").click()
    ask = rig.events.wait(Get, where=lambda e: e.what == "timetable", after=mark)
    assert ask.handle == "h2", ask
    assert not rig.events.of(OpenItem, after=mark)
    rig.page.click("#timetableBack")
    mark = rig.events.mark()  # up to the groups, into another, then find in it
    rig.page.click("#guideUp")
    ask = rig.events.wait(Get, where=lambda e: e.what == "groups", after=mark)
    assert (ask.page, ask.find) == (0, ""), ask
    answer(rig, sid, ask, GROUPS)
    rig.page.wait_for_selector("#guideRows .row.branch >> nth=2")
    labelled(rig, "guideFind")  # the groups' placeholder, the label with it
    assert rig.page.locator("#guideRows .group", has_text="Provider A").count() == 1
    assert rig.page.locator("#guideUp").is_hidden()
    rig.shot("groups")
    mark = rig.events.mark()
    rig.page.fill("#guideFind", "new")
    ask = rig.events.wait(Get, where=lambda e: e.what == "groups" and e.find == "new", after=mark)
    news = [row for row in GROUPS["rows"] if row["title"] == "News"]
    answer(rig, sid, ask, dict(GROUPS, find="new", rows=news))
    rig.page.wait_for_selector("#guideRows .row.branch")
    assert rig.page.locator("#guideRows .row.branch").count() == 1
    mark = rig.events.mark()
    rig.page.locator("#guideRows .row.branch", has_text="News").click()
    ask = rig.events.wait(Get, where=lambda e: e.what == "guide", after=mark)
    assert (ask.list, ask.page, ask.find) == ("g3", 0, ""), ask
    mark = rig.events.mark()
    rig.page.fill("#guideFind", "fern")
    ask = rig.events.wait(Get, where=lambda e: e.what == "guide" and e.find == "fern", after=mark)
    assert (ask.list, ask.page) == ("g3", 0), ask


def scenario_channel_list(rig: Rig) -> None:
    """Channels swaps the remote's arrows for the channel list, where the TV is, with the volume
    and channel buttons still below; a row tunes; groups and find as the Guide's; Channels again
    brings the arrows back, and the choice holds when the page opens again."""
    sid = paired_sid(rig)
    rig.hub.send(sid, STATE)
    mark = rig.events.mark()
    rig.page.click("#listButton")
    ask = rig.events.wait(Get, where=lambda e: e.what == "channels", after=mark)
    assert (ask.list, ask.page, ask.find) == ("", 0, ""), ask
    answer(rig, sid, ask, CHANNELS)
    rig.page.wait_for_selector("#zapRows .row >> nth=2")
    assert rig.page.locator("#pad").is_hidden(), "the arrows still show"
    assert rig.page.locator("[data-key=volup]").is_visible(), "the volume went"
    assert rig.page.locator("#zapRows .row.playing").count() == 1
    assert rig.page.inner_text("#zapTitle").startswith("All"), rig.page.inner_text("#zapTitle")
    labelled(rig, "zapFind")
    rig.shot("channel-list")
    mark = rig.events.mark()  # a row tunes
    rig.page.locator("#zapRows .row", has_text="Fernvale Sports").click()
    assert rig.events.wait(OpenItem, after=mark).handle == "h2"
    tuned = {**STATE["now"], "num": "2", "name": "Fernvale Sports"}
    rig.hub.send(sid, {**STATE, "now": tuned})  # the TV tuned: Watching moves
    rig.page.locator("#zapRows .row.playing", has_text="Fernvale Sports").wait_for(timeout=5000)
    assert rig.page.locator("#zapRows .row.playing").count() == 1
    mark = rig.events.mark()  # up to the groups, into one, then find in it
    rig.page.click("#zapUp")
    ask = rig.events.wait(Get, where=lambda e: e.what == "chgroups", after=mark)
    answer(rig, sid, ask, {**GROUPS, "t": "chgroups"})
    rig.page.wait_for_selector("#zapRows .row.branch >> nth=2")
    rig.shot("channel-list-groups")
    mark = rig.events.mark()
    rig.page.locator("#zapRows .row.branch", has_text="News").click()
    ask = rig.events.wait(Get, where=lambda e: e.what == "channels", after=mark)
    assert ask.list == "g3", ask
    mark = rig.events.mark()
    rig.page.fill("#zapFind", "fern")
    ask = rig.events.wait(
        Get, where=lambda e: e.what == "channels" and e.find == "fern", after=mark
    )
    assert ask.list == "g3", ask
    rig.page.click("#listButton")  # the arrows again
    assert rig.page.locator("#pad").is_visible() and rig.page.locator("#zap").is_hidden()
    mark = rig.events.mark()
    rig.page.click("#listButton")
    rig.events.wait(Get, where=lambda e: e.what == "channels", after=mark)
    mark = rig.events.mark()
    rig.page.reload()  # opened again: the list, where the TV is
    ask = rig.events.wait(Get, where=lambda e: e.what == "channels", after=mark, timeout=10)
    assert ask.list == "", ask
    assert rig.page.locator("#zap").is_visible() and rig.page.locator("#pad").is_hidden()
    rig.page.set_viewport_size({"width": 320, "height": 568})
    answer(rig, rig.events.wait(Opened, after=mark).sid, ask, CHANNELS)
    rig.page.wait_for_selector("#zapRows .row >> nth=2")
    rig.shot("channel-list-narrow")


def scenario_find_both(rig: Rig) -> None:
    """On the groups, the find shows the groups it matches and, below them, the channels it
    finds: in the Guide (a row's ▶ tunes) and in the Remote tab's list (a row tunes)."""
    sid = paired_sid(rig)
    news = [row for row in GROUPS["rows"] if row["title"] == "News"]
    found = {**GROUPS, "find": "new", "rows": news, "channels": GUIDE["rows"][:1], "found": 1}
    mark = rig.events.mark()
    rig.page.click("[data-tab=guide]")
    rig.events.wait(Get, where=lambda e: e.what == "guide", after=mark)
    mark = rig.events.mark()
    rig.page.click("#guideUp")
    rig.events.wait(Get, where=lambda e: e.what == "groups", after=mark)
    assert "channel" in (rig.page.get_attribute("#guideFind", "placeholder") or "")
    mark = rig.events.mark()
    rig.page.fill("#guideFind", "new")
    ask = rig.events.wait(Get, where=lambda e: e.what == "groups" and e.find == "new", after=mark)
    answer(rig, sid, ask, found)
    rig.page.locator("#guideRows .group", has_text="Channels").wait_for(timeout=5000)
    assert rig.page.locator("#guideRows .row.branch").count() == 1
    rig.shot("guide-find-both")
    mark = rig.events.mark()
    rig.page.locator("#guideRows .row", has_text="Harbourline News").locator(".side").click()
    assert rig.events.wait(OpenItem, after=mark).handle == "h1"
    rig.page.click("[data-tab=remote]")
    mark = rig.events.mark()
    rig.page.click("#listButton")
    rig.events.wait(Get, where=lambda e: e.what == "channels", after=mark)
    mark = rig.events.mark()
    rig.page.click("#zapUp")
    rig.events.wait(Get, where=lambda e: e.what == "chgroups", after=mark)
    mark = rig.events.mark()
    rig.page.fill("#zapFind", "new")
    ask = rig.events.wait(Get, where=lambda e: e.what == "chgroups" and e.find == "new", after=mark)
    bare = [{k: v for k, v in r.items() if k not in ("now", "next")} for r in GUIDE["rows"][:1]]
    answer(rig, sid, ask, {**found, "t": "chgroups", "channels": bare})
    rig.page.locator("#zapRows .group", has_text="Channels").wait_for(timeout=5000)
    mark = rig.events.mark()
    rig.page.locator("#zapRows .row", has_text="Harbourline News").click()
    assert rig.events.wait(OpenItem, after=mark).handle == "h1"


def scenario_watch_here(rig: Rig) -> None:
    """Watching on this phone: More's Watch on this phone docks a player above the channel list
    and asks for what the TV plays; a channel tapped then plays here (its link in the player);
    the TV taking over stops it; TV | Phone switches, TV handing it back."""
    sid = paired_sid(rig)
    rig.hub.send(sid, STATE)
    rig.page.click("#moreButton")
    rig.page.wait_for_selector("#moreSheet:not([hidden])")
    rig.shot("more-watch-here")
    mark = rig.events.mark()
    rig.page.click("#watchHere")
    watch = rig.events.wait(WatchHere, after=mark)
    assert watch.handle == "", watch
    ask = rig.events.wait(Get, where=lambda e: e.what == "channels", after=mark)
    answer(rig, sid, ask, CHANNELS)
    rig.page.wait_for_selector("#zapRows .row >> nth=2")
    assert rig.page.locator("#player").is_visible(), "no player"
    assert rig.page.locator("[data-key=volup]").is_hidden(), "the TV's buttons still show"
    assert rig.page.locator("#zap .where [data-where=phone].on").count() == 1
    rig.hub.send(sid, {"t": "play", "failed": True})  # nothing on the TV: pick one here
    rig.page.locator("#playerName", has_text="below").wait_for(timeout=5000)
    mark = rig.events.mark()
    rig.page.locator("#zapRows .row", has_text="Fernvale Sports").click()
    watch = rig.events.wait(WatchHere, after=mark)
    assert watch.handle == "h2", watch
    assert not rig.events.of(OpenItem, after=mark), "the TV was tuned"
    # A provider's stream is on another address than the page: the page's policy lets it in
    # (localhost is another origin than 127.0.0.1; the file is no video, so the player errs).
    rig.page.evaluate(
        "window.blocked = []; document.addEventListener('securitypolicyviolation', "
        "(e) => window.blocked.push(e.violatedDirective + ' ' + e.blockedURI))"
    )
    link = f"http://localhost:{rig.hub.port}/icon-192.png?stream=2"
    rig.hub.send(sid, {"t": "play", "url": link, "name": "Fernvale Sports", "num": "2"})
    rig.page.locator("#playerOut").wait_for(timeout=8000)  # it can't play this: the way out
    src = rig.page.evaluate("document.getElementById('video').getAttribute('src')")
    assert src == link, src
    assert rig.page.inner_text("#playerName") == "Fernvale Sports"
    blocked = rig.page.evaluate("window.blocked")
    assert not blocked, f"the page's policy blocked the stream: {blocked}"
    # Picture in picture as the page is left, where the browser does it (its own controls have
    # the button).
    assert rig.page.get_attribute("#video", "autopictureinpicture") is not None
    rig.shot("watch-here")
    rig.hub.send(sid, {"t": "play", "url": "javascript:alert(1)"})  # only http(s) plays
    rig.page.wait_for_timeout(300)
    assert rig.page.evaluate("document.getElementById('video').getAttribute('src')") == link
    # The Library, watching here: the player stays above it, the switch says Phone; a film
    # plays here, a series opens its seasons on the TV.
    mark = rig.events.mark()
    rig.page.click("[data-tab=library]")
    answer(rig, sid, rig.events.wait(Get, where=lambda e: e.what == "genres", after=mark), GENRES)
    rig.page.wait_for_selector("#libraryRows .row.branch >> nth=1")
    assert rig.page.locator("#player").is_visible(), "the player went with the tab"
    assert rig.page.locator("[data-screen=library] .where [data-where=phone].on").count() == 1
    mark = rig.events.mark()
    rig.page.locator("#libraryRows .row.branch", has_text="Drama").click()
    ask = rig.events.wait(Get, where=lambda e: e.what == "library", after=mark)
    titles = [
        {"id": "m1", "title": "Harbour Lights", "sub": "1998", "plays": True},
        {"id": "s1", "title": "Harbour Watch", "sub": ""},
    ]
    head = {"t": "library", "section": "movies", "genre": "g2", "title": "Drama", "count": 2}
    answer(rig, sid, ask, {**head, "find": "", "page": 0, "pages": 1, "items": titles})
    rig.page.wait_for_selector("#libraryRows .row >> nth=1")
    rig.shot("watch-here-library")
    mark = rig.events.mark()
    rig.page.locator("#libraryRows .row", has_text="Harbour Lights").click()
    assert rig.events.wait(WatchHere, after=mark).handle == "m1"
    assert rig.page.inner_text("#playerName") == "Harbour Lights"
    # It goes on where it stopped ("start"), reporting nothing before it got there; then how
    # far it got goes to QiTV (paused here), for the TV to go on from.
    film = f"http://localhost:{rig.hub.port}/icon-192.png?film=m1"
    rig.hub.send(sid, {"t": "play", "url": film, "name": "Harbour Lights", "start": 4320})
    rig.page.wait_for_function(
        "(url) => document.getElementById('video').getAttribute('src') === url", arg=film
    )
    mark = rig.events.mark()
    rig.page.evaluate("""() => { const v = document.getElementById('video');
          let at = 0;
          Object.defineProperty(v, 'duration', { value: 5400, configurable: true });
          Object.defineProperty(v, 'currentTime', {
            configurable: true, get: () => at, set: (value) => { at = value; } });
          v.dispatchEvent(new Event('timeupdate'));
          v.dispatchEvent(new Event('loadedmetadata')); }""")
    assert rig.page.evaluate("document.getElementById('video').currentTime") == 4320
    rig.page.wait_for_timeout(300)
    assert not rig.events.of(Position, after=mark), "it reported 0 before going on"
    rig.page.evaluate("""() => { const v = document.getElementById('video');
          v.currentTime = 4381; v.dispatchEvent(new Event('pause')); }""")
    where = rig.events.wait(Position, after=mark)
    assert (where.position_ms, where.length_ms) == (4381000, 5400000), where
    # A film no phone plays (its sound, say): QiTV says why, where its name was, in full.
    rig.hub.send(sid, {"t": "play", "failed": True, "why": "sound"})
    rig.page.locator("#playerName", has_text="sound").wait_for(timeout=5000)
    assert rig.page.locator(".player-cap.why").count() == 1
    assert rig.page.evaluate("document.getElementById('video').getAttribute('src')") is None
    rig.shot("watch-here-why")
    # A recording (2.8) asked for through the relay: QiTV says it plays from home.
    rig.hub.send(sid, {"t": "play", "failed": True, "why": "rec_relay"})
    rig.page.locator("#playerName", has_text="recordings").wait_for(timeout=5000)
    mark = rig.events.mark()
    rig.page.locator("#libraryRows .row", has_text="Harbour Watch").click()
    assert rig.events.wait(OpenItem, after=mark).handle == "s1"
    assert not rig.events.of(WatchHere, after=mark), "a series played here"
    rig.page.click("[data-tab=remote]")
    rig.hub.send(sid, {"t": "stop"})  # the TV took over
    rig.page.locator("#banner", has_text="TV").wait_for(timeout=5000)
    assert rig.page.locator("#player").is_hidden() and rig.page.locator("#now").is_visible()
    assert rig.page.locator("#zap .where [data-where=tv].on").count() == 1
    mark = rig.events.mark()
    rig.page.click("#zap .where [data-where=phone]")
    rig.events.wait(WatchHere, after=mark)
    mark = rig.events.mark()
    rig.page.click("#zap .where [data-where=tv]")
    rig.events.wait(ToTv, after=mark)
    assert rig.page.locator("#player").is_hidden()


def scenario_providers(rig: Rig) -> None:
    """With two providers, the groups have the providers above them: All providers, then each;
    the one chosen is asked for once (source) and its groups come with its name."""
    sid = paired_sid(rig)
    mark = rig.events.mark()
    rig.page.click("[data-tab=guide]")
    answer(rig, sid, rig.events.wait(Get, where=lambda e: e.what == "guide", after=mark), GUIDE)
    rig.page.wait_for_selector("#guideRows .row >> nth=2")
    mark = rig.events.mark()
    rig.page.click("#guideUp")
    ask = rig.events.wait(Get, where=lambda e: e.what == "groups", after=mark)
    answer(rig, sid, ask, {**GROUPS, "pick": True, "source": "Provider A"})
    rig.page.wait_for_selector("#guideRows .row.branch >> nth=2")
    assert rig.page.inner_text("#guideTitle").startswith("Provider A"), rig.page.inner_text(
        "#guideTitle"
    )
    assert rig.page.locator("#guideUp").is_visible(), "no way up to the providers"
    assert rig.page.inner_text("#guideUp") == "Providers", rig.page.inner_text("#guideUp")
    mark = rig.events.mark()
    rig.page.click("#guideUp")
    ask = rig.events.wait(Get, where=lambda e: e.what == "sources", after=mark)
    sources = [
        {"id": "s0", "title": "All providers"},
        {"id": "s1", "title": "Provider A", "on": True},
        {"id": "s2", "title": "Provider B"},
    ]
    answer(rig, sid, ask, {"t": "sources", "rows": sources})
    rig.page.wait_for_selector("#guideRows .row.branch >> nth=2")
    assert rig.page.locator("#guideRows .row.branch.on", has_text="Provider A").count() == 1
    assert rig.page.locator("#guideFind").is_hidden() and rig.page.locator("#guideUp").is_hidden()
    rig.shot("providers")
    mark = rig.events.mark()
    rig.page.locator("#guideRows .row.branch", has_text="All providers").click()
    ask = rig.events.wait(Get, where=lambda e: e.what == "groups", after=mark)
    assert ask.source == "s0", ask
    answer(rig, sid, ask, {**GROUPS, "pick": True, "source": "All providers"})
    rig.page.locator("#guideTitle", has_text="All providers").wait_for(timeout=5000)
    mark = rig.events.mark()
    rig.page.fill("#guideFind", "news")  # the find asks again, without choosing again
    ask = rig.events.wait(Get, where=lambda e: e.what == "groups" and e.find == "news", after=mark)
    assert ask.source == "", ask


def scenario_guide_pages(rig: Rig) -> None:
    """Opened on the TV's channel, page 3 of 5: the channel shows, the pages above and below
    come as the list scrolls, and what shows stays put."""
    sid = paired_sid(rig)
    mark = rig.events.mark()
    rig.page.click("[data-tab=guide]")
    ask = rig.events.wait(Get, where=lambda e: e.what == "guide", after=mark)
    mark = rig.events.mark()
    answer(rig, sid, ask, channels_page(2, 5, playing=10))
    rig.page.wait_for_selector("#guideRows .row.playing")
    assert rig.page.locator("#guideRows .row.playing").is_visible()
    ask = rig.events.wait(Get, where=lambda e: e.what == "guide" and e.page == 1, after=mark)
    assert ask.list == "g2", ask
    top = rig.page.evaluate(
        "document.querySelector('#guideRows .playing').getBoundingClientRect().top"
    )
    answer(rig, sid, ask, channels_page(1, 5))
    rig.page.wait_for_selector("#guideRows .row >> nth=99")
    moved = rig.page.evaluate(
        "document.querySelector('#guideRows .playing').getBoundingClientRect().top"
    )
    assert abs(moved - top) < 2, (top, moved)  # the page above went above
    mark = rig.events.mark()
    rig.page.locator("#guideRows .row >> nth=99").scroll_into_view_if_needed()
    rig.events.wait(Get, where=lambda e: e.what == "guide" and e.page == 3, after=mark)


def scenario_timetable(rig: Rig) -> None:
    sid = paired_sid(rig)
    mark = rig.events.mark()
    rig.page.click("[data-tab=guide]")
    answer(rig, sid, rig.events.wait(Get, where=lambda e: e.what == "guide", after=mark), GUIDE)
    rig.page.wait_for_selector("#guideRows .row >> nth=2")
    mark = rig.events.mark()
    rig.page.locator("#guideRows .row", has_text="Harbourline News").locator(".row-tap").click()
    ask = rig.events.wait(Get, where=lambda e: e.what == "timetable", after=mark)
    assert ask.handle == "h1", ask
    rig.page.wait_for_selector("[data-screen=timetable]:not([hidden])")
    loading = {"t": "timetable", "id": "h1", "name": "Harbourline News", "num": "1", "rows": []}
    rig.hub.send(sid, dict(loading, loading=True))  # its guide is still on its way
    rig.page.locator("#timetableRows .empty", has_text="Loading").wait_for()
    rows = [
        dict(programme("Morning edition", -140, -80), id="p1"),  # the archive keeps it
        dict(programme("Midday", -80, -20)),  # past, not kept
        dict(programme("Evening bulletin", -20, 25), on=True, id="h1", about="The day's news."),
        dict(programme("Weather", 25, 55)),
    ]
    for row in rows:
        row.setdefault("on", False)
    rig.hub.send(sid, dict(loading, rows=rows))
    rig.page.wait_for_selector("#timetableRows .slot >> nth=3")
    assert rig.page.locator("#timetableRows .slot.playing").count() == 1
    assert rig.page.locator("#timetableRows .slot.still").count() == 1
    assert rig.page.locator("[data-tab=guide].on").count() == 1
    rig.shot("timetable")
    mark = rig.events.mark()
    rig.page.locator("#timetableRows .slot", has_text="Morning edition").click()
    assert rig.events.wait(OpenItem, after=mark).handle == "p1"
    mark = rig.events.mark()
    rig.page.click("#timetableWatch")
    assert rig.events.wait(OpenItem, after=mark).handle == "h1"
    rig.page.click("#timetableBack")
    rig.page.wait_for_selector("[data-screen=guide]:not([hidden])")


def scenario_search_mirror(rig: Rig) -> None:
    """Search is the phone's own: the TV's search isn't opened, the text goes to QiTV (not to
    the TV's search), the results show, a tap opens one, Enter only puts the keyboard away.
    Show on TV: the TV's search opens and shows what is typed, Enter presses its OK."""
    sid = paired_sid(rig)
    mark = rig.events.mark()
    rig.page.click("#searchButton")
    rig.page.wait_for_selector("[data-screen=search]:not([hidden])")
    assert rig.page.evaluate("document.activeElement.id") == "searchInput"
    rig.page.keyboard.type("harb", delay=30)
    query = rig.events.wait(Query, where=lambda e: e.text == "harb", after=mark)
    assert not query.tv, query
    assert not rig.events.of(Press, after=mark), "the TV's search was opened"
    rig.hub.send(sid, RESULTS)
    rig.page.wait_for_selector("#searchRows .row >> nth=1")
    groups = rig.page.locator("#searchRows .group").all_inner_texts()
    assert len(groups) == 2, groups
    rig.shot("search")
    rig.hub.send(sid, {**RESULTS, "q": "har", "groups": []})  # an older text's: not shown
    rig.page.wait_for_timeout(300)
    assert rig.page.locator("#searchRows .row").count() == 2
    mark = rig.events.mark()
    rig.page.locator("#searchRows .row", has_text="Harbour Lights").click()
    assert rig.events.wait(OpenItem, after=mark).handle == "r2"
    mark = rig.events.mark()
    rig.page.focus("#searchInput")
    rig.page.keyboard.press("Enter")
    rig.page.wait_for_timeout(400)
    assert not rig.events.of(Press, after=mark), "Enter pressed the TV's OK"
    mark = rig.events.mark()  # Show on TV
    rig.page.click("#searchTv")
    rig.events.wait(Press, where=lambda e: e.button == "search" and not e.down, after=mark)
    query = rig.events.wait(Query, where=lambda e: e.text == "harb" and e.tv, after=mark)
    assert rig.page.locator("#searchTv.on").count() == 1
    mark = rig.events.mark()
    rig.page.focus("#searchInput")
    rig.page.keyboard.press("Enter")
    rig.events.wait(Press, where=lambda e: e.button == "ok" and not e.down, after=mark)


def scenario_library_open(rig: Rig) -> None:
    sid = paired_sid(rig)
    mark = rig.events.mark()
    rig.page.click("[data-tab=library]")
    ask = rig.events.wait(Get, where=lambda e: e.what == "genres", after=mark)
    assert (ask.section, ask.page, ask.find) == ("movies", 0, ""), ask
    answer(rig, sid, ask, GENRES)
    rig.page.wait_for_selector("#libraryRows .row.branch >> nth=1")
    assert rig.page.locator("#libraryRows .group", has_text="Provider A").count() == 1
    assert rig.page.locator("#libraryChips").is_visible()
    rig.shot("genres")
    mark = rig.events.mark()  # the find finds genres and, below them, the section's titles
    rig.page.fill("#libraryFind", "harb")
    ask = rig.events.wait(Get, where=lambda e: e.what == "genres" and e.find == "harb", after=mark)
    harbour = {"id": "c1", "title": "Harbour Lights", "sub": "1998"}
    found = {**GENRES, "find": "harb", "rows": [], "titles": [harbour], "found": 1}
    answer(rig, sid, ask, found)
    rig.page.locator("#libraryRows .group", has_text="Movies").wait_for()
    rig.shot("genres-find")
    mark = rig.events.mark()
    rig.page.locator("#libraryRows .row", has_text="Harbour Lights").click()
    assert rig.events.wait(OpenItem, after=mark).handle == "c1"
    mark = rig.events.mark()
    rig.page.fill("#libraryFind", "")
    ask = rig.events.wait(Get, where=lambda e: e.what == "genres" and e.find == "", after=mark)
    answer(rig, sid, ask, GENRES)
    rig.page.wait_for_selector("#libraryRows .row.branch >> nth=1")
    mark = rig.events.mark()  # into a genre; its titles come as the list scrolls
    rig.page.locator("#libraryRows .row.branch", has_text="Drama").click()
    ask = rig.events.wait(Get, where=lambda e: e.what == "library", after=mark)
    assert (ask.section, ask.genre, ask.page, ask.find) == ("movies", "g2", 0, ""), ask
    answer(rig, sid, ask, titles_page(0, 50))
    rig.page.wait_for_selector("#libraryRows .row >> nth=49")
    assert rig.page.inner_text("#libraryTitle").startswith("Drama")
    assert rig.page.locator("#libraryChips").is_hidden()
    assert rig.page.locator("#libraryUp").is_visible()
    rig.shot("library")
    mark = rig.events.mark()
    rig.page.locator("#libraryRows .row >> nth=49").scroll_into_view_if_needed()
    ask = rig.events.wait(Get, where=lambda e: e.what == "library" and e.page == 1, after=mark)
    assert ask.genre == "g2", ask
    answer(rig, sid, ask, titles_page(1, 10))
    rig.page.wait_for_selector("#libraryRows .row >> nth=59")
    mark = rig.events.mark()
    rig.page.locator("#libraryRows .row", has_text="Harbour Lights").click()
    assert rig.events.wait(OpenItem, after=mark).handle == "c1"
    mark = rig.events.mark()  # the find box searches the genre, on QiTV
    rig.page.fill("#libraryFind", "harb")
    ask = rig.events.wait(Get, where=lambda e: e.what == "library" and e.find == "harb", after=mark)
    assert (ask.genre, ask.page) == ("g2", 0), ask
    mark = rig.events.mark()  # back up; Series; a failed load offers a retry
    rig.page.click("#libraryUp")
    rig.events.wait(Get, where=lambda e: e.what == "genres", after=mark)
    mark = rig.events.mark()
    rig.page.click("#libraryChips [data-lib=series]")
    ask = rig.events.wait(
        Get, where=lambda e: e.what == "genres" and e.section == "series", after=mark
    )
    failed = {"t": "genres", "section": "series", "find": "", "page": 0, "pages": 1, "rows": []}
    answer(rig, sid, ask, dict(failed, loading=False, failed=True))
    rig.page.wait_for_selector("#libraryRows .again")
    mark = rig.events.mark()
    rig.page.click("#libraryRows .again")
    ask = rig.events.wait(Get, where=lambda e: e.what == "genres", after=mark)
    assert (ask.section, ask.page) == ("series", 0), ask


def scenario_lists_after_reconnect(rig: Rig) -> None:
    """An answer to an earlier ask shows nothing; after a reconnect (the phone slept) the list
    shown comes again, asked by its key, which holds across sessions."""
    sid = paired_sid(rig)
    mark = rig.events.mark()
    rig.page.click("[data-tab=library]")
    answer(rig, sid, rig.events.wait(Get, where=lambda e: e.what == "genres", after=mark), GENRES)
    rig.page.wait_for_selector("#libraryRows .row.branch >> nth=1")
    mark = rig.events.mark()
    rig.page.locator("#libraryRows .row.branch", has_text="Drama").click()
    ask = rig.events.wait(Get, where=lambda e: e.what == "library", after=mark)
    rig.hub.send(sid, {**titles_page(0, 50), "n": ask.n + 7})  # another ask's answer
    rig.page.wait_for_timeout(500)
    assert rig.page.locator("#libraryRows .row").count() == 0, "an earlier ask's answer showed"
    answer(rig, sid, ask, titles_page(0, 50))
    rig.page.wait_for_selector("#libraryRows .row >> nth=49")
    mark = rig.events.mark()
    rig.page.evaluate("import('./app.js').then((m) => m.default.conn.ws.close())")  # it slept
    rig.events.wait(Opened, after=mark, timeout=15)
    again = rig.events.wait(Get, where=lambda e: e.what == "library", after=mark, timeout=10)
    assert (again.section, again.genre, again.page) == ("movies", "g2", 0), again
    answer(rig, again.sid, again, titles_page(0, 50))  # its new session's
    rig.page.wait_for_selector("#libraryRows .row >> nth=49")


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


def scenario_stop(rig: Rig) -> None:
    """2.6: More's Stop: nothing plays on the TV, nothing streams (as X and ■ there)."""
    paired_sid(rig)
    rig.page.click("#moreButton")
    rig.page.wait_for_selector("#moreSheet:not([hidden])")
    stop = rig.page.locator("#moreSheet [data-key=stop]")
    assert stop.inner_text().strip() == "Stop", stop.inner_text()
    rig.shot("more-stop")
    mark = rig.events.mark()
    stop.click()
    rig.events.wait(Press, where=lambda e: e.button == "stop" and not e.down, after=mark)


def scenario_pip_close(rig: Rig) -> None:
    """While the TV shows a small picture, More's button says it closes it."""
    sid = paired_sid(rig)
    rig.page.click("#moreButton")
    rig.page.wait_for_selector("#moreSheet:not([hidden])")
    pip = rig.page.locator("#moreSheet [data-key=pip]")
    assert pip.inner_text().strip() == "Picture in picture", pip.inner_text()
    rig.hub.send(sid, dict(STATE, pip=True, recording=True))
    rig.page.locator("#moreSheet [data-key=pip].on").wait_for()
    # lit as it records: the remote's own Record key (2.7.2: no longer under More)
    assert rig.page.locator(".cluster [data-key=record].on").count() == 1
    assert rig.page.locator("#moreSheet [data-key=record]").count() == 0
    assert pip.inner_text().strip() == "Close picture in picture", pip.inner_text()
    rig.shot("more-pip")
    mark = rig.events.mark()
    pip.click()
    rig.events.wait(Press, where=lambda e: e.button == "pip" and not e.down, after=mark)
    rig.hub.send(sid, dict(STATE, pip=False))
    rig.page.locator("#moreSheet [data-key=pip]:not(.on)").wait_for()
    assert pip.inner_text().strip() == "Picture in picture", pip.inner_text()


def scenario_add_home(rig: Rig) -> None:
    """2.7.2: the menu offers Add to Home Screen; without a browser's install prompt it shows
    the steps; the LIVE key lights while the channel plays behind its live point."""
    sid = paired_sid(rig)
    rig.hub.send(sid, dict(STATE, now=dict(STATE["now"], behind_s=90)))
    rig.page.locator(".cluster [data-key=live].behind").wait_for()
    rig.page.click("#menuButton")
    rig.page.wait_for_selector("#menuSheet:not([hidden])")
    rig.page.locator("#menuList button", has_text="Add to Home Screen").click()
    rig.page.locator("#menuSheet:not([hidden]) .step").first.wait_for()
    assert rig.page.locator("#menuTitle").inner_text() == "Add to Home Screen"
    rig.shot("add-home")
    rig.hub.send(sid, STATE)
    rig.page.locator(".cluster [data-key=live]:not(.behind)").wait_for()


def scenario_guide_follows_tv(rig: Rig) -> None:
    """A Guide opened where the TV is follows the TV's channel: Watching moves, and a channel
    beyond the rows shown opens the Guide where the TV is again; a group the phone chose stays."""
    sid = paired_sid(rig)
    rig.hub.send(sid, STATE)
    mark = rig.events.mark()
    rig.page.click("[data-tab=guide]")
    answer(rig, sid, rig.events.wait(Get, where=lambda e: e.what == "guide", after=mark), GUIDE)
    rig.page.locator("#guideRows .row.playing", has_text="Harbourline News").wait_for()
    sports = dict(STATE["now"], num="2", name="Fernvale Sports")
    mark = rig.events.mark()
    rig.hub.send(sid, dict(STATE, now=sports))  # zapped on the computer, within the rows
    rig.page.locator("#guideRows .row.playing", has_text="Fernvale Sports").wait_for()
    assert rig.page.locator("#guideRows .row.playing").count() == 1
    assert rig.page.locator("#guideRows .row.playing em").inner_text() == "Watching"
    rig.page.wait_for_timeout(500)
    assert not rig.events.of(Get, after=mark), "a channel in the rows needs no new list"
    far = dict(STATE["now"], num="77", name="Larkspur Kids")
    mark = rig.events.mark()
    rig.hub.send(sid, dict(STATE, now=far))  # a channel beyond the rows: where the TV is now
    ask = rig.events.wait(Get, where=lambda e: e.what == "guide", after=mark)
    assert (ask.list, ask.page, ask.find) == ("", 0, ""), ask
    answer(rig, sid, ask, GUIDE)
    rig.page.wait_for_selector("#guideRows .row >> nth=2")
    mark = rig.events.mark()  # the phone picks a group: the Guide stops following
    rig.page.click("#guideUp")
    answer(rig, sid, rig.events.wait(Get, where=lambda e: e.what == "groups", after=mark), GROUPS)
    mark = rig.events.mark()
    rig.page.locator("#guideRows .row.branch", has_text="News").click()
    answer(rig, sid, rig.events.wait(Get, where=lambda e: e.what == "guide", after=mark), GUIDE)
    rig.page.wait_for_selector("#guideRows .row >> nth=2")
    mark = rig.events.mark()
    rig.hub.send(sid, dict(STATE, now=far))
    rig.hub.send(sid, dict(STATE, now=sports))
    rig.page.locator("#guideRows .row.playing", has_text="Fernvale Sports").wait_for()
    rig.page.wait_for_timeout(800)
    assert not rig.events.of(Get, after=mark), "a group the phone chose followed the TV"


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
    # The narrowest phone, the longest hint (Turkish): the hint stays inside the touchpad.
    rig.page.set_viewport_size({"width": 320, "height": 568})
    turkish = "Gezinmek için kaydırın · Tamam için dokunun"
    rig.page.evaluate("t => { document.querySelector('#touchpad span').textContent = t; }", turkish)
    pad = rig.page.locator("#touchpad").bounding_box()
    hint = rig.page.locator("#touchpad span").bounding_box()
    assert pad and hint, (pad, hint)
    inside = (
        hint["x"] >= pad["x"]
        and hint["y"] >= pad["y"]
        and hint["x"] + hint["width"] <= pad["x"] + pad["width"]
        and hint["y"] + hint["height"] <= pad["y"] + pad["height"]
    )
    assert inside, (pad, hint)
    rig.shot("touchpad-narrow")
    rig.page.reload()  # remembered on this phone
    rig.page.wait_for_selector("#touchpad:not([hidden])")


def touch_drag(rig: Rig, x: float, y: float, dy: float, *, steps: int = 6, ms: int = 120) -> None:
    """A finger at (x, y) moved `dy` down and lifted (Chrome's touch emulation)."""
    cdp = rig.page.context.new_cdp_session(rig.page)
    cdp.send("Emulation.setTouchEmulationEnabled", {"enabled": True, "maxTouchPoints": 1})
    cdp.send("Input.dispatchTouchEvent", {"type": "touchStart", "touchPoints": [{"x": x, "y": y}]})
    for k in range(1, steps + 1):
        point = {"x": x, "y": y + dy * k / steps}
        cdp.send("Input.dispatchTouchEvent", {"type": "touchMove", "touchPoints": [point]})
        rig.page.wait_for_timeout(ms / steps)
    cdp.send("Input.dispatchTouchEvent", {"type": "touchEnd", "touchPoints": []})
    cdp.detach()


def scenario_sheet_swipe(rig: Rig) -> None:
    """2.8.10: More's sheet dragged down from its top closes, and a short drag springs back; a
    drag that starts on a key leaves it open; while it is open the remote under it holds still
    (an iPhone scrolled the page under a sheet a swipe could not scroll)."""
    paired_sid(rig)
    sheet = "#moreSheet"
    rig.page.click("#moreButton")
    rig.page.wait_for_selector(f"{sheet}:not([hidden])")
    held = rig.page.evaluate(
        "() => [document.body.classList.contains('sheet-open'),"
        " getComputedStyle(document.querySelector('.remote')).overflowY]"
    )
    assert held == [True, "hidden"], held
    grab = rig.page.locator(f"{sheet} .grab").bounding_box()
    assert grab is not None
    x, y = grab["x"] + grab["width"] / 2, grab["y"] + grab["height"] / 2
    touch_drag(rig, x, y, 40)  # short: it springs back
    rig.page.wait_for_timeout(300)
    state = rig.page.evaluate(
        "s => [document.querySelector(s).hidden, document.querySelector(s).style.transform]", sheet
    )
    assert state == [False, ""], state
    key = rig.page.locator(f"{sheet} [data-key=d5]").bounding_box()
    assert key is not None
    touch_drag(rig, key["x"] + key["width"] / 2, key["y"] + key["height"] / 2, 300)
    rig.page.wait_for_timeout(300)
    assert rig.page.evaluate("s => document.querySelector(s).hidden", sheet) is False
    rig.shot("sheet-open")
    touch_drag(rig, x, y, 320)  # down from its top: it closes
    rig.page.wait_for_selector(sheet, state="hidden")
    after = rig.page.evaluate(
        "() => [document.body.classList.contains('sheet-open'),"
        " getComputedStyle(document.querySelector('.remote')).overflowY,"
        " document.getElementById('veil').hidden]"
    )
    assert after == [False, "auto", True], after


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


def scenario_cast(rig: Rig) -> None:
    """2.8: More's Cast to a TV lists what QiTV found (looking until then); a tap casts there,
    Stop casting brings the picture back; a list that arrives under another menu leaves it."""
    sid = paired_sid(rig)
    rig.page.click("#moreButton")
    rig.page.wait_for_selector("#moreSheet:not([hidden])")
    mark = rig.events.mark()
    rig.page.click("#castTo")
    rig.events.wait(Get, where=lambda e: e.what == "cast", after=mark)
    rig.page.locator("#menuList", has_text="Looking for devices").wait_for()
    rig.hub.send(sid, {"t": "cast", "devices": [{"name": "Den", "on": False}], "on": ""})
    mark = rig.events.mark()
    rig.page.locator("#menuList button", has_text="Den").click()
    rig.events.wait(CastTo, where=lambda e: e.to == "Den", after=mark)
    rig.page.click("#moreButton")
    rig.page.click("#castTo")
    rig.hub.send(sid, {"t": "cast", "devices": [{"name": "Den", "on": True}], "on": "Den"})
    rig.page.locator("#menuList button.danger").wait_for()
    rig.shot("cast")
    mark = rig.events.mark()
    rig.page.click("#menuList button.danger")
    rig.events.wait(CastTo, where=lambda e: e.to == "", after=mark)
    since = int(time.time()) - 60
    rig.hub.send(sid, {**STATE, "rec": [{"id": "b1", "name": "Quayside Sport", "since": since}]})
    rig.page.locator("#recLine", has_text="Quayside Sport").wait_for()
    rig.page.click("#recLine")
    rig.hub.send(sid, {"t": "cast", "devices": [{"name": "Den", "on": False}], "on": ""})
    rig.page.wait_for_timeout(300)
    title = rig.page.inner_text("#menuTitle")
    assert title == "REC", f"a cast list replaced the REC menu: {title!r}"


def scenario_schedule(rig: Rig) -> None:
    """2.8: a programme to come records at its time from the timetable (once or its series),
    a scheduled one cancels; the chips' taps never tune the row."""
    sid = paired_sid(rig)
    mark = rig.events.mark()
    rig.page.click("[data-tab=guide]")
    answer(rig, sid, rig.events.wait(Get, where=lambda e: e.what == "guide", after=mark), GUIDE)
    rig.page.wait_for_selector("#guideRows .row >> nth=2")
    rig.page.locator("#guideRows .row", has_text="Harbourline News").locator(".row-tap").click()
    rig.page.wait_for_selector("[data-screen=timetable]:not([hidden])")
    table = {"t": "timetable", "id": "h1", "name": "Harbourline News", "num": "1"}
    later = dict(programme("Weather", 25, 55), on=False, sched="s4", rec=False)
    rig.hub.send(sid, dict(table, rows=[later]))
    rig.page.wait_for_selector("#timetableRows .slot .chip >> nth=1")
    rig.shot("schedule")
    mark = rig.events.mark()
    rig.page.locator("#timetableRows .chip", has_text="Record").click()
    rig.events.wait(Schedule, where=lambda e: e.handle == "s4" and e.do == "once", after=mark)
    rig.page.locator("#timetableRows .chip", has_text="Series").click()
    rig.events.wait(Schedule, where=lambda e: e.do == "series", after=mark)
    rig.hub.send(sid, dict(table, rows=[dict(later, sched="s5", rec=True)]))
    rig.page.locator("#timetableRows .chip", has_text="Cancel recording").click()
    rig.events.wait(Schedule, where=lambda e: e.handle == "s5" and e.do == "cancel", after=mark)
    assert not rig.events.of(OpenItem, after=mark), "a chip's tap tuned the row"


def scenario_timer(rig: Rig) -> None:
    """2.8: a channel with no guide records at a time from its timetable: on chosen days, or
    once; a time gone by cannot be sent; QiTV's answer shows by the button."""
    sid = paired_sid(rig)
    mark = rig.events.mark()
    rig.page.click("[data-tab=guide]")
    answer(rig, sid, rig.events.wait(Get, where=lambda e: e.what == "guide", after=mark), GUIDE)
    rig.page.wait_for_selector("#guideRows .row >> nth=2")
    rig.page.locator("#guideRows .row", has_text="Harbourline News").locator(".row-tap").click()
    rig.page.wait_for_selector("[data-screen=timetable]:not([hidden])")
    table = {"t": "timetable", "id": "h1", "name": "Harbourline News", "num": "1", "rows": []}
    offset = datetime.now().astimezone().utcoffset()
    here = int(offset.total_seconds() // 60) if offset is not None else 0
    rig.hub.send(sid, dict(table, timer=True, tz=here))
    rig.page.wait_for_selector("#timerOpen:visible")
    rig.page.click("#timerOpen")
    rig.page.wait_for_selector("#timerForm:not([hidden])")
    assert rig.page.locator("#timerClock").is_hidden(), "the same clock: nothing to say"
    assert rig.page.is_enabled("#timerSchedule"), "the next hour can be scheduled at once"
    rig.page.select_option("#timerRepeat", "days")
    assert not rig.page.is_enabled("#timerSchedule"), "no day ticked"
    rig.page.locator("#timerDays .chip >> nth=0").click()
    rig.page.locator("#timerDays .chip >> nth=2").click()
    rig.page.fill("#timerFrom", "23:30")
    rig.page.fill("#timerTo", "01:00")
    rig.page.fill("#timerName", "Late film")
    hint = rig.page.inner_text("#timerHint")
    assert hint == "Ends the next day.", hint
    rig.shot("timer-form")
    mark = rig.events.mark()
    rig.page.click("#timerSchedule")
    asked = rig.events.wait(Timer, where=lambda e: e.handle == "h1", after=mark)
    assert (asked.title, asked.begin, asked.minutes, asked.days, asked.day) == (
        "Late film",
        23 * 60 + 30,
        90,
        (0, 2),
        "",
    ), asked
    assert rig.page.locator("#timerForm").is_hidden()
    rig.hub.send(sid, {"t": "timer", "id": "h1", "text": "Records Harbourline News 23:30–01:00"})
    rig.page.locator("#timerNote", has_text="Records Harbourline News").wait_for()
    rig.shot("timer-sent")
    rig.page.click("#timerOpen")  # once, today, at a time already over
    rig.page.fill("#timerFrom", "00:00")
    rig.page.fill("#timerTo", "00:01")
    assert not rig.page.is_enabled("#timerSchedule"), "a time gone by was offered"
    assert rig.page.inner_text("#timerHint") == "That time has passed."
    rig.page.click("#timerCancel")
    rig.hub.send(sid, dict(table, timer=True, tz=here + 180))  # QiTV three hours ahead
    rig.page.wait_for_function(
        "document.querySelector('#timerOpen') && !document.querySelector('#timerBar').hidden"
    )
    rig.page.click("#timerOpen")
    clock = rig.page.inner_text("#timerClock")
    assert clock.startswith("Times are QiTV's clock:"), clock
    rig.shot("timer-clock")


SCENARIOS.update(
    {
        "cast": scenario_cast,
        "schedule": scenario_schedule,
        "timer": scenario_timer,
        "forget-clean": scenario_forget_clean,
        "hello-keeps-now": scenario_hello_keeps_now,
        "now-playing": scenario_now_playing,
        "back-to": scenario_back_to,
        "ask": scenario_ask,
        "stop": scenario_stop,
        "rec-line": scenario_rec_line,
        "guide-tune": scenario_guide_tune,
        "channel-list": scenario_channel_list,
        "find-both": scenario_find_both,
        "providers": scenario_providers,
        "watch-here": scenario_watch_here,
        "guide-pages": scenario_guide_pages,
        "timetable": scenario_timetable,
        "search-mirror": scenario_search_mirror,
        "library-open": scenario_library_open,
        "lists-after-reconnect": scenario_lists_after_reconnect,
        "more-numpad": scenario_more_numpad,
        "pip-close": scenario_pip_close,
        "add-home": scenario_add_home,
        "guide-follows-tv": scenario_guide_follows_tv,
        "touchpad": scenario_touchpad,
        "sheet-swipe": scenario_sheet_swipe,
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
        rig.page.wait_for_timeout(6500)  # it hopped; the address hangs; the deadline passes
        assert rig.page.url.startswith(rig.relay or ""), rig.page.url
        mark = rig.events.mark()
        rig.page.click("[data-key=ok]")  # still a working remote, through the relay
        rig.events.wait(Press, where=lambda e: e.button == "ok", after=mark, timeout=5)
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
    "page-versions": scenario_page_versions,
    "hop-back-on-loss": scenario_hop_back_on_loss,
    "two-tvs": scenario_two_tvs,
    "scan-again-relay": scenario_scan_again_relay,
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
    if args.scenario == "all" and not os.environ.get("QITV_KEPT_PAGE"):
        names = [name for name in names if name != "page-versions"]  # needs a kept page
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
