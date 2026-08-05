"""Which PIDs has a client asked AT-Field not to kill?

Services already register themselves with AT-Field by writing a manifest to
``<state_dir>/clients/<name>/*.json`` -- the same open convention
:mod:`atfield.reporter` uses to discover event webhooks. This module reads the
*targeting* half of that convention.

AT-Field remains job-agnostic, exactly as ``reporter.py`` describes: it attaches
no meaning to any particular role string. A PID is protected when either

* the manifest sets ``"atfield_never_kill": true`` -- a service declaring its
  own supervisor processes off-limits, always honoured; or
* the manifest's ``role`` appears in ``targeting.protected_client_roles``, which
  the *operator* configures. AT-Field does not know what a "coordinator" is; it
  only matches the strings it was told to match.

Why this exists at all: protection by process name cannot work for Python
services, because the supervisor, the tray icon and the job worker are all
``python.exe``. Without a per-PID signal, the kill-root walk-up in
:mod:`atfield.actuator` climbs from a job worker into its supervisor and takes
the whole service down instead of the one job that caused the pressure.

Stale manifests are the main hazard. Manifests outlive the processes that wrote
them (there were 176 on the dev rig for a handful of live processes), so a bare
PID match would eventually protect an unrelated process that reused the number.
Every candidate is therefore checked against the live process table and its
recorded start time before it counts.
"""

from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import TYPE_CHECKING, Any, Iterable

if TYPE_CHECKING:  # pragma: no cover - typing only
    from atfield.actuator import ProcessProvider

_log = logging.getLogger("atfield.client_registry")

__all__ = ["ProtectedClient", "discover_protected"]

_MANIFEST_GLOB = "clients/*/*.json"
_NEVER_KILL_FIELD = "atfield_never_kill"

# A manifest's ``started_at`` is wall-clock at service start, while the OS
# reports process creation a moment earlier (interpreter startup sits between
# them). Anything within this window is the same process; anything outside it is
# a recycled PID.
_START_TOLERANCE_S = 120.0


class ProtectedClient:
    """A live, verified protected process."""

    __slots__ = ("pid", "role", "client", "reason")

    def __init__(self, pid: int, role: str, client: str, reason: str) -> None:
        self.pid = pid
        self.role = role
        self.client = client
        self.reason = reason

    def __repr__(self) -> str:  # pragma: no cover - diagnostics only
        return (
            f"ProtectedClient(pid={self.pid}, client={self.client!r}, "
            f"role={self.role!r}, reason={self.reason!r})"
        )


def _manifest_claims_protection(
    data: dict[str, Any], protected_roles: frozenset[str]
) -> str | None:
    """Return why this manifest is protected, or None."""
    if data.get(_NEVER_KILL_FIELD) is True:
        return f"manifest {_NEVER_KILL_FIELD}=true"
    role = data.get("role")
    if isinstance(role, str) and role.lower() in protected_roles:
        return f"role {role!r} in targeting.protected_client_roles"
    return None


def _is_same_process(info: Any, started_at: Any) -> bool:
    """Guard against PID reuse by a manifest outliving its process.

    Without a recorded start time there is nothing to compare, so the PID is
    accepted -- being slightly over-protective is the safe direction here, since
    the failure mode is "declined to kill something" rather than "killed a
    supervisor".
    """
    if not isinstance(started_at, (int, float)):
        return True
    create_time = getattr(info, "create_time", 0.0) or 0.0
    if not create_time:
        return True
    return abs(float(create_time) - float(started_at)) <= _START_TOLERANCE_S


def discover_protected(
    state_dir: Path,
    *,
    provider: ProcessProvider,
    protected_roles: Iterable[str] = (),
) -> dict[int, ProtectedClient]:
    """Map live PID -> :class:`ProtectedClient` for every protected manifest.

    Best-effort: a malformed or unreadable manifest is skipped rather than
    allowed to break the kill path. Refusing to read manifests must never turn
    into refusing to act on a genuine thermal emergency.
    """
    roles = frozenset(r.lower() for r in protected_roles)
    out: dict[int, ProtectedClient] = {}

    try:
        paths = sorted(state_dir.glob(_MANIFEST_GLOB))
    except OSError:
        _log.exception("failed to glob %s for protected clients", state_dir / _MANIFEST_GLOB)
        return out

    for path in paths:
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        if not isinstance(data, dict):
            continue

        reason = _manifest_claims_protection(data, roles)
        if reason is None:
            continue

        pid = data.get("pid")
        if not isinstance(pid, int):
            continue

        info = provider.get(pid)
        if info is None:
            continue  # manifest outlived its process
        if not _is_same_process(info, data.get("started_at")):
            continue  # PID was recycled

        out[pid] = ProtectedClient(
            pid=pid,
            role=str(data.get("role") or "?"),
            client=str(data.get("name") or path.parent.name),
            reason=reason,
        )

    return out
