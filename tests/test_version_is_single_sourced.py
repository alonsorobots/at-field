"""``__version__`` and pyproject must agree.

They are two copies of one fact, and they drifted: pyproject said 0.4.8 while
``atfield.__version__`` still said 0.4.7, so a freshly built bundle reported the
OLD version. That matters beyond cosmetics -- the mesh and the tray use the
version string to detect skew, so a build that lies about its version makes a
node look synced when it is not, which is the failure mode version reporting
exists to prevent.

Cheap to assert, impossible to notice by hand.
"""
import pathlib
import re

import atfield

_PYPROJECT = pathlib.Path(__file__).resolve().parents[1] / "pyproject.toml"


def test_dunder_version_matches_pyproject():
    text = _PYPROJECT.read_text(encoding="utf-8")
    m = re.search(r'(?m)^version = "([^"]+)"', text)
    assert m, "no version found in pyproject.toml"
    assert atfield.__version__ == m.group(1), (
        f"atfield.__version__ is {atfield.__version__!r} but pyproject says "
        f"{m.group(1)!r} -- a build from this tree would report the wrong version"
    )
