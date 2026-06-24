"""AT-Field collector protocol: capability detection, health, sampling.

Every concrete sensor source (NVML, nvidia-smi, psutil, LibreHardwareMonitor,
future AMD/Intel adapters, user plugins) implements the :class:`Collector`
protocol defined here. The service treats them all identically:

1. **At startup**, call :meth:`Collector.probe` exactly once. It returns a
   :class:`ProbeResult` describing whether the source is usable on this box
   and which signal names it will provide. The service builds a working
   signal map from the union of all probes and disables any rule that
   references an unmapped signal — with a clear startup log line, never
   silently. This is what makes goal #2 ("works on most setups") and
   goal #3 ("zero config") deliverable: the user does not need to edit
   their config when their hardware lacks a sensor.

2. **Every tick**, call :meth:`Collector.sample`. Returns
   ``{signal_name: Sample}``. A collector that has nothing to report (e.g.
   the per-process VRAM reader between scrape intervals) returns an empty
   dict, *not* stale samples. The signals layer treats absent samples as
   "abstain", per :pyfile:`atfield/signals.py`.

3. **Health is observable**, not inferred. :meth:`Collector.health` reports
   the current :class:`HealthState`. Transient failures (one bad subprocess
   call, one HTTP timeout) leave the collector HEALTHY; consecutive failures
   trip it to DEGRADED (rules using its signals begin abstaining via stale
   eviction); a hard error (driver gone, sidecar crashed) trips FAILED and
   the service stops calling ``sample()`` on it.

The protocol is small on purpose. Collectors do *not* know about rules,
thresholds, or kills; they only produce timestamped samples. Policy lives
in :pyfile:`atfield/policy.py`.

A note on extensibility
-----------------------
The :class:`Collector` protocol is a public API surface from v0.1.0
onward — third-party packages may register collectors via setuptools
entry points (the ``atfield.collectors`` group) or by dropping a module
under ``%PROGRAMDATA%\\ATField\\plugins\\``. This is how AMD GPU support,
Intel Arc support, third-party shared-memory adapters, etc. ship without
forking core. Breaking changes here are SemVer-major.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Protocol, runtime_checkable

from atfield.signals import Sample

__all__ = [
    "Collector",
    "CollectorError",
    "HealthState",
    "ProbeResult",
]


# ---------------------------------------------------------------------------
# Health
# ---------------------------------------------------------------------------


class HealthState(Enum):
    """Operational state of a collector, reported by :meth:`Collector.health`.

    The state machine is owned by the collector itself (it knows its own
    failure modes); the service merely observes via :meth:`Collector.health`
    and decides whether to keep calling ``sample()``.
    """

    HEALTHY = "healthy"     #: last sample(s) succeeded; service polls normally
    DEGRADED = "degraded"   #: transient trouble; service still polls but rules abstain on stale data
    FAILED = "failed"       #: unrecoverable; service stops calling sample() until restart
    UNPROBED = "unprobed"   #: probe() has not yet been called

    @property
    def is_pollable(self) -> bool:
        """Should the service call :meth:`Collector.sample` in this state?"""
        return self in (HealthState.HEALTHY, HealthState.DEGRADED)


# ---------------------------------------------------------------------------
# Probe result
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class ProbeResult:
    """Outcome of :meth:`Collector.probe`, called once at service startup.

    Attributes
    ----------
    available :
        ``True`` if the collector successfully connected to its source and
        is ready to sample. ``False`` means the source is missing or
        unreachable on this box (no driver, no daemon, no permissions).
        A ``False`` result is normal and expected — it's what enables
        capability negotiation rather than hard-failing on missing hardware.
    reason :
        Human-readable explanation. For ``available=True``, a short summary
        like ``"NVML 12.535, 2 GPUs detected"``. For ``available=False``,
        the operator-actionable cause: ``"NVML library not found; install
        NVIDIA driver >= 535"`` or ``"LibreHardwareMonitor HTTP daemon not
        responding on 127.0.0.1:8085"``. This string ends up in the startup
        log and in ``atf status`` output, so write it for a sleep-deprived
        engineer at 2 AM.
    signals :
        Signal names this collector will provide on subsequent
        :meth:`Collector.sample` calls (e.g.
        ``("gpu.0.core_temp_c", "gpu.0.vram_used_percent", ...)``). The
        service unions these across all collectors to form the working
        signal map and disables rules whose signals are not in it.
        Empty when ``available=False``.
    metadata :
        Free-form dict for collector-specific debug info (driver version,
        sensor names matched, fallback path taken). Logged at startup, not
        consumed by the service.
    """

    available: bool
    reason: str
    signals: tuple[str, ...] = ()
    metadata: dict[str, str] = field(default_factory=dict)


# ---------------------------------------------------------------------------
# Errors
# ---------------------------------------------------------------------------


class CollectorError(RuntimeError):
    """Raised by a collector when an operation fails in a recoverable way.

    Concrete collectors should raise this from ``sample()`` for transient
    issues (one HTTP timeout, one nvidia-smi stall) so the service can
    increment the failure counter without crashing the watchdog. Hard
    failures (driver unloaded, library uninstalled mid-run) should
    transition :meth:`Collector.health` to FAILED rather than raise.
    """


# ---------------------------------------------------------------------------
# Protocol
# ---------------------------------------------------------------------------


@runtime_checkable
class Collector(Protocol):
    """Contract every sensor source implements.

    Implementations are not required to subclass anything; matching the
    structural shape is sufficient. ``@runtime_checkable`` is provided for
    convenience in tests but the service does not isinstance-check at the
    hot path — it trusts the registry at startup.
    """

    #: Stable, lowercase identifier, written into every :class:`Sample` this
    #: collector emits. Used for audit-log attribution and per-source health
    #: tracking. Must be unique across loaded collectors. Examples: ``"nvml"``,
    #: ``"nvidia_smi"``, ``"psutil"``, ``"lhm"``, ``"my_amd_adlx"``.
    name: str

    def probe(self) -> ProbeResult:
        """Detect whether this source is usable; declare provided signals.

        Called exactly once at service startup, before any ``sample()``
        call. Must not raise — return ``ProbeResult(available=False,
        reason=...)`` for any failure mode. The service writes the result
        to the startup log verbatim.
        """
        ...

    def sample(self) -> dict[str, Sample]:
        """Read the source once; return ``{signal_name: Sample}``.

        Called every service tick (default 1 Hz, see ``general.tick_hz``)
        for collectors whose :meth:`health` is :attr:`HealthState.is_pollable`.
        Should be fast (< 100 ms typical, hard-timeout per implementation).
        Returning an empty dict is valid and means "I have nothing new to
        report this tick"; do *not* return stale samples, the signals layer
        treats absence as abstain.
        """
        ...

    def health(self) -> HealthState:
        """Current operational state. Cheap to call; do not perform I/O here."""
        ...

    def shutdown(self) -> None:
        """Release resources (subprocesses, HTTP sessions, NVML handles).

        Called once on graceful service stop. Must be idempotent.
        """
        ...
