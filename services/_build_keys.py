"""Keys a release build carries: written by CI while it packages QiTV, from its secrets.

Empty in the repository (never commit a key here): a build from the source code uses the
key the user enters in Settings. CI writes OPENSUBTITLES_API_KEY from the secret named
QITV_OPENSUBTITLES_KEY. QiTV uses the user's own key when one is set, else this one.
"""

OPENSUBTITLES_API_KEY = ""
