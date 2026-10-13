"""The traceback parser on a real BEC traceback and on the ways text gets mangled on its way."""

from __future__ import annotations

import json

import pytest
from log_fakes import SCAN_ABORTION_TRACEBACK

from bec_mcp_server.tools.tracebacks import (
    INNER_FRAMES,
    MAX_FRAMES,
    OUTER_FRAMES,
    locate,
    parse_traceback,
    parse_tracebacks,
    remove_traceback,
)

SIMPLE = """Traceback (most recent call last):
  File "/opt/bec/bec_lib/bec_lib/devicemanager.py", line 42, in load
    self._load(config)
  File "/usr/lib/python3.12/json/__init__.py", line 346, in loads
    return _default_decoder.decode(s)
json.decoder.JSONDecodeError: Expecting value: line 1 column 1 (char 0)"""


def test_a_real_chained_bec_traceback():
    tb = parse_traceback(SCAN_ABORTION_TRACEBACK)

    assert [e.type for e in tb.chain] == [
        "bec_server.scan_server.errors.DeviceInstructionError",
        "bec_server.scan_server.errors.ScanAbortion",
    ]
    assert tb.links == ("cause",)
    assert tb.root.message.splitlines() == [
        "An error occurred during 'set' on device 'samx'.",
        "LimitError: position=50 not within limits (-5, 5)",
    ]
    # raised without a message, so Python printed only the class name
    assert tb.raised.message == "" and tb.raised.text() == "ScanAbortion"
    assert [f.function for f in tb.root.frames][-2:] == ["wait", "_raise_if_failed"]
    # the 3.11+ ~~~^^^ markers are not mistaken for source lines
    assert tb.root.frames[0].code == "method()"


def test_origin_is_the_innermost_bec_frame_of_the_first_exception():
    origin = parse_traceback(SCAN_ABORTION_TRACEBACK).origin()

    assert (origin.package, origin.path, origin.line, origin.function) == (
        "bec_server",
        "bec_server/scan_server/scans/scan_status.py",
        183,
        "_raise_if_failed",
    )
    assert origin.code == "raise obj.exception()"


def test_origin_skips_library_frames():
    origin = parse_traceback(SIMPLE).origin()
    assert origin.path == "bec_lib/devicemanager.py"


def test_text_before_the_traceback_is_its_prefix():
    tb = parse_traceback(f"Failed to load the config: {SIMPLE}")
    assert tb.prefix == "Failed to load the config:"
    assert remove_traceback(f"Failed to load the config: {SIMPLE}") == tb.prefix
    assert remove_traceback("no traceback here") == "no traceback here"


def test_text_without_a_traceback_gives_none():
    assert parse_traceback("Scan 12 finished") is None
    assert parse_traceback("") is None


def test_context_chains_are_told_apart_from_causes():
    text = SIMPLE + (
        "\n\nDuring handling of the above exception, another exception occurred:\n\n"
        'Traceback (most recent call last):\n  File "/opt/x/debye_bec/debye_bec/nidaq.py", '
        "line 9, in stage\n    raise RuntimeError('stage failed')\nRuntimeError: stage failed"
    )
    tb = parse_traceback(text)
    assert tb.links == ("context",)
    assert tb.raised.frames[0].package == "debye_bec"
    assert tb.digest()["root_cause"].startswith("JSONDecodeError: Expecting value")


def test_loguru_marks_and_variable_notes_are_understood():
    text = (
        "Traceback (most recent call last):\n\n"
        '> File "/opt/bec/bec_lib/bec_lib/client.py", line 10, in start\n'
        "    self._x()\n"
        "    │    └ <function BECClient._x>\n"
        "    └ <BECClient>\n\n"
        "ValueError: bad"
    )
    tb = parse_traceback(text)
    (frame,) = tb.frames
    assert (frame.file, frame.line, frame.code) == (
        "/opt/bec/bec_lib/bec_lib/client.py",
        10,
        "self._x()",
    )
    assert tb.raised.text() == "ValueError: bad"


def test_escaped_newlines_and_colour_codes_are_undone():
    escaped = json.dumps(SCAN_ABORTION_TRACEBACK)[1:-1]
    assert "\n" not in escaped
    assert len(parse_traceback(escaped).frames) == 12

    coloured = SIMPLE.replace("json.decoder", "\x1b[31mjson.decoder").replace("\n", "\x1b[0m\n")
    assert parse_traceback(coloured).raised.type == "json.decoder.JSONDecodeError"


def test_a_paste_stripped_of_its_indentation_still_parses():
    dedented = "\n".join(line.strip() for line in SCAN_ABORTION_TRACEBACK.splitlines())
    tb = parse_traceback(dedented)
    assert [len(e.frames) for e in tb.chain] == [8, 4]
    assert tb.frames[0].code == "method()"
    assert tb.raised.short_type == "ScanAbortion"


