"""What a provider's refusal of a live stream is shown as (spec 6.9).

Kept apart from services.live.source, which reads the streams in the time-shift helper
process: the GUI needs these messages, not the HTTP and HLS libraries the source loads.
The helper never sets a language: these stay English (code compares them) and are shown with
services.i18n.translate.
"""

from services.i18n import N_

# Shown in place of an "http" failure's message when the provider refused for its limit.
# i18n: error: the provider allows no more streams now
STREAM_LIMIT = N_("The provider refused the stream: its connection limit is reached.")
# i18n: error: the provider refused the stream (perhaps for its connection limit)
STREAM_REFUSED = N_("The provider refused the stream: it may limit connections.")  # a plain 403
REFUSALS = (STREAM_LIMIT, STREAM_REFUSED)
