"""Snooze / mute for AT-Field -> agent notifications (0.4.27).

Every notice AT-Field sends an agent (a warning, a kill notice, the cap warning) can be
silenced by the agent it is addressed to:

* **snooze** -- for N minutes, scoped to one owner (a Claude session id, or a Kiroshi job)
  and optionally one rule;
* **mute** -- for a Kiroshi JOB until the job ends (no live worker on this host has carried
  it for ``MUTE_JOB_GONE_S``) or until unmuted, optionally one rule.

Silencing changes WHO IS TOLD, never WHAT IS DONE: a kill still happens, and the suppressed
notice is still written to events.jsonl and the webhook with ``suppressed`` saying by what,
until when and by whom. Persisted to ``<state_dir>/notify_prefs.json`` so it survives a
restart. The HTTP thread writes and the tick thread reads, hence the lock.
"""
from __future__ import annotations

import json
import logging
import os
import sys
import threading
import time
from pathlib import Path
from typing import Any, Iterable

_log = logging.getLogger("atfield.notify_prefs")

FILENAME = "notify_prefs.json"
MUTE_JOB_GONE_S = 3600.0          # a muted job with no live worker on this host this long has ended
MAX_SNOOZE_MIN = 7 * 24 * 60
SESSION_ID_MIN = 8                # Owner.id is the session id's first 8 chars


def _now() -> float:              # module-level so tests can drive the clock
    return time.time()


def atf_command() -> str:
    """The exact CLI to type on this host: the frozen bundle's atf.exe (not on PATH)."""
    if getattr(sys, "frozen", False):
        return '"' + str(Path(sys.executable).with_name("atf.exe")) + '"'
    return "atf"


def footer(owner: Any, rule: str, host: str) -> str:
    """One line: the exact command that silences THIS owner for THIS rule."""
    atf = atf_command()
    if owner.kind == "kiroshi_worker" and owner.job:
        scope = f"--job {owner.job}"
        return (f"To silence (on {host}): {atf} snooze {scope} --rule {rule} --minutes 60 "
                f"(or 15 / 240; drop --rule for every rule), or {atf} mute {scope} --rule {rule} "
                f"until the job ends.")
    if owner.kind != "claude_session":
        return f"To silence (on {host}): this owner has no job name, so only by its session."
    return (f"To silence (on {host}): {atf} snooze --session {owner.id} --rule {rule} "
            f"--minutes 60 (or 15 / 240; drop --rule for every rule).")


