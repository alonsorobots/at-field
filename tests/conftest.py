"""Pytest fixtures shared across the AT-Field test suite.

Goal here is to make the vast majority of tests deterministic and clock-free
so they run identically on CI and on developer laptops. Anything that needs
real I/O (psutil, file rotation) gets a tmp_path or a fake.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import TYPE_CHECKING

import pytest

if TYPE_CHECKING:
    from atfield.signals import Sample


_NS_PER_S = 1_000_000_000


@pytest.fixture
def make_sample() -> Callable[..., "Sample"]:
    """Factory for :class:`atfield.signals.Sample` with sane defaults."""
    from atfield.signals import Sample

    def _make(
        value: float = 0.0,
        *,
        t_s: float = 0.0,
        source_id: str = "test",
        unit: str = "celsius",
    ) -> Sample:
        return Sample(
            value=value,
            taken_at_ns=int(t_s * _NS_PER_S),
            source_id=source_id,
            unit=unit,
        )

    return _make
