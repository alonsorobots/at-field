# Awesome-list submissions

Templates for the three lists where AT-Field is a clean fit. Send each
PR as a single-line addition to the relevant section of the target
README; copy the suggested entry verbatim, then propose the PR.

## awesome-windows

**Repo:** https://github.com/Awesome-Windows/Awesome
**Section:** *Utilities → System*

Suggested entry:

```markdown
- [AT-Field](https://github.com/alonsorobots/at-field) - Always-on hardware watchdog for AI workloads on Windows. Monitors NVIDIA GPU/VRAM temps, system RAM, page-file pressure, and CPU temperature; kills runaway Python/PyTorch processes before they damage hardware. Single-installer Tauri tray app + LocalSystem service. MIT.
```

## awesome-python

**Repo:** https://github.com/vinta/awesome-python
**Section:** *Sysadmin* (or *Hardware* if the maintainers prefer)

Suggested entry:

```markdown
- [AT-Field](https://github.com/alonsorobots/at-field) - Hardware watchdog daemon for AI rigs on Windows. NVML + LibreHardwareMonitor under the hood, N-of-M sliding-window rule engine, process-tree-aware killer, localhost HTTP API + Prometheus exporter, Tauri-based tray dashboard.
```

## awesome-machine-learning

**Repo:** https://github.com/josephmisiti/awesome-machine-learning
**Section:** *Tools / Infrastructure* (Python sub-section)

Suggested entry:

```markdown
- [AT-Field](https://github.com/alonsorobots/at-field) - Watchdog for ML training runs that prevents OOM crashes and thermal damage. Monitors VRAM (junction temp included), system RAM, swap, and CPU temps; kills the offending Python process tree before the rig dies. Windows-first; Linux on the roadmap.
```

## Submission checklist

For each PR:

1. Fork the target repo, branch off `main`.
2. Add the entry verbatim, preserving the surrounding section's
   alphabetical / category order.
3. PR title: `Add AT-Field`.
4. PR body: a 2-3 sentence pitch, link to the project README, link
   to the latest GitHub Release for proof of life.
5. Link the issue/PR back here in `docs/planning/release-checklist.md`
   so we can chase up unmerged ones.

A note on timing: ship the v0.2.0 release first. Awesome-list
maintainers reasonably reject submissions that are "0.1.0-alpha"
without a public release artifact attached.
