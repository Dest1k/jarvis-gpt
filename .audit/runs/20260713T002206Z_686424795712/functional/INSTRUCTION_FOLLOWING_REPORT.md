# Instruction following report

Operator gate completed: 68 cases, 169/169 required repeat keys. Accepted repeat totals: FAIL=49, INCONCLUSIVE=74, PASS=46.

| Profile | PASS | FAIL | INCONCLUSIVE | blocked |
|---|---:|---:|---:|---:|
| gemma4-mono | 0 | 0 | 31 | 0 |
| gemma4-mono-perf | 0 | 2 | 29 | 0 |
| gemma4-turbo | 46 | 47 | 14 | 0 |

Turbo completed useful direct answers in a substantial subset, but exact count/JSON constraints, follow-up references, ambiguity handling, document work, citations, and runtime status tasks failed repeatably. Both independent profile reviews disagreed on classifying 60 degraded 31B outcomes, so the prompt rule records them as INCONCLUSIVE; direct provider probes still independently establish degenerate `cyclic` output.
