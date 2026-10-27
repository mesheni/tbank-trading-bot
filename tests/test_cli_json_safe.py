"""train_summary.json: NaN/inf не должны попадать в файл (RFC 8259)."""
from __future__ import annotations

import json

from cli import _json_safe


def test_json_safe_replaces_non_finite_floats() -> None:
    payload = {"a": float("nan"), "b": [float("inf"), 1.0]}
    encoded = json.dumps(_json_safe(payload), allow_nan=False)
    assert json.loads(encoded) == {"a": None, "b": [None, 1.0]}
