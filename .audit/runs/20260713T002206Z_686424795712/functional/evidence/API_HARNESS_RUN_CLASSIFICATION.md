# API Harness Run Classification

## Authority

| Run | Raw result | Classification |
| --- | --- | --- |
| `jarvis-functional-turbo-20260713T163718Z-7c4de202d0f4` | 2 PASS, 27 FAIL, 1 ERROR, 24 SKIP | Preserved, non-authoritative harness/environment failure |
| `jarvis-functional-turbo-v2-20260713T163821Z-88fd91140364` | 52 PASS, 0 FAIL, 0 ERROR, 2 optional SKIP | Authoritative API/CLI functional result: PASS |

The first run inherited the system HTTP proxy. Its uniform empty HTTP 503
responses came from that proxy path, not from the loopback JARVIS API. The run
is retained as raw evidence but must not be used to create product findings or
to downgrade runtime acceptance.

The corrected v2 run disabled inherited proxy handling with `trust_env=False`
and exercised the intended direct loopback endpoint. Its manifest verdict is
the accepted result.

## Accepted Status Mapping

- v2 `PASS` cases: accepted as verified functional passes.
- v2 `F051 SKIP`: accepted as not applicable because strict loopback token mode
  was disabled; the case is optional.
- v2 `F054 SKIP`: accepted as not applicable because
  `keep_conversations=true`; the case is optional.
- First-run `PASS`, `FAIL`, `ERROR`, and `SKIP`: map to
  `NON_AUTHORITATIVE_HARNESS_ENVIRONMENT_FAILURE` for final acceptance.
- First-run HTTP 503s, dependent skips, and stream cascade error: no accepted
  product defect.

## Final Accepted Result

`API/CLI functional harness: PASS (authoritative v2 run)`

All raw JSONL, CSV, and manifest files remain unchanged and preserved.
