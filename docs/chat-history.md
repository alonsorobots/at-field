# AT-Field — Bootstrap Chat Transcript

> This is the reconstructed transcript of the conversation that produced the design captured in [`../PLANNING.md`](../PLANNING.md). It's preserved verbatim-in-spirit so that future contributors (human or AI) can trace any decision back to its reasoning.
>
> **Date:** Thursday, May 14, 2026 (last updated after the workspace move)
> **Participants:** Alonso Martinez (user, `alonsorobots`), Cursor agent (Claude Opus 4.7)
> **Outcome:** Repo bootstrapped at [`alonsorobots/at-field`](https://github.com/alonsorobots/at-field), planning doc committed, fresh agent handoff via this file.

---

## User (opening)

> So I run a bunch of AI jobs on this computer and I don't want to per job manage something that is doing hardware monitoring just to make sure I don't hurt my computer. I'm thinking we should develop something that is ALWAYS running on this computer (starts with Windows startup) that is looking at hardware temps as well as MEM (both physical and virtual) and GPU's Mem (VRAM) etc and if we reach a sustained (spike tolerant) that it will kill whatever python proc is driving this usage. Is there something built to solve this issue? I would be surprised if there aren't more people in my situation.

## Agent — initial landscape research

Searched the web and GitHub. The closest prebuilt solutions:

- **[VRAM Guard](https://github.com/Yp-pro/VRAM-Guard)** — closest match. Python + LibreHardwareMonitor, autostarts via tray, suspends GPU processes when VRAM temp crosses a threshold, panic-kills at ~105 °C. VRAM-temperature focused, NVIDIA-only, doesn't care about system RAM / pagefile.
- **[VRAM Shield](https://vramshield.com/)** — commercial sibling. Same scope.
- **[killall for Windows](https://github.com/NoCoderRandom/killall)** — CLI primitive, `killall gpu --threshold N`.
- **gpu-monitor / wsl-gpu-guard** — niche dashboards.

**Verdict:** none combine multi-signal sustained thresholds + Python-targeted kill + true Windows Service. Recommended building a custom watchdog. ~300 LoC core + NSSM service wrapper.

Asked the user to pick: build, try VRAM Guard first, or hybrid.

## User — proceed-to-build signal + hardware

> CPU - AMD Ryzen 9 9950X3D
> GPUs: 2x NVIDIA GeForce RTX 5090 32 GB GDDR7
> Memory: 128GB DDR5-5600
> OS: Windows 11
> "Look at Pose_MBPE's VMS guard and monitor. I think we are getting temp sensor data and MEM usage there"

Interpreted as: build it, use VRAM Guard as a reference for sensor APIs.

## Agent — deep-dive into VRAM Guard source

Fetched `vram_guard.py`, `core/lhm_client.py`, `core/vram_guard_core.py`, `core/process_throttler.py`. Key takeaways:

- Bundles `LibreHardwareMonitor.exe` v0.9.5, runs as subprocess, polls `http://127.0.0.1:8085/data.json`
- Walks JSON sensor tree, regex-extracts temps, prioritizes "GPU Memory Junction"
- Uses `nvidia-smi --query-compute-apps=pid,gpu_bus_id` for GPU process discovery
- `psutil` for suspend/resume/terminate; requires admin
- T1 threshold → pulse suspend cycle; T2 threshold + 10 s sustained → kill all GPU processes
- Tray app, not service; requires user session

**Gaps for this user's needs:**

| Need | VRAM Guard | Required |
|---|---|---|
| Per-GPU enumeration | ❌ first match wins | ✅ for 2× 5090 |
| GPU core temp / util | ❌ | ✅ |
| Per-process VRAM | ❌ | ✅ for targeted kill |
| System RAM % | ❌ | ✅ |
| Pagefile / commit | ❌ | ✅ |
| CPU package temp | ❌ (LHM has it, unused) | ✅ |
| Sustained-window logic | ⚠️ only T2 has 10 s timer | ✅ EMA + N-of-M |
| Targeted Python kill | ❌ kills all GPU procs | ✅ |
| True Windows Service | ❌ tray app | ✅ |
| Per-GPU policy | ❌ | ✅ |

## Agent — proposed architecture

Presented the diagram (Collectors → SignalStore → PolicyEngine → Actuator), signal set, sustained-window rule model, NSSM Windows Service deployment, audit logging.

Asked four decisions:

1. **Threshold profile** → user picked **Conservative**: VRAM-junction >90°C/20s, GPU core >83°C/30s, RAM >85%/60s, CPU >90°C/30s.
2. **Kill policy** → user picked **Graceful SIGTERM** but with critical caveat:
   > "many jobs have coordinators with self-healing workers so you need to know how to actually kill dispatcher. Not sure how you can best generalize this to all code and setups."
3. **Scope** → user picked **configure-later** (start with python-only, YAML/TOML extensible).
4. **Deploy shape** → user picked **True Windows Service via NSSM, LocalSystem at boot**.
5. **Notify** → user picked **log-only** (events.jsonl + rotating log, no toast/Discord).

## Agent — process-tree refinement

In response to the dispatcher/worker concern, generalized the kill strategy:

> When deciding to kill, find the top-most python.exe ancestor whose parent is *not* itself python or a known AI launcher (`torchrun`, `accelerate`, `deepspeed`, `mpiexec`, `ray`, `jupyter`, `ipykernel_launcher`). Terminate the entire process tree rooted there.

Made `launcher_names` configurable in `config.toml`.

## User — make it a public repo + thoroughness check

> Maybe we should make this a git repo?? other people might find it useful. Did you search for something like it in github first?

Agent had done a light search, not a thorough GitHub-native sweep. Did a proper second pass:

| Repo | Stars | Verdict |
|---|---|---|
| Yp-pro/VRAM-Guard | 1 | Single-signal, tray app |
| Thymester/System-Resource-Monitor | 3 | Alerts only, no kill |
| Youda008/HwMonitorService | 9 | Pure data plane |
| darkbrain-fc/HardwareSupervisor | 10 | C# fan curves, wrong domain |
| NoCoderRandom/killall | — | Manual CLI primitive |
| Gitorian/sysmon | — | Rust, logging only |

**Gap confirmed.** Public repo is justified.

## Agent — naming proposal

User asked for AI-themed names with Evangelion flavor (mentioned "AT-Field"). Agent proposed:

- **AT-Field** (recommended), recontextualized as **A**bsolute **T**hermal-and-memory **Field**. Eva fans get the reference, ML engineers get a self-descriptive backronym.
- Also offered MAGI, NERV, dropout, lossfloor, kill-switch.

User picked **AT-Field**.

## User — license / language / hosting

- License: **MIT**
- Language: **Python**
- Host: **alonsorobots GitHub account**

## User — searchability concern

> I guess the only tricky thing about AT-field is the searchability no? is there workarounds via hashtags?

Agent acknowledged the real problem (Google ranks Eva lore, GitHub doesn't rank short branded names for descriptive queries). Presented workarounds:

1. **GitHub Topics** (the "hashtags" — up to 20 per repo)
2. **Keyword-rich repo description** (the field below the name; Google indexes it heavily)
3. **README H1 with keywords** + first-paragraph SEO
4. **PyPI keywords**
5. **Awesome-list submissions**
6. **Launch blog post** to seed organic traffic

Cited `ruff` / `uv` as branded tools that rank for descriptive queries despite name not matching.

Offered four naming strategies. User picked **pure brand** ("AT-Field"), compensate via SEO + topics + README + blog post.

## Agent — bootstrap

Verified `C:\Users\admin\Desktop\RESEARCH` exists. Created `at-field/` subfolder. `git init -b main`. Wrote initial `LICENSE`, `.gitignore`, `pyproject.toml`.

## User — created the GitHub repo

> I created https://github.com/alonsorobots/at-field

Agent added `origin` remote. Remote had a 2-line stub README and a standard MIT LICENSE with "Alonso Martinez". Aligned local LICENSE copyright + `pyproject.toml` `authors` field to "Alonso Martinez". Merged unrelated histories cleanly. Configured local git identity (`Alonso Martinez <alonsorobots@gmail.com>`).

## User — workspace + handoff

> I guess I started this Cursor agent outside of this workspace which might make it weird. Maybe you should transfer all your planning and I can start a new agent in that repo? please create planning doc in repo and create a .md in /docs that has this chat exported
> `C:\Users\admin\Dropbox\MyWork\RESEARCH\workspaces\at-field-1.code-workspace`

Agent:
1. Fixed `at-field-1.code-workspace` to point at `Desktop/RESEARCH/at-field` (was pointing at non-existent `at-field-1`)
2. Wrote `PLANNING.md` at repo root with all decisions + architecture + todo list
3. Wrote this file (`docs/chat-history.md`) — you are reading it.
4. Committed locally (not pushed).

## User — folder reshuffle

> shoot, I moved the folder here and changed it from at-field-1 to at-field. is that an issue? I realize it had created an at-field-1 because there was a copy. I only want the original
> `C:\Users\admin\Desktop\RESEARCH\workspaces\at-field.code-workspace`

The user moved the entire `workspaces/` folder out of Dropbox and into `Desktop/RESEARCH/workspaces/`, and renamed the workspace file `at-field-1.code-workspace → at-field.code-workspace`. A stray `Desktop/RESEARCH/at-field-1/` duplicate folder had also been auto-created by Cursor (containing only the GitHub initial-commit stub, no real work). Agent:

1. Confirmed the new workspace file's `folders[].path = "../at-field"` resolves correctly to the real repo from the new location.
2. Deleted the empty `at-field-1/` duplicate folder.
3. Updated `PLANNING.md` and this transcript to reflect the new workspace path.

**Current authoritative paths:**
- Repo on disk: `C:\Users\admin\Desktop\RESEARCH\at-field`
- Cursor workspace: `C:\Users\admin\Desktop\RESEARCH\workspaces\at-field.code-workspace`
- GitHub: `https://github.com/alonsorobots/at-field`

---

## Appendix A — Locked-in decisions (quick reference)

| Field | Value |
|---|---|
| Name | AT-Field (brand) / "Absolute Thermal Field" (backronym) |
| Repo | `alonsorobots/at-field` |
| Package (PyPI) | `atfield` |
| CLI | `atf` (primary), `atfield` (alias) |
| License | MIT |
| Python | ≥ 3.10 |
| Service | NSSM, `LocalSystem`, auto-start at boot |
| State dir | `%ProgramData%\ATField\` |
| Threshold profile | Conservative (see PLANNING §8) |
| Kill mode | Graceful SIGTERM → 5 s → SIGKILL, process-tree aware |
| Killable names (initial) | `python.exe`, `pythonw.exe`, `python3.exe` |
| Launcher names (skip past) | `torchrun`, `accelerate`, `deepspeed`, `mpiexec`, `ray`, `jupyter`, `ipykernel_launcher` |
| Notifications (initial) | Log-only (`events.jsonl` + rotating log) |
| README/SEO strategy | Pure brand + topics + description SEO + blog post |

## Appendix B — Reading list (the agent consulted these)

- VRAM Guard source: <https://github.com/Yp-pro/VRAM-Guard/tree/main/core>
- LibreHardwareMonitor: <https://github.com/LibreHardwareMonitor/LibreHardwareMonitor>
- NSSM: <https://nssm.cc/>
- `nvidia-ml-py`: <https://pypi.org/project/nvidia-ml-py/>
- `psutil` process management: <https://psutil.readthedocs.io/>
- earlyoom (Linux precedent): <https://github.com/rfjakob/earlyoom>

## Appendix C — Where to start (for the next agent)

Read [`../PLANNING.md`](../PLANNING.md) §7 "Implementation order". The scaffolding is in place; the next concrete task is `src/atfield/config.py`. Conventional commits per module. Don't silently revise the Appendix A decisions — they're the result of an interactive Q&A.
