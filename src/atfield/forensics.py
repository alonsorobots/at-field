"""Forensic rolling buffer: pre-crash signal history that survives hard resets.

Motivation
----------
AT-Field's per-signal sample history lives in process memory (see
``http_api.ServiceState._MultiResHistory``), which makes it useless for
diagnosing the failure modes we most need to diagnose -- the ones where
the entire OS goes down with the service. If the system hard-restarts at
04:07:51 and the service was healthy at 04:07:11, we want the 40 seconds
of pre-crash GPU power/temp/util samples that bracketed the event.

This module flushes a low-overhead, append-only JSONL stream of every
sampled signal to disk on a short cadence (default every 5 s) so the
worst-case loss window is the cadence interval. The file is rotated on
service startup (current run becomes ``forensics-prev.jsonl``) so two
generations of runs are always available for forensics, plus a small
ring of older archives via size-based rotation.

File format (one JSON object per line, no header)
-------------------------------------------------
::

    {"ts": 1778894831.245, "samples": {"gpu.0.core_temp_c": 67.0, ...}}
    {"ts": 1778894832.247, "samples": {"gpu.0.core_temp_c": 67.0, ...}}

``ts`` is a unix float; ``samples`` is the raw signal->value map for that
tick. We don't bother with per-sample ``taken_at_ns`` because all signals
in a given tick are sampled within the same ~10 ms window.

Why JSONL not Parquet/SQLite
----------------------------
Append-only JSONL is the only format that's guaranteed to be partially
readable after a hard crash. SQLite write-ahead logs and Parquet column
buffers can leave the file in a corrupt state if the kernel didn't get
a chance to fsync. JSONL appends are byte-aligned: the worst case is a
torn last line that grep skips.

Concurrency / I/O safety
------------------------
The flusher runs in a background thread so the service main loop never
blocks on disk. Append-mode writes on Windows are atomic at line
granularity for small writes (< 4 KB), and our typical tick is ~600
bytes, so even a concurrent reader (``atf tail-forensics``, or pandas
loading the file) won't see torn lines.

Sizing
------
At 1 Hz with ~14 signals at ~50 bytes each per JSON line, this writes
roughly 1.1 KB/s = 4 MB/hour = ~96 MB/day. Rotation triggers at 50 MB
(see ``MAX_BYTES_BEFORE_ROTATE``); we keep at most ``KEEP_ARCHIVES``
rotated copies. With the default settings, the on-disk footprint is
capped at ~250 MB even on a long-running install.
"""

from __future__ import annotations

import json
import logging
import threading
import time
from collections.abc import Mapping
from pathlib import Path
from typing import Final

__all__ = [
    "FORENSICS_FILENAME",
    "FORENSICS_PREV_FILENAME",
    "ForensicBuffer",
    "rotate_on_startup",
]


_log = logging.getLogger("atfield.forensics")

FORENSICS_FILENAME: Final = "forensics.jsonl"
FORENSICS_PREV_FILENAME: Final = "forensics-prev.jsonl"

# Flush every N seconds. Five gives us a worst-case 5 s loss window on
# a crash, which is short enough to bracket TDR/PSU sag patterns while
# keeping per-flush I/O cheap (one append of ~5 KB).
DEFAULT_FLUSH_INTERVAL_S: Final = 5.0

# Rotate the current file when it grows past this. 50 MB ~= 12 hours
# of 1 Hz x 14 signals; rotation keeps the file useful for pandas/grep
# without making the user manage log size.
MAX_BYTES_BEFORE_ROTATE: Final = 50 * 1024 * 1024

# Keep this many rotated archives (forensics-prev.jsonl,
# forensics-prev.2.jsonl, ...). Older files are deleted on rotation.
KEEP_ARCHIVES: Final = 3


# ---------------------------------------------------------------------------
# Startup rotation
# ---------------------------------------------------------------------------


