# Data and trust boundaries

| Data | Source/trust | Validation | Storage/sinks | Retention/exposure | PHASE B |
|---|---|---|---|---|---|
| Chat/tool args | operator/model; mixed | Pydantic + tool schema + approval | messages, audit, approvals, LLM | largely unbounded; approval payload gap | retry/isolation/secrets |
| Web pages/results | untrusted remote | hardened core HTTP; inconsistent Playwright policy | evidence, prompt, memory | provenance varies | redirect/subresource/injection/TLS |
| Documents/archives | untrusted local | type/path/size checks; symlink/atomicity gaps | copied files, chunks, generated output | no unified cleanup TTL | symlink, bombs, collision |
| Host/process actions | privileged | capability config, SafeGate, PID identity | session/replay/audit | bounded replay details | approve/cancel/retry/orphan |
| SQLite/runtime KV | trusted state, corruption possible | permissive JSON, no version/integrity gate | WAL, backup, API/UI | append-only growth | corruption/lock/full/restore |
| Secrets/tokens | protected env | redaction on some sinks | worker env, approvals/audit risk | undefined | synthetic sentinel scan |
| Profiles/models | operator config + filesystem | name/path checks, no artifact compatibility gate | override, dispatcher | durable override | all profiles/switch rollback |

Session isolation, deletion semantics and sensitive-data retention require live/copied-state confirmation.
