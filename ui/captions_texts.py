"""AI captions' texts (2.9): what ui/captions_controller.py says, kept here (marked, shown with
translate()) so that every catalog carries them, and the sizes it and Settings show."""

from services import i18n
from services.i18n import N_, tr

# The AI captions row's state (the Subtitles panel), at its right.
# i18n: the AI captions row's state while the speech model downloads; percent: 0 to 100
DOWNLOADING = N_("Downloading {percent}%")
PREPARING = N_("Preparing…")  # i18n: the AI captions row's state while the speech model loads
# i18n: the AI captions row: the picture slows a little to get the few seconds captions need
CATCHING_UP = N_("Catching up…")
# The live picture waits a few seconds once so that captions, made from the sound ahead of it,
# come in time (the AI captions row, the captions' place and the Stay live indicator).
# i18n: AI captions: the live picture waits a few seconds once so that captions come in time
STARTING = N_("Starting captions…")
CANT_KEEP_UP = N_("Can't keep up")  # i18n: the AI captions row: this computer is too slow
# i18n: the AI captions row: a stream QiTV can't read ahead (it has no time-shift)
NOT_AVAILABLE = N_("Not available for this stream")
COULDNT_START = N_("Couldn't start AI captions")  # i18n: a toast
# i18n: the question before AI captions' first use; size: as "880 MB"
DOWNLOAD_QUESTION = N_(
    "AI captions need a speech model on this computer: {size} to download once. Download it now?"
)
# AI captions fell behind with the larger speech model (Automatic) and the faster isn't here.
# i18n: the question when AI captions fall behind with the larger speech model; size: "880 MB"
STEP_DOWN_QUESTION = N_(
    "AI captions can't keep up on this computer. Download the faster speech model ({size}) "
    "and switch to it?"
)
# i18n: a toast: AI captions fell behind with the larger speech model and now use the faster one
STEPPED_DOWN = N_("AI captions switched to the faster speech model to keep up")
DOWNLOAD = N_("Download")  # i18n: the AI captions question's yes (verb)
NOT_NOW = N_("Not now")  # i18n: the AI captions question's no
# i18n: a toast; reason: why, as the system or the server said it
DOWNLOAD_FAILED = N_("The AI captions download failed: {reason}")
# Where the language AI captions hear came from: the Language row's "Türkçe · audio track".
# i18n: the AI captions language came from the audio track (lowercase, after the language)
FROM_AUDIO_TRACK = N_("audio track")
FROM_VIEWER = N_("you chose")  # i18n: the AI captions language: the viewer picked it (lowercase)
# i18n: the AI captions language is Settings' default language (lowercase, after the language)
FROM_DEFAULT = N_("default")
# i18n: the AI captions language: the speech model finds it by itself (lowercase)
FROM_MODEL = N_("automatic")

_MB = 1000 * 1000
_GB = 1000 * _MB


def size_text(size: int) -> str:
    """A download's size as people read it: "880 MB", "2.3 GB" (decimal units)."""
    if size >= _GB:
        number = i18n.decimal(f"{size / _GB:.1f}".rstrip("0").rstrip("."))
        return tr("{size} GB", size=number)  # i18n: a size in gigabytes, as "2.3"
    return tr("{size} MB", size=i18n.number(max(1, round(size / _MB))))  # i18n: in megabytes
