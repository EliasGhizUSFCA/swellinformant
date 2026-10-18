"""Model-run detection (app.services.sync.detect). Owned by the sync-core package.

Created by the foundation with the stub's behaviour only: sync-core replaces these tests
with the detection rules of its design (fake providers, a frozen clock).
"""

from __future__ import annotations

from collections.abc import Callable

import pytest

from app.services.sync import detect


@pytest.mark.parametrize(
    "call",
    [
        lambda: detect.load_known(None),  # type: ignore[arg-type]
        lambda: detect.plan_sync(None),
    ],
)
def test_detection_stubs_say_they_are_not_implemented(call: Callable[[], object]) -> None:
    with pytest.raises(NotImplementedError, match="not implemented yet"):
        call()
