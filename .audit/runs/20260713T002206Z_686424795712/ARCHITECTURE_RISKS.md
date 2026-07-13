# Architecture risks

- `tools.py` (~15k lines), `page.tsx` (~5.8k) and ad-hoc storage commits concentrate unrelated behavior and make policy consistency hard.
- Core HTTP, Playwright browser, CDP and bundled synthesis have divergent trust policies.
- Main SQLite, vault, audit/event bus and file outputs lack a general transactional outbox/unit-of-work abstraction.
- Whole-array runtime KV and append-only histories create concurrency and growth hazards.
- Launcher cleanup/state owns cross-process lifecycle through heuristics rather than one supervisor identity model.
- Runtime Linux/browser/process hardening is not exercised by current Windows-only backend CI.
- Sync SQLite/filesystem/regex operations inside async flows can create loop lag under slow disk or hostile inputs.
