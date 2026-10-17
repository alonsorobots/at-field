"""Tell whoever OWNS the load: a Claude session (pipe push) or a Kiroshi worker (webhook).

2026-10-16: cpu-pkg-warm fired while one Claude session's subagents held ~7 cores; the
toast and the Kiroshi steward heard about it, the session that could act did not.

Attribution uses what the processes already carry. A Claude Code session hands its
children ``CLAUDE_CODE_MESSAGING_SOCKET`` / ``_TOKEN`` / ``_SESSION_ID``; its inbox
accepts ``{"type":"auth",...}\\n{"type":"user",...}\\n`` on that pipe. A Kiroshi worker
registers a manifest; its pool slots are its descendants, and they belong to the WORKER
even when it was launched from a session's shell and inherited that session's env -- the
steward, not the launching agent, can narrow the job.

Owners are ranked by share of the sampled load; the walk takes owners until half the load
is covered, each over an absolute floor, at most three, and STOPS at an owner it cannot
address (no fall-through: the next owner is by construction not the main cause). Only
Claude sessions get a pipe post; a worker is told through the existing webhook, whose
payload carries the ranked ``owners`` (never a socket or token).
"""
from __future__ import annotations

import json
import logging
import re
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
    """``samples``: pid -> (load on the rule's axis, env or None). Pure."""
    worker_of: dict[int, int] = {}
    for wpid in workers:
        for p in (wpid, *descendants_of(wpid)):
            worker_of.setdefault(p, wpid)
    load: dict[Any, list[float]] = {}
    proto: dict[Any, Owner] = {}
    for pid, (value, env) in samples.items():
        if pid in worker_of:                              # worker-descendant beats env
            key = ("w", worker_of[pid])
            proto.setdefault(key, worker_owner(workers[worker_of[pid]]))
        else:
            inbox = inbox_of(env)
            key = ("s", inbox.socket) if inbox else ("u",)
            proto.setdefault(key, session_owner(inbox))
        load.setdefault(key, []).append(value)
    total = sum(sum(v) for v in load.values())
    out = [replace(proto[k], share=sum(v) / total if total else 0.0, pids=len(v))
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