class NotifyPrefs:
    def __init__(self, path: Path) -> None:
        self._path = Path(path)
        self._lock = threading.Lock()
        self._prefs: list[dict[str, Any]] = []
        try:
            raw = json.loads(self._path.read_text(encoding="utf-8"))
            self._prefs = [p for p in raw.get("prefs", []) if isinstance(p, dict)]
        except FileNotFoundError:
            pass
        except Exception as e:  # noqa: BLE001 -- a corrupt file must not stop the service
            _log.error("notify prefs unreadable (%s); starting empty", e)

    # -- writes (HTTP thread) ---------------------------------------------

    def snooze(self, *, session: str | None = None, job: str | None = None, minutes: float,
               rule: str | None = None, by: str | None = None) -> dict[str, Any]:
        scope, target = _scope(session, job)
        if not isinstance(minutes, (int, float)) or isinstance(minutes, bool) \
                or not 0 < minutes <= MAX_SNOOZE_MIN:
            raise ValueError(f"minutes must be a number in (0, {MAX_SNOOZE_MIN}]")
        now = _now()
        return self._put({"kind": "snooze", "scope": scope, "target": target,
                          "rule": _rule(rule), "until": now + 60.0 * minutes,
                          "set_by": by or "?", "set_at": now})

    def mute(self, *, job: str | None, rule: str | None = None,
             by: str | None = None) -> dict[str, Any]:
        if not isinstance(job, str) or not job.strip():
            raise ValueError("mute takes a job (a Kiroshi job name); snooze a session instead")
        now = _now()
        return self._put({"kind": "mute", "scope": "job", "target": job.strip(),
                          "rule": _rule(rule), "until": None, "set_by": by or "?",
                          "set_at": now, "job_seen_at": now})

    def unmute(self, *, session: str | None = None, job: str | None = None,
               rule: str | None = None) -> int:
        """Remove snoozes AND mutes for the owner (one rule, or all rules if rule is None)."""
        scope, target = _scope(session, job)
        with self._lock:
            keep = [p for p in self._prefs
                    if not (p["scope"] == scope and _same(scope, p["target"], target)
                            and (rule is None or p["rule"] == rule))]
            n = len(self._prefs) - len(keep)
            self._prefs = keep
            self._save()
        return n

    # -- reads (tick thread) ------------------------------------------------

    def match(self, owner: Any, rule: str) -> dict[str, Any] | None:
        """The pref silencing ``owner`` for ``rule`` right now, or None."""
        if owner.kind == "kiroshi_worker" and owner.job:
            scope, target = "job", owner.job
        elif owner.kind == "claude_session":
            scope, target = "session", owner.id
        else:
            return None
        with self._lock:
            self._prune_locked()
            for p in self._prefs:
                if (p["scope"] == scope and _same(scope, p["target"], target)
                        and p["rule"] in (None, rule)):
                    return dict(p)
        return None

    def observe_jobs(self, live_jobs: Iterable[str]) -> None:
        """Refresh each job mute's last-seen time from the live worker manifests."""
        live = set(live_jobs)
        now = _now()
        with self._lock:
            changed = False
            for p in self._prefs:
                if p["kind"] == "mute" and p["target"] in live:
                    p["job_seen_at"] = now
                    changed = True
            self._prune_locked(force_save=changed)

    def snapshot(self) -> list[dict[str, Any]]:
        with self._lock:
            self._prune_locked()
            return [dict(p) for p in self._prefs]

    # -- internals ----------------------------------------------------------

    def _put(self, pref: dict[str, Any]) -> dict[str, Any]:
        with self._lock:          # one entry per (kind, scope, target, rule): a re-snooze extends
            self._prefs = [p for p in self._prefs
                           if (p["kind"], p["scope"], p["target"], p["rule"])
                           != (pref["kind"], pref["scope"], pref["target"], pref["rule"])]
            self._prefs.append(pref)
            self._save()
        return dict(pref)

    def _prune_locked(self, *, force_save: bool = False) -> None:
        now = _now()
        keep = [p for p in self._prefs
                if (p["kind"] == "snooze" and p["until"] > now)
                or (p["kind"] == "mute" and now - p.get("job_seen_at", now) < MUTE_JOB_GONE_S)]
        if force_save or len(keep) != len(self._prefs):
            self._prefs = keep
            self._save()

    def _save(self) -> None:
        try:
            self._path.parent.mkdir(parents=True, exist_ok=True)
            tmp = self._path.with_suffix(".tmp")
            tmp.write_text(json.dumps({"prefs": self._prefs}, indent=1), encoding="utf-8")
            os.replace(tmp, self._path)
        except Exception as e:  # noqa: BLE001 -- in memory it still holds until restart
            _log.error("notify prefs not persisted: %s", e)


def _scope(session: str | None, job: str | None) -> tuple[str, str]:
    if bool(session) == bool(job):
        raise ValueError("give exactly one of session or job")
    if session:
        s = str(session).strip()
        if len(s) < SESSION_ID_MIN:
            raise ValueError(f"session id needs at least its first {SESSION_ID_MIN} characters")
        return "session", s
    return "job", str(job).strip()


def _rule(rule: str | None) -> str | None:
    if rule is None or rule == "":
        return None
    if not isinstance(rule, str):
        raise ValueError("rule must be a string")
    return rule.strip()


def _same(scope: str, a: str, b: str) -> bool:
    # A session matches on its first 8 characters (the id every notice prints).
    return a[:SESSION_ID_MIN] == b[:SESSION_ID_MIN] if scope == "session" else a == b
