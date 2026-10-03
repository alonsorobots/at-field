# Review base rates (AT-Field)

Defects reviews actually confirmed in this repo. Read before generic classes.

- test-mirrors-the-caller: a fix is pinned only by a test that calls the new helper directly, so reverting the CALL SITE in run_service stays green — seen: 0.4.19 P2, 2026-10-03. Ask: if I put the old line back in the service loop, which test goes red?
- untested-recovery-path: a "survives restart" behaviour (spool from the previous process) had no test; disabling it left the suite green — seen: 0.4.19 P3, 2026-10-03. Ask: is there a test that starts with state left on disk by a previous run?
