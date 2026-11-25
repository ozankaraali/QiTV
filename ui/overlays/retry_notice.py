"""A live channel whose stream ended and opens again by itself (`services.revive`): a pill at the
top centre (the sleep timer's, ui/overlays/sleep_notice.py) says "The stream ended · Trying
again in 0:25", counting down ("Reconnecting…" while a try opens), with Try now: a click on it,
or Enter, tries at once (`keepWatching`, the live controls' `retry_now`). It stays until the
channel plays again or another one is chosen. Over a Kick or Twitch channel that is not live
(2026-11-25) it says so instead: "examplechannel isn't live now · It plays as soon as it's live"
(Try now asks its site at once).
"""

from __future__ import annotations

from services.i18n import N_, translate
from services.live.live_session import RECONNECTING
from services.revive import fmt_wait
from ui.overlays.sleep_notice import SleepNotice

# i18n: the pill over a live channel whose stream ended, before "Trying again in {time}"
STREAM_ENDED = N_("The stream ended")
# i18n: the pill over a live channel whose stream ended: when QiTV tries it again by itself
TRYING_AGAIN = N_("Trying again in {time}")  # i18n: {time} counts down: 0:25, 5:00
TRY_NOW = N_("Try now")  # i18n: button: open the live channel's stream again now; short
# The pill over a Kick or Twitch channel that isn't streaming now: QiTV checks now and then.
# i18n: a pill, after "{channel} isn't live now ·": the channel plays once its stream starts
WAITS_FOR_LIVE = N_("It plays as soon as it's live")


def retry_text(seconds: float) -> str:
    """ "Trying again in 0:25" (in the active language); "Reconnecting…" once the try is due."""
    if seconds <= 0.0:
        return translate(RECONNECTING)
    return translate(TRYING_AGAIN, time=fmt_wait(seconds))


def ended_text(seconds: float) -> str:
    """The pill's text: "The stream ended · Trying again in 0:25"."""
    return f"{translate(STREAM_ENDED)} · {retry_text(seconds)}"


def waiting_text(why: str) -> str:
    """The pill over a site's channel that is not live (2026-11-25), `why` in the active
    language: "examplechannel isn't live now · It plays as soon as it's live"."""
    return f"{why} · {translate(WAITS_FOR_LIVE)}"


class RetryNotice(SleepNotice):
    accessible_name = STREAM_ENDED

    def button_text(self) -> str:
        return translate(TRY_NOW)