def rotate_on_startup(state_dir: Path) -> None:
    """Rotate ``forensics.jsonl`` → ``forensics-prev.jsonl`` if non-empty.

    Called once at service start (before :class:`ForensicBuffer` opens its
    own handle). The previous run's stream is preserved so the operator
    can analyze pre-crash samples even after the service has restarted
    several times.

    Idempotent and crash-safe: if the rotation itself is interrupted, the
    next start either retries it (current file still there) or no-ops
    (already rotated). Failures are logged and swallowed -- we never want
    a forensics issue to keep the watchdog from starting.
    """
    state_dir.mkdir(parents=True, exist_ok=True)
    cur = state_dir / FORENSICS_FILENAME
    if not cur.exists() or cur.stat().st_size == 0:
        return
    try:
        # Cascade existing archives down one slot: 2->3, 1->2, prev->1.
        # Older ones are silently dropped (we cap at KEEP_ARCHIVES).
        for n in range(KEEP_ARCHIVES - 1, 0, -1):
            src = _numbered_archive(state_dir, n)
            dst = _numbered_archive(state_dir, n + 1)
            if src.exists():
                if dst.exists():
                    dst.unlink()
                src.rename(dst)
        prev = state_dir / FORENSICS_PREV_FILENAME
        if prev.exists():
            archive_1 = _numbered_archive(state_dir, 1)
            if archive_1.exists():
                archive_1.unlink()
            prev.rename(archive_1)
        cur.rename(prev)
        _log.info("forensics: rotated previous run to %s", prev.name)
    except Exception:
        _log.exception("forensics: rotation failed; continuing without rotation")


def _numbered_archive(state_dir: Path, n: int) -> Path:
    """Generate the path for an N-deep archive (forensics-prev.<N>.jsonl)."""
    return state_dir / f"forensics-prev.{n}.jsonl"


# ---------------------------------------------------------------------------
# ForensicBuffer
# ---------------------------------------------------------------------------


