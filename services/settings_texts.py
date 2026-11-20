"""Texts Settings shows in the drawer and in the old Settings window alike (no Qt), kept here
rather than in ui/settings_dialog.py, which goes once no section is left in that window: the
questions it asks before what can't be taken back, and what it says of OpenSubtitles' key.
"""

from __future__ import annotations

from services.i18n import N_

# i18n: asked by Settings' Clear Image Cache before it clears it (2.9.3)
CLEAR_CACHE_QUESTION = N_("Clear the image cache? Posters and logos download again as they show.")
# i18n: Settings → Subtitles, in a build that has an OpenSubtitles key of its own
OPENSUBTITLES_BUILT_IN_HINT = N_(
    "This copy of QiTV has a key of its own: yours is optional, and is used instead when set."
)
