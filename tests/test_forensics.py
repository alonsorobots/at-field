"""Tests for :mod:`atfield.forensics`.

Focus areas:
* Background-flush correctness (samples land on disk within one interval).
* Startup rotation cascade preserves N generations and drops the oldest.
* Size-based rotation kicks in past the cap.
* Sample value coercion handles Sample objects, plain floats, ints,
  and rejects bool / unserializable types.
* Stop drains pending writes; idempotent start/stop.
"""

from __future__ import annotations

import json
import time
from pathlib import Path

import pytest

from atfield.forensics import (
    FORENSICS_FILENAME,
    FORENSICS_PREV_FILENAME,
    KEEP_ARCHIVES,
    ForensicBuffer,
    _coerce_value,
    rotate_on_startup,
)
from atfield.signals import Sample


def _read_jsonl(p: Path) -> list[dict]:
    return [json.loads(line) for line in p.read_text(encoding="utf-8").splitlines() if line.strip()]


# ---------------------------------------------------------------------------
# rotate_on_startup
# ---------------------------------------------------------------------------


class TestRotateOnStartup:
    def test_no_op_when_no_current_file(self, tmp_path):
        rotate_on_startup(tmp_path)
        assert not (tmp_path / FORENSICS_FILENAME).exists()
        assert not (tmp_path / FORENSICS_PREV_FILENAME).exists()

    def test_no_op_when_current_file_empty(self, tmp_path):
        cur = tmp_path / FORENSICS_FILENAME
        cur.write_text("", encoding="utf-8")
        rotate_on_startup(tmp_path)
        assert cur.exists()
        assert not (tmp_path / FORENSICS_PREV_FILENAME).exists()

    def test_rotates_current_to_prev(self, tmp_path):
        cur = tmp_path / FORENSICS_FILENAME
        cur.write_text('{"ts": 1.0, "samples": {"a": 1.0}}\n', encoding="utf-8")
        rotate_on_startup(tmp_path)
        assert not cur.exists()
        prev = tmp_path / FORENSICS_PREV_FILENAME
        assert prev.exists()
        assert _read_jsonl(prev) == [{"ts": 1.0, "samples": {"a": 1.0}}]

    def test_cascades_existing_archives(self, tmp_path):
        (tmp_path / FORENSICS_FILENAME).write_text("RUN3\n", encoding="utf-8")
        (tmp_path / FORENSICS_PREV_FILENAME).write_text("RUN2\n", encoding="utf-8")
        (tmp_path / "forensics-prev.1.jsonl").write_text("RUN1\n", encoding="utf-8")

        rotate_on_startup(tmp_path)

        assert not (tmp_path / FORENSICS_FILENAME).exists()
        assert (tmp_path / FORENSICS_PREV_FILENAME).read_text(encoding="utf-8") == "RUN3\n"
        assert (tmp_path / "forensics-prev.1.jsonl").read_text(encoding="utf-8") == "RUN2\n"
        assert (tmp_path / "forensics-prev.2.jsonl").read_text(encoding="utf-8") == "RUN1\n"

    def test_drops_oldest_archive_past_keep_count(self, tmp_path):
        (tmp_path / FORENSICS_FILENAME).write_text("NEW\n", encoding="utf-8")
        (tmp_path / FORENSICS_PREV_FILENAME).write_text("PREV\n", encoding="utf-8")
        for n in range(1, KEEP_ARCHIVES + 1):
            (tmp_path / f"forensics-prev.{n}.jsonl").write_text(f"OLD{n}\n", encoding="utf-8")

        rotate_on_startup(tmp_path)

        # The oldest (KEEP_ARCHIVES) should now be gone or overwritten by
        # the previous KEEP_ARCHIVES-1.
        oldest = tmp_path / f"forensics-prev.{KEEP_ARCHIVES}.jsonl"
        # It exists (cascaded into) but holds OLD{KEEP_ARCHIVES - 1}'s content.
        assert oldest.read_text(encoding="utf-8") == f"OLD{KEEP_ARCHIVES - 1}\n"


# ---------------------------------------------------------------------------
# ForensicBuffer
# ---------------------------------------------------------------------------


