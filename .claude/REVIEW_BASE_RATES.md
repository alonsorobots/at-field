# Review base rates (AT-Field)

Defects reviews actually confirmed in this repo. Read before generic classes.

- test-mirrors-the-caller: a fix is pinned only by a test that calls the new helper directly, so reverting the CALL SITE in run_service stays green — seen: 0.4.19 P2, 2026-10-03. Ask: if I put the old line back in the service loop, which test goes red?
- untested-recovery-path: a "survives restart" behaviour (spool from the previous process) had no test; disabling it left the suite green — seen: 0.4.19 P3, 2026-10-03. Ask: is there a test that starts with state left on disk by a previous run?
- dormant-guard re-armed: fixing a guard that was silently off (lean scan returned []) turns ON a killer nobody has watched for weeks, against workloads that grew meanwhile -- seen: 0.4.25 / 2026-10-25 (32 GB RSS cap vs 96 GB MDBPE). Ask: what has run on these hosts since the guard went blind, and would it trip the guard now?
- representative-pid race: choosing a tree by aggregate but naming one short-lived MEMBER as the offender lets the member exit before the re-walk, skipping the whole kill -- seen: 0.4.25 / 2026-10-25. Ask: does the pid handed to execute() outlive the seconds between scan and kill?
- boundary argument untested: a grouping walk's protected_pids was load-bearing but every test had one slot, so dropping it stayed green -- seen: 0.4.25 / 2026-10-25. Ask: is there a test with TWO subtrees under the same protected parent?
