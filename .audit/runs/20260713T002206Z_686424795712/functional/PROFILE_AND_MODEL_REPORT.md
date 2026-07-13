# Profile and model report

| Profile | Resolved model | Context/config | Observed result |
|---|---|---|---|
| gemma4-turbo | `/models/gemma4-26b-a4b-nvfp4` served as `dispatcher` | 32768, CUDA graph, FP8 KV, max seqs 16 | Interactive; final keyboard smoke answered in about 1.1 s. |
| gemma4-mono-perf | `/models/gemma4-31b-it-nvfp4` served as `dispatcher` | 4096, eager, 2.5 GiB CPU offload | Three direct probes returned repeated `cyclic`; GUI fallback/timeout. |
| gemma4-mono | `/models/gemma4-31b-it-nvfp4` served as `dispatcher` | 16384, eager, 24 GiB CPU and 16 GiB KV offload, max seqs 1 | Readiness crossed the 20-minute deadline; one direct completion took 47.25 s and returned repeated `cyclic`; observed decode 0.1-0.4 tok/s. |

Launcher selection, resolved API settings, container command, provider `/v1/models`, loaded model root, and GUI display were captured for every profile. Identity mapping was truthful; functional quality/readiness for both 31B profiles failed.
