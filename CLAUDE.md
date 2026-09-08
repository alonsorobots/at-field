# AT-Field — working notes for agents

AT-Field is the watchdog that stops these machines cooking themselves. It kills
the user's own compute processes when a machine crosses a thermal or memory
wall. Both directions are expensive: a spurious kill throws away in-flight work,
and a missed one is hardware damage.

## Before you change ANY thermal threshold or disable ANY thermal rule

**Read [`docs/AURORA_THERMALS.md`](docs/AURORA_THERMALS.md) first.** It is not
optional background — it documents an incident where a healthy machine was hard-
killed **63 times in three hours** by a sensor that does not physically exist,
and a second one where a real hardware protection was switched off on an
inference that turned out to be wrong.

The four checks it requires, in short:

1. **Name the sensor.** Core, hot spot, or memory junction — and does it exist
   on *that* hardware? Turing (2070 SUPER) has a hot spot and **no** memory
   junction. Blackwell (5090) has a memory junction and **no** hot spot. They
   are exactly complementary.
2. **Check for duplicates.** Two signals identical to several decimals are one
   sensor wearing two names. That is precisely how the 63 kills happened.
3. **Never cross collectors in an argument.** NVML indexes GPUs by PCI order,
   LHM by sensor-enumeration order. **They have been observed transposed on
   this fleet.** Pairing an NVML number with an LHM number for "the same card"
   is unsound reasoning, and it has produced a confident wrong answer before.
4. **Load one physical card and see which signal moves.** The only test that has
   ever settled a sensor question here. Minutes to run; it has overturned every
   confident wrong answer so far.

**The asymmetry that decides ties:** spurious kills are recoverable — AT-Field
requeues without burning retries. A cooked card is not. **When uncertain, leave
the guard armed and raise the question.**

## Fleet facts that are easy to get wrong

- `nvidia-smi --query-gpu=fan.speed` reports a **target**, not the actual fan
  state. Reading it as real once produced a "the fans never ramp" conclusion
  that HWiNFO disproved (fans were at 58% / 42%).
- **AT-Field cannot see fans, pump RPM, or CPU package power on any host.** A
  cooling failure is therefore invisible until temperature rises. If you need
  that data, the user must export HWiNFO — it has been decisive twice.
- Temperature tracks **power**, not utilization %. A 9950X3D pulls 80–126 W at
  11–27% "usage". Diagnosing cooling from utilization produced a wrong
  "your pump is failing, service the cooler" recommendation.
- Hosts do **not** share thresholds. Chronos `gpu-core-hot` is 88 °C, DEMETER
  and Aurora 83 °C; Chronos `vram-junction-hot` is 100 °C, DEMETER 90 °C.
- `vram-junction-hot` showing **DISABLED on Aurora is correct** (Turing has no
  such sensor). Do not "fix" it.

## The failure shape this codebase keeps producing

**A control that cannot fail, or that passes *because* of the flaw.** Suspect it
before you suspect the code. Recent examples, all real:

- A per-tick RSS cap added as a *safety feature* starved the tick loop until
  every rule was mathematically unable to fire, while `/health` reported
  `armed, rules_active: 7, rules_starved: 0`. The machine then ran an hour at
  Tjmax.
- A starvation alarm that **retracted itself** — it cleared on any arriving
  sample, including a constant 0.0 °C from a wedged NVML session, and logged
  "rule is guarding again".
- A deploy that passed every check while publishing 2 thermal signals instead
  of 5.
- In one review of new liveness work, **5 of 7 mutations survived the full test
  suite green**, including a test that could not fail for the exact thing its
  docstring claimed to pin.

So: after writing a guard or a test, **break the thing it guards and confirm it
goes red.** A control you never made fail is a control nobody has checked.

## Testing

```
.venv\Scripts\python.exe -m pytest -q
```
Note this suite prints **no summary line** under `-q` — count the progress dots
and check for `F`/`E`. The venv also carries an editable `.pth` pointing at the
main tree, so a git-worktree run silently imports the code under test; set
`PYTHONPATH=<worktree>/src` to measure a real baseline.

## Deploying

`ssh <host>` is **already elevated** on all three mesh nodes (including
`ssh chronos` from Chronos). Prefer it over `Start-Process -Verb RunAs`: an
unanswered UAC prompt returns **exit 0 and writes no transcript**, so the deploy
silently does nothing. Always confirm by re-reading `/health.version`, never by
the exit code.

Full incident survey: `~/.claude/plans/thermal-safety-survey.md`.