class TestForensicBuffer:
    def test_records_get_flushed_to_disk(self, tmp_path):
        buf = ForensicBuffer(tmp_path, flush_interval_s=0.05)
        buf.start()
        try:
            buf.record({"sig.a": 1.5, "sig.b": 2.5}, ts=100.0)
            buf.record({"sig.a": 1.6}, ts=101.0)
            time.sleep(0.2)
        finally:
            buf.stop()

        rows = _read_jsonl(tmp_path / FORENSICS_FILENAME)
        assert len(rows) == 2
        assert rows[0] == {"ts": 100.0, "samples": {"sig.a": 1.5, "sig.b": 2.5}}
        assert rows[1] == {"ts": 101.0, "samples": {"sig.a": 1.6}}

    def test_unwraps_sample_objects(self, tmp_path):
        buf = ForensicBuffer(tmp_path, flush_interval_s=0.05)
        sample = Sample(value=42.0, taken_at_ns=123, source_id="test", unit="C")
        buf.start()
        try:
            buf.record({"gpu.0.core_temp_c": sample}, ts=200.0)
            time.sleep(0.15)
        finally:
            buf.stop()

        rows = _read_jsonl(tmp_path / FORENSICS_FILENAME)
        assert rows == [{"ts": 200.0, "samples": {"gpu.0.core_temp_c": 42.0}}]

    def test_empty_record_call_is_noop(self, tmp_path):
        buf = ForensicBuffer(tmp_path, flush_interval_s=0.05)
        buf.start()
        try:
            buf.record({}, ts=300.0)
            buf.record({"non_numeric": "hi"}, ts=300.0)
            time.sleep(0.15)
        finally:
            buf.stop()

        # Either the file doesn't exist (nothing to write) or it's empty.
        path = tmp_path / FORENSICS_FILENAME
        assert not path.exists() or path.stat().st_size == 0

    def test_stop_drains_pending_writes(self, tmp_path):
        buf = ForensicBuffer(tmp_path, flush_interval_s=60.0)  # never wakes
        buf.start()
        buf.record({"sig": 1.0}, ts=400.0)
        # Stop signals the flusher and also forces a final drain.
        buf.stop()

        rows = _read_jsonl(tmp_path / FORENSICS_FILENAME)
        assert rows == [{"ts": 400.0, "samples": {"sig": 1.0}}]

    def test_start_is_idempotent(self, tmp_path):
        buf = ForensicBuffer(tmp_path, flush_interval_s=0.05)
        buf.start()
        buf.start()  # second call should not spawn a second thread
        try:
            buf.record({"sig": 1.0}, ts=500.0)
            time.sleep(0.15)
        finally:
            buf.stop()

        rows = _read_jsonl(tmp_path / FORENSICS_FILENAME)
        assert rows == [{"ts": 500.0, "samples": {"sig": 1.0}}]

    def test_size_rotation_preserves_data(self, tmp_path):
        # Tiny cap so we trigger rotation after a few records. Each record
        # is ~37 bytes; flushing in batches of >5 lines blows past 200 B,
        # so several rotations should fire over the run.
        buf = ForensicBuffer(
            tmp_path,
            flush_interval_s=0.05,
            max_bytes_before_rotate=200,
        )
        buf.start()
        try:
            for i in range(20):
                buf.record({"sig": float(i)}, ts=600.0 + i)
                time.sleep(0.02)
            time.sleep(0.2)
        finally:
            buf.stop()

        prev = tmp_path / FORENSICS_PREV_FILENAME
        # At least one rotation should have happened.
        assert prev.exists()
        # Concatenate archives oldest -> newest so the resulting list is
        # in the same order the records were appended. Note: with KEEP_ARCHIVES
        # we only retain a fixed number, so older overruns may be dropped --
        # the contract is "we retain the most-recent N rotations", not "we
        # retain everything forever".
        rows: list[dict] = []
        # Numbered archives, oldest first (KEEP_ARCHIVES is the deepest slot).
        for n in range(KEEP_ARCHIVES, 0, -1):
            p = tmp_path / f"forensics-prev.{n}.jsonl"
            if p.exists():
                rows.extend(_read_jsonl(p))
        rows.extend(_read_jsonl(prev))
        cur = tmp_path / FORENSICS_FILENAME
        if cur.exists():
            rows.extend(_read_jsonl(cur))

        # Whatever subset survived, it should be a contiguous tail of the
        # original 0..19 sequence (newest data wins). At minimum the most
        # recent record should be present.
        sigs = [r["samples"]["sig"] for r in rows]
        assert 19.0 in sigs
        # The retained values should be monotonically increasing (no
        # interleaving / shuffling between archives).
        assert sigs == sorted(sigs)

    def test_writes_survive_simulated_crash(self, tmp_path):
        """If the process disappears mid-run, prior flushes are intact.

        We simulate by starting, recording, sleeping past one flush
        interval, then *not* calling stop() -- the daemon thread will
        be cleaned up by the test runner; the file must already have
        the data.
        """
        buf = ForensicBuffer(tmp_path, flush_interval_s=0.05)
        buf.start()
        for i in range(5):
            buf.record({"sig": float(i)}, ts=700.0 + i)
        time.sleep(0.2)
        # Simulate sudden process death: do NOT call stop().
        # The flusher thread will be reaped at interpreter exit, but
        # the data should already be on disk from the periodic flush.
        rows = _read_jsonl(tmp_path / FORENSICS_FILENAME)
        assert len(rows) == 5
        # Manually clean up the daemon thread to keep the test harness
        # tidy.
        buf.stop()


# ---------------------------------------------------------------------------
# _coerce_value
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "raw,expected",
    [
        (1.5, 1.5),
        (42, 42.0),
        (Sample(value=88.5, taken_at_ns=1, source_id="t", unit="%"), 88.5),
        (True, None),
        (False, None),
        ("hello", None),
        (None, None),
        ([1, 2], None),
    ],
)
def test_coerce_value(raw, expected):
    assert _coerce_value(raw) == expected