def test_every_traceback_in_a_text_is_found():
    text = (
        'first\nTraceback (most recent call last):\n  File "a.py", line 1, in f\n    x()\n'
        'KeyError: 1\nlater\nTraceback (most recent call last):\n  File "b.py", line 2, in g\n'
        "    y()\nTypeError: 2"
    )
    tracebacks = parse_tracebacks(text)
    assert [(tb.raised.type, tb.prefix) for tb in tracebacks] == [
        ("KeyError", "first"),
        ("TypeError", ""),
    ]
    assert parse_traceback(text).raised.type == "KeyError"


def test_exception_group_gutters_are_stripped():
    text = """  + Exception Group Traceback (most recent call last):
  |   File "/opt/bec/bec_server/bec_server/x.py", line 3, in main
  |     raise ExceptionGroup("many", [ValueError(1), TypeError(2)])
  | ExceptionGroup: many (2 sub-exceptions)
  +-+---------------- 1 ----------------
    | ValueError: 1
    +------------------------------------"""
    tb = parse_traceback(text)
    assert tb.raised.text() == "ExceptionGroup: many (2 sub-exceptions)"
    assert tb.frames[0].code.startswith("raise ExceptionGroup")


def test_frames_without_a_header_are_a_partial_paste():
    text = '  File "/opt/bec/bec_lib/bec_lib/scans.py", line 7, in run\n    go()\nKeyError: samx'
    tb = parse_traceback(text)
    assert tb.raised.text() == "KeyError: samx"
    assert tb.origin().path == "bec_lib/scans.py"


def test_a_cut_off_traceback_keeps_its_frames():
    text = (
        'Traceback (most recent call last):\n  File "/opt/bec/bec_lib/bec_lib/a.py", line 1, in f'
    )
    tb = parse_traceback(text)
    assert tb.raised.type == "?"
    assert tb.frames[0].line == 1


def test_repeated_lines_are_counted():
    text = (
        'Traceback (most recent call last):\n  File "r.py", line 2, in f\n    return f()\n'
        "  [Previous line repeated 996 more times]\nRecursionError: maximum recursion depth"
    )
    assert parse_traceback(text).frames[0].repeated == 996


def test_deep_tracebacks_are_cut_in_the_middle():
    frames = "".join(f'  File "m.py", line {n}, in f{n}\n    f()\n' for n in range(MAX_FRAMES + 40))
    tb = parse_traceback(f"Traceback (most recent call last):\n{frames}RecursionError: deep")
    assert len(tb.frames) == MAX_FRAMES
    assert tb.frames_cut == 40
    assert tb.frames[0].line == 0 and tb.frames[-1].line == MAX_FRAMES + 39


def test_the_digest_bounds_frames_per_exception():
    frames = "".join(f'  File "m.py", line {n}, in f{n}\n' for n in range(30))
    digest = parse_traceback(f"Traceback (most recent call last):\n{frames}ValueError: x").digest()
    (entry,) = digest["chain"]
    assert len(entry["frames"]) == INNER_FRAMES + OUTER_FRAMES
    assert entry["frames_skipped"] == 30 - INNER_FRAMES - OUTER_FRAMES
    assert entry["frames"][-1]["line"] == 29


def test_digest_shape():
    digest = parse_traceback(SCAN_ABORTION_TRACEBACK).digest()
    assert set(digest) == {"exception", "message", "root_cause", "origin", "chain"}
    assert digest["exception"] == "bec_server.scan_server.errors.ScanAbortion"
    assert digest["chain"][1]["link"] == "cause"
    assert digest["origin"]["path"] == "bec_server/scan_server/scans/scan_status.py"
    assert set(digest["chain"][0]["frames"][0]) == {
        "file",
        "line",
        "function",
        "code",
        "package",
        "path",
    }


def test_the_key_is_stable_and_tells_tracebacks_apart():
    tb = parse_traceback(SCAN_ABORTION_TRACEBACK)
    assert tb.key() == parse_traceback(SCAN_ABORTION_TRACEBACK).key()
    assert tb.key() != parse_traceback(SIMPLE).key()


@pytest.mark.parametrize(
    "file, expected",
    [
        (
            "/opt/venv/lib/python3.12/site-packages/bec_lib/client.py",
            ("bec_lib", "bec_lib/client.py"),
        ),
        ("/home/x/bec/bec_lib/bec_lib/client.py", ("bec_lib", "bec_lib/client.py")),
        (
            "/sls/x10da/debye_bec/debye_bec/devices/nidaq.py",
            ("debye_bec", "debye_bec/devices/nidaq.py"),
        ),
        ("/usr/lib/python3.12/threading.py", ("stdlib", "threading.py")),
        ("/opt/venv/lib/python3.12/site-packages/ophyd/signal.py", ("ophyd", "ophyd/signal.py")),
        ("<ipython-input-3-abc>", (None, None)),
        ("/tmp/script.py", (None, None)),
    ],
)
def test_frames_name_their_package_and_path_inside_it(file, expected):
    assert locate(file) == expected
