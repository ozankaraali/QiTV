"""What a provider's refusal of a live stream is shown as (spec 6.9).

Kept apart from services.live.source, which reads the streams in the time-shift helper
process: the GUI needs these messages, not the HTTP and HLS libraries the source loads.
"""

# Shown in place of an "http" failure's message when the provider refused for its limit.
STREAM_LIMIT = "The provider refused the stream: its connection limit is reached."
STREAM_REFUSED = "The provider refused the stream: it may limit connections."  # a plain 403
REFUSALS = (STREAM_LIMIT, STREAM_REFUSED)
