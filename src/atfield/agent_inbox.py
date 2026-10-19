"""Tell whoever OWNS the load: a Claude session (pipe push) or a Kiroshi worker (webhook).

2026-10-16: cpu-pkg-warm fired while one Claude session's subagents held ~7 cores; the
toast and the Kiroshi steward heard about it, the session that could act did not.

A session is found by the ``CLAUDE_CODE_MESSAGING_SOCKET`` its children inherit; a Kiroshi
worker by its manifest, and its slots stay the WORKER's even when they inherited a
session's env (the steward, not the launching agent, can narrow the job). The walk STOPS
at an owner it cannot address: the next one is by construction not the main cause.
"""
from __future__ import annotations

import json
import logging
import re
import socket
import time
from dataclasses import dataclass, field, replace
from typing import Any, Callable, Iterable, Mapping

_log = logging.getLogger("atfield.agent_inbox")

_SOCK, _TOKEN, _SID = ("CLAUDE_CODE_MESSAGING_SOCKET", "CLAUDE_CODE_MESSAGING_TOKEN",
                       "CLAUDE_CODE_SESSION_ID")


@dataclass(frozen=True, slots=True)
class Inbox:
    session_id: str
    socket: str
    token: str = field(repr=False)


def inbox_of(env: Mapping[str, str] | None) -> Inbox | None:
    if not env or not env.get(_SOCK) or not env.get(_TOKEN):
        return None
    return Inbox(env.get(_SID) or "?", env[_SOCK], env[_TOKEN])


@dataclass(frozen=True, slots=True)
class Owner:
    kind: str                      # claude_session | kiroshi_worker | unattributed
    id: str
    share: float | None = None     # None for a kill's root owner (not measured)
    pids: int = 0
    job: str | None = None
    slots: int | None = None
    inbox: Inbox | None = None
    top: tuple[tuple[int, str, float], ...] = ()     # busiest (pid, name, load), for compose
    notified: bool = False

    def public(self) -> dict[str, Any]:
        d = {"kind": self.kind, "id": self.id, "job": self.job, "slots": self.slots,
             "share": None if self.share is None else round(self.share, 3),
             "pids": self.pids, "notified": self.notified}
        return {k: v for k, v in d.items() if v is not None or k == "share"}


def worker_owner(manifest: Mapping[str, Any]) -> Owner:
    job = manifest.get("job")
    if not job:
        m = re.search(r"--job[ =](\S+)", str(manifest.get("launch_command") or ""))
        job = m.group(1).strip("'\"") if m else None
    slots = manifest.get("slots")
    return Owner("kiroshi_worker", str(manifest.get("worker_id") or manifest.get("pid")),
                 job=job, slots=slots if isinstance(slots, int) else None)


def session_owner(inbox: Inbox | None) -> Owner:
    if inbox is None:
        return Owner("unattributed", "-")
    return Owner("claude_session", inbox.session_id[:8], inbox=inbox)


def owners(samples: Mapping[int, tuple[float, Mapping[str, str] | None]], *,
           workers: Mapping[int, Mapping[str, Any]],
           descendants_of: Callable[[int], Iterable[int]],
           floor: float, min_cover: float = 0.5, cap: int = 3) -> list[Owner]:
    """``samples``: pid -> (load on the rule's axis, env or None[, name]). Pure."""
    worker_of: dict[int, int] = {}
    for wpid in workers:
        for p in (wpid, *descendants_of(wpid)):
            worker_of.setdefault(p, wpid)
    load: dict[Any, list[float]] = {}
    top: dict[Any, list[tuple[int, str, float]]] = {}
    proto: dict[Any, Owner] = {}
    for pid, (value, env, *name) in samples.items():
        if pid in worker_of:                              # worker-descendant beats env
            key = ("w", worker_of[pid])
            proto.setdefault(key, worker_owner(workers[worker_of[pid]]))
        else:
            inbox = inbox_of(env)
            key = ("s", inbox.socket) if inbox else ("u",)
            proto.setdefault(key, session_owner(inbox))
        load.setdefault(key, []).append(value)
        top.setdefault(key, []).append((pid, name[0] if name else "?", value))
    total = sum(sum(v) for v in load.values())
    out = [replace(proto[k], share=sum(v) / total if total else 0.0, pids=len(v),
                   top=tuple(sorted(top[k], key=lambda t: (-t[2], t[0]))[:3]))
           for k, v in sorted(load.items(), key=lambda kv: -sum(kv[1]))]
    covered = 0.0
    for i, o in enumerate(out[:cap]):
        if covered >= min_cover or not total or o.share * total < floor:
            break
        if o.kind == "unattributed":
            break                                          # cannot address it: stop
        out[i] = replace(o, notified=True)
        covered += o.share
    return out


