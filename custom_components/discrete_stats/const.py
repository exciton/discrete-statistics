"""Constants for the discrete_stats integration."""

DOMAIN = "discrete_stats"

HOUR = 3600.0

# Reserved canonical state for spans the compiler cannot attribute to a
# real state: before an entity's first known state, or across a gap whose
# source rows have been purged.
NO_DATA = "no_data"

METRIC_SECONDS = "seconds"
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
