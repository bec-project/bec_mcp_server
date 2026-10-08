import json

import numpy as np
from bec_lib.messages import BECStatus

from bec_mcp_server.bec.digest import (
    age_s,
    first_line,
    iso_time,
    jsonable,
    short_service_name,
    signal_digest,
)


def test_first_line_handles_missing_and_multiline_text():
    assert first_line(None) == ""
    assert first_line("   ") == ""
    assert first_line("hello\nworld") == "hello"


def test_jsonable_handles_numpy_scalars_enums_and_small_arrays():
    assert jsonable(np.float64(1.5)) == 1.5
    assert isinstance(jsonable(np.int32(7)), int)
    assert jsonable(np.array([1, 2, 3])) == [1, 2, 3]
    assert jsonable(BECStatus.RUNNING) == 2


def test_jsonable_summarizes_large_arrays():
    out = jsonable(np.arange(1000, dtype=float))
    assert out == {"shape": [1000], "dtype": "float64", "min": 0.0, "max": 999.0, "mean": 499.5}


def test_jsonable_handles_sets_and_long_sequences():
    assert jsonable({"b", "a"}) == ["a", "b"]
    out = jsonable(list(range(100)))
    assert out["items"] == list(range(16))
    assert "100 items" in out["summary"]


def test_iso_time_and_age():
    assert iso_time(None) is None
    assert iso_time(1700000000.0).startswith("2023-11-14T")
    assert age_s(None) is None
    assert age_s(100.0, now=112.34) == 12.3
    # clock skew between hosts must not produce a negative age
    assert age_s(200.0, now=100.0) == 0.0
    assert age_s("not a time") is None


def test_short_service_name_trims_instance_uuid():
    assert short_service_name("DeviceServer") == "DeviceServer"
    assert (
        short_service_name("BECIPythonClient/4b9d1af8-44ed-4f3a-8787-ef9f958f59b")
        == "BECIPythonClient/4b9d1af8"
    )


def test_signal_digest_honors_max_points_beyond_jsonable_cap():
    assert signal_digest(np.arange(100.0), max_points=100) == list(map(float, range(100)))
    digest = signal_digest(np.arange(100.0), max_points=16)
    assert digest["min"] == 0.0 and digest["max"] == 99.0
    assert digest["first"] == [0.0, 1.0, 2.0] and digest["last"] == [97.0, 98.0, 99.0]


def test_signal_digest_and_jsonable_never_emit_non_finite_floats():
    values = np.array([1.0, float("nan"), float("inf"), 4.0] * 10)
    digest = signal_digest(values, max_points=16)
    assert digest["min"] == 1.0 and digest["max"] == 4.0
    assert digest["non_finite_count"] == 20
    json.dumps(digest, allow_nan=False)

    inline = signal_digest(np.array([1.0, float("nan")]), max_points=4)
    assert inline == [1.0, "nan"]
    json.dumps(jsonable(np.full(100, np.nan)), allow_nan=False)
