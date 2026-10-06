"""JSON that a browser can actually parse.

Python's `json.dumps` happily writes `NaN` and `Infinity`, which are NOT valid
JSON: the browser's `JSON.parse` throws and the whole panel renders blank. These
are not exotic values either — `Report.ruin_exposure()` returns `inf` whenever a
run never dipped below zero, and a single bad candle anywhere in a series turns
every downstream statistic into `NaN`.

Practical consequence: every response from the platform backend goes through
`json_safe`, because one unserializable float turns a 200 into a blank screen
with no error in the network tab.

Extracted from the old single-page console so that deleting it does not take the
platform's entire JSON layer with it. Stdlib-only on purpose: it is imported at
module load by the backend and must not drag numpy in.
"""
from __future__ import annotations

import math


def finite(value: float) -> float | None:
    """`inf` / `nan` -> None. The UI renders None as "-".

    Useful where a single value is handed to the UI on its own, rather than as
    part of a structure `json_safe` will walk.
    """
    return value if math.isfinite(value) else None


def json_safe(value):
    """Replace non-finite floats with None, recursively; coerce numpy scalars."""
    if isinstance(value, float):
        # np.float64 IS a float subclass, so numpy's nan/inf land here too.
        return value if math.isfinite(value) else None
    if isinstance(value, dict):
        return {key: json_safe(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [json_safe(item) for item in value]
    if type(value).__module__ == "numpy":
        # np.int64 and np.bool_ are NOT int/bool subclasses — json.dumps raises
        # "Object of type int64 is not JSON serializable" and the endpoint
        # returns 500. Checked without importing numpy to keep this stdlib-only.
        return json_safe(value.item() if getattr(value, "ndim", 0) == 0 else value.tolist())
    return value
