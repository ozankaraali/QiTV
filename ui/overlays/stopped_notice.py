"""After Stop (2.6): nothing plays and nothing streams. A pill at the top centre (the sleep
timer's, ui/overlays/sleep_notice.py) says "Stopped", with Play again: a click on it plays what
was stopped again, as Space and ▶ do (the shell's `play_again`). It stays until something plays.
"""

from __future__ import annotations

from services.i18n import N_, translate
from ui.overlays.sleep_notice import SleepNotice

STOPPED = N_("Stopped")  # i18n: a pill after the Stop button: nothing plays
PLAY_AGAIN = N_("Play again")  # i18n: button: play what was stopped again


class StoppedNotice(SleepNotice):
    def button_text(self) -> str:
        return translate(PLAY_AGAIN)
