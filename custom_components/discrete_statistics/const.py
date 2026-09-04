"""Constants for the discrete_statistics integration."""

DOMAIN = "discrete_statistics"

HOUR = 3600.0

METRIC_DURATION = "duration"
METRIC_COUNT = "count"

# States ignored by the `record_known` default.
UNKNOWN_STATES = frozenset({"unknown", "unavailable"})

# Dispositions
DISPOSITION_IGNORE = "ignore"
DISPOSITION_RECORD = "record"
# Recorded, unless the entity leaves the state again within `min_duration`.
DISPOSITION_IGNORE_SHORT = "ignore_short"

# `default:` values
DEFAULT_RECORD = "record"
DEFAULT_RECORD_KNOWN = "record_known"
DEFAULT_IGNORE = "ignore"
DEFAULT_IGNORE_SHORT = "ignore_short"
# `record_known`, except that `unavailable` and `unknown` are recorded when
# they last `min_duration`.
DEFAULT_IGNORE_SHORT_UNKNOWN = "ignore_short_unknown"

# The longest `min_duration`. The compiler reads one extra hour before a
# window to find the state carried into it, and that has to be enough to
# measure a spell that began before the window.
MAX_MIN_DURATION = HOUR

# Skip a scheduled run when the recorder queue is deeper than this. The
# watermark is data-derived, so a skipped run costs nothing but latency.
BACKLOG_THRESHOLD = 1000
