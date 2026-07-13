# SPARK-0001 — Direct DNS question is misrouted to shopping

- Status: `READY`
- Priority: `P2`
- Source finding: `FUNC-FIND-001`
- Dependencies: none
- Allowed files: backend/src/jarvis_gpt/agent.py; backend/src/jarvis_gpt/tools.py; backend/src/jarvis_gpt/shop_registry.py; backend/tests/test_shop_routing.py; backend/tests/test_tools.py

## Problem

Shopping/catalog workflow ran and did not answer the DNS request.

## Harmless reproduction

Start the isolated turbo profile and submit OP-0006 exactly twice in separate new chats. Verify the trace route is shopping/catalog and neither final contains the DNS answer.

Use only the isolated functional home and controlled fixtures. Do not use production credentials or external mutable state.

## Implementation scope

Address the single hypothesis: Intent classification overweights shopping/network catalog terms.

## Regression test

Add `test_dns_question_does_not_route_to_shop` to `backend/tests/test_shop_routing.py`; assert the selected intent is DNS/network and no shop tool is present. Run `py -3.11 -m pytest backend/tests/test_shop_routing.py backend/tests/test_tools.py -q`.

## Validation and cleanup

- Run the focused regression test while iterating.
- Run the directly affected backend/frontend suite once after integration.
- Keep generated files under a temporary test directory and remove only task-owned fixtures.

## Binary acceptance criteria

Both deterministic repeats route to DNS/network lookup and return one factual sentence without shopping output.