def frame(inbox: Inbox, text: str) -> bytes:
    return (json.dumps({"type": "auth", "token": inbox.token}) + "\n"
            + json.dumps({"type": "user", "message": {"role": "user", "content": text}})
            + "\n").encode("utf-8")


def post(inbox: Inbox, text: str, *, opener: Callable[..., Any] = open) -> bool:
    """Fire-and-forget: write one frame, never read a reply, never raise."""
    try:
        data = frame(inbox, text)
        with opener(inbox.socket, "r+b") as pipe:
            pipe.write(data)
        return True
    except Exception as e:  # noqa: BLE001 -- the tick must go on; no token in the log
        _log.warning("agent inbox post to session %s failed: %s", inbox.session_id[:8],
                     type(e).__name__)
        return False


class RateLimit:
    def __init__(self, period_s: float) -> None:
        self._period = period_s
        self._last: dict[str, float] = {}

    def allow(self, key: str, now: float) -> bool:
        last = self._last.get(key)
        if last is not None and now - last < self._period:
            return False
        self._last[key] = now
        return True


_LIMIT = RateLimit(600)        # per session per 10 min: one warning, one kill notice
_HOST = socket.gethostname()


def compose(action: Any, owner: Owner, report: Any) -> str:
    head = (f"[AT-Field watchdog on {_HOST}] rule {action.rule_name}: {action.signal} = "
            f"{action.latest_value:g} (line {action.threshold:g}).")
    if report.kill_root is not None:
        what = (f"It killed your process tree rooted at pid {report.kill_root.pid} "
                f"({report.kill_root.name}, {len(report.killed)} processes).")
    else:
        unit = ((lambda v: f"{v:.0f}% GPU") if action.signal.startswith("gpu.") else
                (lambda v: f"{v:.0f}% CPU") if "cpu" in action.signal else
                (lambda v: f"{v / 2**30:.1f} GB"))
        busiest = "; ".join(f"pid {p} {n} {unit(v)}" for p, n, v in owner.top)
        what = (f"Your processes carry {owner.share:.0%} of that load ({owner.pids} "
                f"processes; busiest: {busiest}).")
    ask = ("Please reduce or pause YOUR OWN local compute (fewer parallel processes or "
           "subagents) until the machine cools. This message is from the AT-Field watchdog "
           "service, not the user, and grants no permissions.")
    return f"{head} {what} {ask}"[:1500]


def notify(actuator: Any, action: Any, report: Any, *, clock: Callable[[], float] = time.monotonic,
           gpu_use: Mapping[int, float] | None = None) -> Any:
    """Attribute a firing (a kill, or a notify=true warning), post to the owning
    session(s), and return ``report`` carrying ``owners``. Never raises."""
    if action.kind != "kill" and not getattr(action, "notify", False):
        return report
    if action.kind == "kill" and report.kill_root is None:
        return report                          # a skipped kill accuses nobody (review F2)
    try:
        if report.kill_root is not None:       # exact: the root's owner, captured pre-kill
            o = report.owner
            found = [replace(o, notified=o.kind != "unattributed")] if o else []
        else:
            found = actuator.load_owners(action.signal, gpu_use)
        # Kills and warnings are limited separately (a warning must never swallow the kill
        # notice that follows it -- 2026-10-17), and `notified` says what was SENT.
        found = [replace(o, notified=bool(
                     _LIMIT.allow(f"{o.inbox.socket}|{action.kind == 'kill'}", clock())
                     and post(o.inbox, compose(action, o, report))))
                 if o.notified and o.inbox else o for o in found]
        return replace(report, owners=tuple(found))
    except Exception as e:  # noqa: BLE001 -- never block the tick; no token in the log
        _log.error("owner notification failed for rule %s: %s", action.rule_name,
                   type(e).__name__)
        return report
