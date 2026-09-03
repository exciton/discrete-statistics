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

# `default:` values
DEFAULT_RECORD = "record"
DEFAULT_RECORD_KNOWN = "record_known"
DEFAULT_IGNORE = "ignore"

# Skip a scheduled run when the recorder queue is deeper than this. The
# watermark is data-derived, so a skipped run costs nothing but latency.
BACKLOG_THRESHOLD = 1000
