# Static executive summary

PHASE A found a large, safety-conscious local agent runtime with strong typed execution, exact approval binding, process identity checks, hardened core HTTP transport, non-root/read-only Compose settings, substantial backend assertions and a buildable frontend.

The main risks are boundary inconsistency rather than absence of controls: browser paths bypass the hardened HTTP policy; browser worker containment exposes secrets/data; state mutations frequently lack one atomic unit; launcher ownership is inferred too broadly; autonomy/chat retries lack durable idempotency; frontend truth/realtime contracts have drifted; and archive/document failure paths are not atomic.

Evidence: 254 features, 22 requirements, 830 collected pytest cases, 394 safe cases passing, 50.37% line coverage for that deliberately restricted subset, clean lint/compile/typecheck/build/config/schema checks, and two intentional `FAIL_HERMETIC` reproductions. Findings: {'high': 15, 'medium': 9, 'low': 1}; status mix {'static-confirmed': 17, 'probable-runtime': 4, 'spec-gap': 3, 'test-gap': 1}.

Runtime has not been verified. Spark remains blocked until all applicable PHASE B scenarios are executed and findings are confirmed/refuted.