class ForensicBuffer:
    """Rolling on-disk record of per-tick samples.

    Threading model: ``record()`` is called from the service main loop on
    every tick and is non-blocking -- it just stages the sample in an
    in-memory list under a lock. A background flusher thread wakes every
    ``flush_interval_s`` seconds, drains the list, and writes the staged
    lines to disk in one append call.

    Crash window: the maximum data loss on a hard crash is one
    flush_interval, plus whatever's been written since the last fsync
    (which is best-effort -- Windows append-mode + ASCII text is durable
    enough for forensics purposes without explicit fsync overhead).

    Usage::

        buffer = ForensicBuffer(state_dir)
        buffer.start()
        try:
            while running:
                samples = poll_collectors()
                buffer.record(samples, ts=time.time())
        finally:
            buffer.stop()  # flushes pending then joins the thread
    """

    def __init__(
        self,
        state_dir: Path,
        *,
        flush_interval_s: float = DEFAULT_FLUSH_INTERVAL_S,
        max_bytes_before_rotate: int = MAX_BYTES_BEFORE_ROTATE,
    ) -> None:
        self._state_dir = state_dir
        self._path = state_dir / FORENSICS_FILENAME
        self._flush_interval_s = flush_interval_s
        self._max_bytes = max_bytes_before_rotate

        self._pending: list[bytes] = []
        self._pending_lock = threading.Lock()
        self._stop_event = threading.Event()
        self._flusher: threading.Thread | None = None

    # -- Public API ---------------------------------------------------------

    def start(self) -> None:
        """Spawn the background flusher thread. Idempotent."""
        if self._flusher is not None and self._flusher.is_alive():
            return
        self._state_dir.mkdir(parents=True, exist_ok=True)
        self._stop_event.clear()
        self._flusher = threading.Thread(
            target=self._run_flusher,
            name="atfield-forensics-flusher",
            daemon=True,
        )
        self._flusher.start()
        _log.info(
            "forensics: started (flush=%.1fs, rotate>%d MB, file=%s)",
            self._flush_interval_s,
            self._max_bytes // (1024 * 1024),
            self._path,
        )

    def stop(self) -> None:
        """Signal the flusher to exit, drain any pending lines, and join."""
        self._stop_event.set()
        flusher = self._flusher
        self._flusher = None
        if flusher is not None and flusher.is_alive():
            flusher.join(timeout=self._flush_interval_s + 1.0)
        # Final drain in case the flusher missed it.
        self._flush_now()

    def record(self, samples: Mapping[str, object], *, ts: float | None = None) -> None:
        """Stage one tick's sample bundle for the next flush.

        Non-blocking. The actual disk write happens up to ``flush_interval_s``
        later in the background thread. Empty sample bundles are dropped --
        no signal == nothing to forensically reconstruct.

        ``samples`` accepts the raw signal->Sample map from collectors OR
        a flat signal->value dict; values that aren't JSON-serializable
        (Sample objects) are unwrapped to their numeric ``value`` field.
        """
        if not samples:
            return
        flat: dict[str, float] = {}
        for name, raw in samples.items():
            value = _coerce_value(raw)
            if value is not None:
                flat[name] = value
        if not flat:
            return
        payload = {
            "ts": ts if ts is not None else time.time(),
            "samples": flat,
        }
        line = json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8") + b"\n"
        with self._pending_lock:
            self._pending.append(line)

    # -- Background flusher ------------------------------------------------

    def _run_flusher(self) -> None:
        """Loop body for the background thread.

        Wakes on ``flush_interval_s`` or when ``stop()`` is called,
        flushes whatever's pending, and considers rotation. Any I/O
        exception is logged and swallowed -- forensics is best-effort and
        must never crash the watchdog.
        """
        while not self._stop_event.is_set():
            # Wait up to flush_interval; returns early if stop was set.
            self._stop_event.wait(self._flush_interval_s)
            try:
                self._flush_now()
                self._maybe_rotate()
            except Exception:
                _log.exception("forensics: flusher iteration failed")

    def _flush_now(self) -> None:
        """Append the pending buffer to the on-disk JSONL file."""
        with self._pending_lock:
            if not self._pending:
                return
            batch = self._pending
            self._pending = []
        try:
            with self._path.open("ab") as fh:
                fh.write(b"".join(batch))
        except OSError as exc:
            _log.error("forensics: flush failed (%s); dropping %d staged lines", exc, len(batch))

    def _maybe_rotate(self) -> None:
        """If the current file has grown past the size cap, rotate it.

        The same cascade logic as ``rotate_on_startup`` -- archive ring
        shifted down, current becomes ``forensics-prev.jsonl``, fresh
        file starts on the next ``record()``.
        """
        try:
            size = self._path.stat().st_size
        except OSError:
            return
        if size < self._max_bytes:
            return
        try:
            for n in range(KEEP_ARCHIVES - 1, 0, -1):
                src = _numbered_archive(self._state_dir, n)
                dst = _numbered_archive(self._state_dir, n + 1)
                if src.exists():
                    if dst.exists():
                        dst.unlink()
                    src.rename(dst)
            prev = self._state_dir / FORENSICS_PREV_FILENAME
            if prev.exists():
                archive_1 = _numbered_archive(self._state_dir, 1)
                if archive_1.exists():
                    archive_1.unlink()
                prev.rename(archive_1)
            self._path.rename(prev)
            _log.info("forensics: rotated (size=%d MB > %d MB cap)",
                      size // (1024 * 1024), self._max_bytes // (1024 * 1024))
        except OSError:
            _log.exception("forensics: rotation on size cap failed")


def _coerce_value(raw: object) -> float | None:
    """Pull a numeric value out of either a Sample or a plain number.

    Returns None for anything we can't render as a JSON float. Centralizing
    this keeps the JSONL stream homogeneous so downstream tooling (pandas,
    grep, the future ``atf forensics`` CLI) doesn't have to branch.
    """
    if hasattr(raw, "value"):
        raw = raw.value  # Sample unwrap
    if isinstance(raw, bool):
        # bools are technically ints; we don't want True/False in a metric
        # stream and explicitly reject them so a bug somewhere upstream
        # isn't silently masked.
        return None
    if isinstance(raw, (int, float)):
        return float(raw)
    return None
