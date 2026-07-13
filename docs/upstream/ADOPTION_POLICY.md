# Upstream adoption policy

## Purpose and boundary

This is an engineering provenance and adoption gate. It is not legal advice and
does not make claims about model authorship. A machine `PASS` means only that
the declared record is structurally complete, referenced local evidence is
present with the declared hashes, blocking review states are absent, and a
recorded human approval is present. It does not independently prove that a
license, security assessment, or approval is substantively correct.

The gate is fail closed. External work may be researched, registered, and
proposed, but it must not be copied, installed, executed, merged, pushed, or
used to change production merely because it is popular or has a candidate
record.

## Engineering origin kinds

Every component or candidate uses exactly one origin kind:

| Origin kind | Engineering meaning |
| --- | --- |
| `internal_human` | Implemented directly by an internal human contributor. |
| `commissioned_internal` | Commissioned internally and implemented by a named agent, with no imported external code. |
| `inspired_by` | An external mechanism informed the design; imported code must be declared separately. |
| `external_dependency` | Consumed as a pinned external dependency. |
| `external_adapter` | Internally implemented boundary around an external system. |
| `vendored` | External material copied into the repository. |
| `ported_code` | External code translated or materially adapted. |
| `forked` | Development based on an external repository fork. |
| `generated_fixture` | Synthetic, generated test material with no production role. |

`commissioned_internal` is an engineering provenance label. It requires
`commissioned_by`, `implementation_agent`, and
`external_code_imported: false`; it does not require an upstream repository.
Internal origins reject repository, commit, license-snapshot, source-manifest,
imported-path, adoption-mode, and other external-only fields. External origins
likewise reject internal commissioning fields.
If external code is later imported, the affected work must use the external
candidate gate and must not remain represented as purely commissioned internal
work.

## Adoption modes

External candidates select one mode: `idea_only`, `test_corpus`,
`external_dependency`, `black_box_adapter`, `ported_module`, or `fork`.
`idea_only` does not require copied source files or imported destinations. It
does not waive repository, pinned commit, license, review, test, provenance, or
human-approval requirements for an adoption decision.

`test_corpus`, `ported_module`, and `fork` are copied-code modes. They require
the exact upstream source file list, a provenance record, imported destination
paths, and source/result hashes. Each `source_files.path` is resolved beneath a
separately supplied reviewed-source root and its declared SHA-256 is recomputed
from the exact raw bytes. Every imported source must match exactly one such
manifest entry; every destination is resolved beneath a separately supplied
destination root and its result SHA-256 is independently recomputed. Missing,
unmapped, stale, escaped, non-regular, oversized, symlink, junction, and reparse
paths fail. Transformation prose never substitutes for either hash. Other
non-idea modes still require the exact upstream files reviewed for the proposed
adoption. `idea_only` cannot declare imported code or destinations.

## Required gate evidence

No external candidate may pass without all of the following:

1. A reproduced finding ID or a concrete capability gap.
2. The exact canonical HTTPS repository URL: lowercase host, no credentials,
   port, query, fragment, whitespace, backslash, percent encoding, dot segment,
   repeated separator, or trailing slash.
3. A pinned 40-character Git commit SHA, never a branch or tag.
4. A license record taken from that repository. Its repository-relative path is
   distinct from the local sanitized evidence path. Verified or explicitly
   approved claims require that local evidence; the validator rehashes its exact
   raw bytes and requires the provenance `license_snapshot.sha256` to match.
5. The exact upstream source files reviewed, except that `idea_only` may use an
   empty list.
6. One declared adoption mode.
7. Separate dependency and security reviews with bounded, sanitized evidence.
8. An isolated spike result with bounded, sanitized evidence.
9. Regression, failure, and rollback tests.
10. A provenance record matching the candidate and pinned source.
11. Explicit human approval with immutable evidence.

Evidence references are repository-relative paths beneath `docs/upstream/`,
`docs/assurance/`, or `qa/`. They must not reference `.audit`, runtime state,
absolute paths, traversal paths, or unsanitized credential material. Every
asserted evidence reference carries a SHA-256 digest. The validator reads only
the explicitly referenced files and performs no network access or discovery.
The matched allowed-prefix directory is resolved as the exact read root;
descriptor-based bounded reads reject symlink/reparse escapes and path changes
before consuming bytes. Source and destination roots are explicit independent
inputs and use the same canonical-path and bounded-read policy.

## License handling

| Repository license evidence | Gate behavior |
| --- | --- |
| MIT/BSD/Apache-like | Candidate only after repository license and notice verification. |
| GPL/AGPL/LGPL/MPL | Mandatory explicit review before the gate can pass. |
| Custom/source-available | Blocked pending explicit review. |
| No license | Code copying is forbidden; adoption remains blocked. |
| Unknown | Blocked. |

The policy records engineering controls, not a legal conclusion. A permissive
classification requires `VERIFIED` status and verified notices. Copyleft,
custom, and source-available classifications require
`EXPLICIT_REVIEW_APPROVED`. `NO_LICENSE`, `UNKNOWN`, and pending review states
remain `BLOCKED`. A permissive identifier never waives notice verification or
the separately recorded human adoption decision. The outer provenance-record
hash does not replace the license-snapshot byte binding.

## Verdicts and decision ownership

- `PASS`: the offline gate is mechanically complete and all referenced hashes
  match. The recorded human decision remains authoritative.
- `BLOCKED`: the document is structurally usable, but a required review,
  verification, evidence file, or approval is unresolved.
- `FAIL`: the document is malformed, internally inconsistent, falsely claims
  evidence, omits a hard requirement, or contains a rejected/failed gate.

Deterministic `FAIL` is never overridden by a reviewer. Missing evidence never
becomes `PASS`. The donor registry begins empty; future projects may be added as
`UNVERIFIED` research backlog but must not be described as approved donors
until a completed decision record says so.

## Offline validation

From the repository root:

```powershell
py -3.11 -m qa.upstream docs\upstream\candidates\<candidate>.json --evidence-root <sanitized-evidence-root> --source-root <reviewed-source-root> --destination-root <adoption-destination-root>
```

Exit codes are `0=PASS`, `1=FAIL`, `2=BLOCKED`, and `3=VALIDATOR_ERROR`.
Validation never downloads a repository, installs a dependency, executes donor
code, or modifies candidate/evidence files. Omitting a root required by the
selected adoption mode yields `BLOCKED`; malformed or contradictory metadata
and byte mismatches yield `FAIL`. Standalone provenance validation without its
candidate/root context is structural only and cannot establish an adoption
`PASS`.
