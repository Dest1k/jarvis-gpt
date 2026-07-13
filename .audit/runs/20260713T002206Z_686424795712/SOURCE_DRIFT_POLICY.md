# Source drift policy

Baseline production commit: `686424795712cb0a562750b6dade13de18c48792`.

PHASE B compares the current production tree to that commit while excluding `.audit/**` and `docs/audit/**`. If any other path differs, create `SOURCE_DRIFT.md` with paths/commits, reread each affected file, rerun relevant static/live checks and mark prior conclusions pending. Do not transfer findings or candidate tasks automatically across drift.
