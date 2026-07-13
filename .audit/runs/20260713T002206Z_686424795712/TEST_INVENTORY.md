# Test inventory

- 52 tracked test/fixture files; `pytest --collect-only` expands to 830 cases.
- Safe subset: 394 passed. Broad sandbox run: 717 passed, 3 skipped, then 68 failures/16 errors dominated by denied AF_UNIX/socket/process containment; classified `BLOCKED_BY_ENV`, not product FAIL.
- Strong: approvals/execution/replay/planner/storage happy paths, model/dispatcher contracts, web parsing/core destination checks, document/tool routing.
- Weak/absent: frontend behavioral tests; PowerShell behavior; Docker/Compose render/start; browser effective sandbox; DB/vault/audit fault injection; concurrent jobs/chat/events; retry idempotency; stream disconnect; retention/migrations; symlink/sensitive ingest; archive rollback; hostile regex.
- 16 skip-decorated plus explicit skip cases create OS coverage gaps. CI is Windows/Python 3.11 while the container is Linux/Python 3.12.

Fixtures are predominantly fake/mock/synthetic. Host-bridge and process-tree modules were not executed in PHASE A because the campaign forbids those surfaces here.
