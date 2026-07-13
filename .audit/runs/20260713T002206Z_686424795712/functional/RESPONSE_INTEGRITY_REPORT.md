# Response integrity report

- Authoritative synthetic turbo harness: 52 PASS, 0 FAIL/ERROR, 2 optional SKIP.
- GUI semantic review exposed raw `call:*`/tool JSON on document and runtime routes.
- Mono GUI attempts frequently produced no terminal answer within the bounded 15-second window; direct probes emitted repeated `cyclic` tokens.
- Navigation interruption produced empty stale assistant state in the GUI.
- No functional READY marker is allowed while response leaks, empty finals, and profile degeneration remain.
