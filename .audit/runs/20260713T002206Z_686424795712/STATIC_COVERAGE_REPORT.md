# Static coverage report

The approved 394-test subset covered 16,103 of 31,972 executable backend statements: **50.37%**. Full per-file JSON is `evidence/static/coverage.json`; report stdout is EVID-STATIC-016.

High coverage examples: storage 87%, operations 86%, persona 88%, redaction 95%, verification 93%. Risk-heavy low coverage in this restricted run: tools 24%, runtime lease 16%, web-surfer adapter 27%, state verification 41%, web surfer 46%, model hub 0%, worker 0%. Several low values are caused by deliberately excluded socket/process/runtime suites, but they still define PHASE B priorities.

Frontend has zero unit/component/E2E coverage; only typecheck and production build passed.
