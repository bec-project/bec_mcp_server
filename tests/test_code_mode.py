"""Code mode: off by default, confined to the allowed roots, and useful on a real traceback."""

from __future__ import annotations

import json
import os
import sys
import textwrap
import traceback
from pathlib import Path

import anyio
import pytest
from mcp.client import Client

from bec_mcp_server import cli
from bec_mcp_server.app import CODE_MODE_NOTE, build_app
from bec_mcp_server.bec.log_stream import LogRecord
from bec_mcp_server.settings import Settings, parse_code_roots
from bec_mcp_server.source import roots as roots_module
from bec_mcp_server.source.roots import SourceRoot, SourceTree, parse_symbols
from bec_mcp_server.tools import ToolContext, code, code_hints, log_analysis, tool_result

WORKER = '''\
"""A scan worker, standing in for BEC code."""


class Worker:
    def __init__(self, devices):
        self.devices = devices

    def run(self, name):
        return self._move(name)

    def _move(self, name):
        device = self.devices[name]
        return device


def helper():
    raise ValueError("Device samx is not enabled")
'''

CODE_TOOLS = {"bec_code_traceback", "bec_code_read", "bec_code_search"}


@pytest.fixture
def src(tmp_path):
    """A source root with one package: src/beclike/worker.py."""
    root = tmp_path / "src"
    package = root / "beclike"
    package.mkdir(parents=True)
    (package / "__init__.py").write_text("", encoding="utf-8")
    (package / "worker.py").write_text(WORKER, encoding="utf-8")
    return root


@pytest.fixture
def tree(src):
    return SourceTree([SourceRoot("src", src)])


def _raise_in(root, *, path_prefix=None):
    """A real traceback from running Worker.run in ``root``; paths optionally rewritten."""
    sys.path.insert(0, str(root))
    try:
        sys.modules.pop("beclike.worker", None)
        sys.modules.pop("beclike", None)
        from beclike.worker import Worker  # pylint: disable=import-outside-toplevel

        try:
            Worker({}).run("samx")
        except KeyError:
            text = traceback.format_exc()
    finally:
        sys.path.remove(str(root))
        sys.modules.pop("beclike.worker", None)
        sys.modules.pop("beclike", None)
    if path_prefix is not None:
        text = text.replace(str(root), path_prefix)
    return text


def _answer(tree, text):
    return code.traceback_call(tree, code_hints.TracebackHints(code_hints.TracebackRefs()), text)


def _list_tools(server):
    async def run():
        async with Client(server, mode="legacy") as client:
            return (await client.list_tools()).tools

    return anyio.run(run)


def _payload(result):
    assert not result.is_error, result.content[0].text
    return json.loads(result.content[0].text)


def _code_settings(root, **kwargs):
    return Settings(mode="code", code_roots=(SourceRoot("src", root),), **kwargs)


# ------------------------------------------------------------------------------------------------
# off by default
# ------------------------------------------------------------------------------------------------


def test_the_default_mode_has_no_code_tools_and_no_code_note():
    app = build_app()

    names = {tool.name for tool in _list_tools(app.server)}
    assert not names & CODE_TOOLS
    assert app.context.code is None
    assert CODE_MODE_NOTE not in app.server.instructions


def test_code_mode_registers_the_code_tools_read_only(src):
    app = build_app(_code_settings(src))

    tools = {tool.name: tool for tool in _list_tools(app.server)}
    assert CODE_TOOLS <= set(tools)
    for name in CODE_TOOLS:
        assert tools[name].annotations.read_only_hint is True
        assert tools[name].description.count(". ") == 0
    assert CODE_MODE_NOTE in app.server.instructions


def test_code_mode_needs_roots_and_roots_need_code_mode(src):
    with pytest.raises(SystemExit, match="--code-root"):
        Settings(mode="code")
    with pytest.raises(SystemExit, match="only applies to --mode code"):
        Settings(code_roots=(SourceRoot("src", src),))


def test_code_roots_come_from_flags_or_the_environment(monkeypatch, src, tmp_path):
    other = tmp_path / "other"
    other.mkdir()
    monkeypatch.setenv("BEC_MCP_CODE_ROOTS", f"{src},lib={other}")
    from_env = cli.parse_args(["--mode", "code"])
    assert [(root.label, root.path) for root in from_env.code_roots] == [
        ("src", src),
        ("lib", other),
    ]

    from_flags = cli.parse_args(["--mode", "code", "--code-root", str(other)])
    assert [root.label for root in from_flags.code_roots] == ["other"]


def test_root_labels_stay_unique_and_bad_roots_are_refused(tmp_path):
    first, second = tmp_path / "a" / "src", tmp_path / "b" / "src"
    first.mkdir(parents=True)
    second.mkdir(parents=True)

    roots = parse_code_roots([f"{first},{second},{first}"], option="--code-root")

    assert [root.label for root in roots] == ["src", "src-2"]
    with pytest.raises(SystemExit, match="not a directory"):
        parse_code_roots([str(tmp_path / "missing")], option="--code-root")
    with pytest.raises(SystemExit, match="not /"):
        parse_code_roots(["/"], option="--code-root")


# ------------------------------------------------------------------------------------------------
# confinement
# ------------------------------------------------------------------------------------------------


def test_files_are_found_by_display_path_relative_path_module_and_foreign_path(tree):
    expected = "src/beclike/worker.py"

    assert tree.find("src/beclike/worker.py").display == expected
    assert tree.find("beclike/worker.py").display == expected
    assert tree.find("beclike.worker").display == expected
    foreign = tree.find("/opt/venv/lib/python3.11/site-packages/beclike/worker.py")
    assert (foreign.display, foreign.match) == (expected, "tail")


@pytest.mark.parametrize(
    "name",
    [
        "src/../outside.py",
        "../outside.py",
        "/etc/passwd",
        "src/beclike/notes.txt",
        "<frozen importlib._bootstrap>",
        "worker.py/..",
    ],
)
def test_nothing_outside_the_roots_or_besides_python_is_resolved(tree, src, name):
    (src.parent / "outside.py").write_text("SECRET = 1\n", encoding="utf-8")
    (src / "beclike" / "notes.txt").write_text("not code\n", encoding="utf-8")

    assert tree.find(name) is None


def test_a_bare_file_name_from_elsewhere_does_not_match_by_name_alone(tree):
    # another project's worker.py must not be taken for this one
    assert tree.find("/opt/other/thing/worker.py") is None
    assert tree.find("worker.py") is not None


def test_symlinks_out_of_a_root_are_refused(tree, src, tmp_path):
    secret = tmp_path / "secret.py"
    secret.write_text("TOKEN = 'x'\n", encoding="utf-8")
    (src / "beclike" / "leak.py").symlink_to(secret)
    (src / "linked_dir").symlink_to(tmp_path, target_is_directory=True)

    assert tree.find("src/beclike/leak.py") is None
    assert tree.find(str(src / "beclike" / "leak.py")) is None
    assert tree.find("src/linked_dir/secret.py") is None
    assert tree.files_named("secret.py") == []


@pytest.mark.skipif(not hasattr(os, "mkfifo"), reason="needs FIFOs")
def test_a_fifo_is_never_opened(tree, src):
    os.mkfifo(src / "beclike" / "pipe.py")

    assert tree.find("src/beclike/pipe.py") is None


def test_large_files_are_refused(monkeypatch, tree):
    monkeypatch.setattr(roots_module, "MAX_FILE_BYTES", 100)

    with pytest.raises(ValueError, match="more than code mode reads"):
        tree.read_lines(tree.resolve("src/beclike/worker.py"))


def test_hidden_and_cache_directories_are_not_indexed(tree, src):
    for hidden in (".git", "__pycache__", ".venv"):
        (src / hidden).mkdir()
        (src / hidden / "worker.py").write_text("", encoding="utf-8")

    assert tree.files_named("worker.py") == ["src/beclike/worker.py"]


def test_a_miss_names_the_files_with_that_name(tree):
    with pytest.raises(LookupError, match="src/beclike/worker.py"):
        tree.resolve("src/elsewhere/worker.py")


def test_equal_tails_prefer_the_first_root_and_say_so(tmp_path, src):
    second = tmp_path / "second"
    (second / "beclike").mkdir(parents=True)
    (second / "beclike" / "worker.py").write_text(WORKER, encoding="utf-8")
    tree = SourceTree([SourceRoot("src", src), SourceRoot("second", second)])

    found = tree.find("/deployed/beclike/worker.py")

    assert (found.display, found.alternatives) == ("src/beclike/worker.py", 1)


def test_symbols_have_qualified_names_and_spans():
    symbols = {symbol.name: symbol for symbol in parse_symbols(WORKER)}

    assert symbols["Worker._move"].start == 11
    assert symbols["Worker._move"].end == 13
    assert symbols["helper"].kind == "function"
    assert parse_symbols("def broken(:\n") == []


# ------------------------------------------------------------------------------------------------
# bec_code_traceback
# ------------------------------------------------------------------------------------------------


def test_a_traceback_resolves_to_the_raising_function_and_its_callers(tree, src):
    answer = _answer(tree, _raise_in(src))

    frames = [frame for frame in answer["data"]["frames"] if frame["under_roots"]]
    assert [frame["in"] for frame in frames] == ["Worker.run", "Worker._move"]
    raiser = frames[-1]
    assert raiser["source_check"] == "matches"
    assert ">   12|         device = self.devices[name]" in raiser["code"]
    # the raising function is shown whole, its caller in a short window
    assert "def _move(self, name):" in raiser["code"]
    assert "return device" in raiser["code"]
    assert answer["summary"].startswith("KeyError: 'samx' raised in Worker._move")
    assert answer["ok"] is True


def test_a_traceback_from_another_host_matches_by_path_tail(tree, src):
    text = _raise_in(src, path_prefix="/opt/bec/venv/lib/python3.11/site-packages")

    answer = _answer(tree, text)

    raiser = answer["data"]["frames"][-1]
    assert raiser["file"] == "src/beclike/worker.py"
    assert raiser["matched_by"] == "path tail"
    assert raiser["source_check"] == "matches"


def test_a_different_version_under_the_root_is_flagged(tree, src):
    text = _raise_in(src)
    worker = src / "beclike" / "worker.py"
    worker.write_text(WORKER.replace("class Worker:", "# moved\n\n\nclass Worker:"), "utf-8")

    answer = _answer(tree, text)

    raiser = answer["data"]["frames"][-1]
    assert raiser["source_check"] == "moved"
    assert raiser["line_here"] == raiser["line"] + 3
    assert ">   15|         device = self.devices[name]" in raiser["code"]
    codes = [finding["code"] for finding in answer["data"]["findings"]]
    assert "source_version_mismatch" in codes


def test_frames_outside_the_roots_are_listed_without_code(tmp_path, tree):
    text = (
        "Traceback (most recent call last):\n"
        '  File "/usr/lib/python3.11/socket.py", line 10, in connect\n'
        "    sock.connect(address)\n"
        "ConnectionRefusedError: [Errno 61] Connection refused\n"
    )

    answer = _answer(tree, text)

    frame = answer["data"]["frames"][0]
    assert frame == {
        "frame": 0,
        "file": "python3.11/socket.py",
        "line": 10,
        "function": "connect",
        "under_roots": False,
        "traceback_source": "sock.connect(address)",
    }
    assert answer["summary"].startswith(
        "ConnectionRefusedError: [Errno 61] Connection refused: its"
    )
    assert answer["findings"][0] == (
        "Warning: The code that raised is not below the allowed source roots (src); its frames "
        "are in the Python standard library."
    )


def test_lines_bec_appends_to_an_exception_reach_the_summary(tree, src):
    text = _raise_in(src).rstrip("\n") + "\n\nLimitError: position=50 not within limits (-5, 5)\n"

    answer = _answer(tree, text)

    assert answer["data"]["exception_detail"] == "LimitError: position=50 not within limits (-5, 5)"
    assert answer["summary"].startswith(
        "KeyError: 'samx' (LimitError: position=50 not within limits (-5, 5)) raised in"
    )


def test_text_without_frames_is_refused_with_advice(tree):
    with pytest.raises(ValueError, match="bec_code_search"):
        _answer(tree, "KeyError: 'samx'")


def test_the_code_budget_drops_frames_and_says_where_to_read_on(monkeypatch, tree, src):
    monkeypatch.setattr(code, "MAX_ANSWER_CODE_CHARS", 250)

    answer = _answer(tree, _raise_in(src))

    assert answer["truncated"] is True
    skipped = [f for f in answer["data"]["findings"] if f["code"] == "frames_without_code"]
    assert skipped[0]["next"] == {
        "tool": "bec_code_read",
        "args": {"path": "src/beclike/worker.py", "line": 9},
    }


def test_a_long_raising_function_is_cut_around_its_line(monkeypatch, tree, src):
    monkeypatch.setattr(code, "MAX_FUNCTION_LINES", 2)

    answer = _answer(tree, _raise_in(src))

    assert answer["data"]["frames"][-1]["code"].count("\n") == 2
    cut = [f for f in answer["data"]["findings"] if f["code"] == "function_cut"]
    assert cut[0]["next"]["args"] == {"path": "src/beclike/worker.py", "symbol": "Worker._move"}


# ------------------------------------------------------------------------------------------------
# bec_code_read and bec_code_search
# ------------------------------------------------------------------------------------------------


def test_read_without_a_path_lists_the_roots_by_label_only(tree, src):
    answer = code.read_answer(tree, "")

    assert answer["data"] == {"roots": [{"label": "src"}]}
    assert str(src) not in json.dumps(answer)


def test_read_a_symbol_a_line_window_and_a_range(tree):
    by_symbol = code.read_answer(tree, "beclike.worker", symbol="_move")
    assert by_symbol["data"]["range"] == [11, 13]
    assert by_symbol["data"]["symbol"] == {"name": "Worker._move", "kind": "function"}

    by_line = code.read_answer(tree, "src/beclike/worker.py", line=12)
    assert by_line["data"]["in"] == "Worker._move"
    assert ">   12|" in by_line["data"]["code"]

    by_range = code.read_answer(tree, "src/beclike/worker.py", start=4, end=5)
    assert by_range["data"]["code"].splitlines() == [
        "     4| class Worker:",
        "     5|     def __init__(self, devices):",
    ]


def test_read_of_a_short_file_returns_it_whole_and_a_long_one_as_an_outline(monkeypatch, tree):
    whole = code.read_answer(tree, "src/beclike/worker.py")
    assert whole["data"]["range"] == [1, 17]

    monkeypatch.setattr(code, "WHOLE_FILE_LINES", 5)
    outline = code.read_answer(tree, "src/beclike/worker.py")
    assert [entry["name"] for entry in outline["data"]["outline"]] == [
        "Worker",
        "Worker.__init__",
        "Worker.run",
        "Worker._move",
        "helper",
    ]
    assert "code" not in outline["data"]


def test_read_caps_the_lines_it_returns(monkeypatch, tree):
    monkeypatch.setattr(code, "MAX_READ_LINES", 3)

    answer = code.read_answer(tree, "src/beclike/worker.py", start=1, end=17)

    assert answer["data"]["range"] == [1, 3]
    assert answer["truncated"] is True
    assert answer["data"]["findings"][0]["next"]["args"]["start"] == 4


def test_read_explains_unknown_symbols_and_lines(tree):
    with pytest.raises(LookupError, match="Worker.run"):
        code.read_answer(tree, "src/beclike/worker.py", symbol="runn")
    with pytest.raises(ValueError, match="17 lines"):
        code.read_answer(tree, "src/beclike/worker.py", line=99)


def test_search_finds_where_a_message_is_raised(tree):
    answer = code.search_answer(tree, "is not ENABLED")

    assert answer["data"]["matches"] == [
        {
            "file": "src/beclike/worker.py",
            "line": 17,
            "text": 'raise ValueError("Device samx is not enabled")',
            "in": "helper",
        }
    ]


def test_search_ranks_code_before_its_tests(tree, src):
    tests = src / "tests"
    tests.mkdir()
    (tests / "test_worker.py").write_text(
        'def test_disabled():\n    assert "is not enabled" in error\n', encoding="utf-8"
    )

    answer = code.search_answer(tree, "is not enabled")

    assert [match["file"] for match in answer["data"]["matches"]] == [
        "src/beclike/worker.py",
        "src/tests/test_worker.py",
    ]


def test_search_lists_definitions_first_and_respects_scope_and_limit(tree):
    answer = code.search_answer(tree, "_move", path="src/beclike")

    assert answer["data"]["matches"][0]["text"] == "def _move(self, name):"
    limited = code.search_answer(tree, "self", limit=2)
    assert len(limited["data"]["matches"]) == 2
    assert limited["truncated"] is True
    with pytest.raises(LookupError, match="not under an allowed source root"):
        code.search_answer(tree, "self", path="elsewhere")
    with pytest.raises(ValueError, match="at least"):
        code.search_answer(tree, "ab")


# ------------------------------------------------------------------------------------------------
# through MCP, and the traceback findings on other tools' answers
# ------------------------------------------------------------------------------------------------


def test_every_code_tool_answers_without_arguments(call_tool, stdout_guard, src):
    app = build_app(_code_settings(src))
    for name in sorted(CODE_TOOLS):
        with stdout_guard():
            payload = _payload(call_tool(app.server, name))
        assert payload["summary"].startswith("No ")


def test_mcp_status_names_the_code_roots_without_host_paths(call_tool, src):
    payload = _payload(call_tool(build_app(_code_settings(src)).server, "mcp_status"))

    assert payload["data"]["mode"] == "code"
    assert payload["data"]["code"] == {"roots": [{"label": "src"}]}
    assert payload["data"]["pools"]["code"] == {"capacity": 2, "in_flight": 0}
    assert str(src) not in json.dumps(payload)
    default = _payload(call_tool(build_app().server, "mcp_status"))
    assert default["data"]["code"] == {"state": "off"}


def _add_log_tool(app, text):
    @app.server.tool(description="A log line with a traceback.")
    async def fake_logs() -> dict:
        return tool_result(True, "one record", data={"messages": [{"message": text}]})


def test_answers_with_a_traceback_point_at_bec_code_traceback_by_ref(call_tool, src):
    app = build_app(_code_settings(src))
    text = _raise_in(src, path_prefix="/opt/bec/venv/lib/python3.11/site-packages")
    _add_log_tool(app, f"Failed to run move on samx.\n{text}")

    logs = _payload(call_tool(app.server, "fake_logs"))

    hint = logs["data"]["findings"][0]
    assert hint["code"] == "traceback_code"
    assert hint["next"]["tool"] == "bec_code_traceback"
    ref = hint["next"]["args"]["ref"]
    assert logs["findings"][0] == (
        "Info: Code mode can show the source of the 3 frames behind KeyError: 'samx'. "
        f'Next: bec_code_traceback(ref="{ref}").'
    )

    resolved = _payload(call_tool(app.server, "bec_code_traceback", {"ref": ref}))
    assert resolved["summary"].startswith("KeyError: 'samx' raised in Worker._move")
    # code answers quote source, so they never point at themselves
    assert not any(f["code"] == "traceback_code" for f in resolved["data"].get("findings", []))


def test_other_modes_never_add_traceback_findings(call_tool, src):
    app = build_app()
    _add_log_tool(app, _raise_in(src))

    assert _payload(call_tool(app.server, "fake_logs"))["findings"] == []


def test_an_unknown_ref_asks_for_the_text(call_tool, src):
    result = call_tool(build_app(_code_settings(src)).server, "bec_code_traceback", {"ref": "tb-0"})

    assert result.is_error
    assert "Pass the traceback text instead" in result.content[0].text


def test_a_failing_hint_never_costs_the_answer(monkeypatch, src):
    def broken(self, data):
        raise RuntimeError("boom")

    monkeypatch.setattr(code_hints.TracebackHints, "_tracebacks", broken)
    hints = code_hints.TracebackHints(code_hints.TracebackRefs())
    token = code_hints.ANSWER_HOOK.set(hints.hints)
    try:
        answer = tool_result(True, "fine", data={"text": "x"})
    finally:
        code_hints.ANSWER_HOOK.reset(token)

    assert answer["summary"] == "fine"


def test_register_refuses_a_context_without_roots(src):
    context = build_app().context
    assert isinstance(context, ToolContext)
    with pytest.raises(RuntimeError, match="source roots"):
        code.register(build_app().server, context, code_hints.TracebackRefs())


def test_the_readme_documents_code_mode():
    text = (Path(__file__).parents[1] / "README.md").read_text(encoding="utf-8")

    assert "--code-root" in text
    for name in CODE_TOOLS:
        assert f"`{name}`" in text


def test_the_source_snippet_helper_dedents_nothing():
    # numbered lines keep their indentation, so the agent sees the real nesting
    rendered = code.render(textwrap.dedent(WORKER).splitlines(), 11, 12, mark=12)

    assert rendered.splitlines() == [
        "    11|     def _move(self, name):",
        ">   12|         device = self.devices[name]",
    ]


# ------------------------------------------------------------------------------------------------
# log records from the log analyzer's cache
# ------------------------------------------------------------------------------------------------


def _record(record_id, message, service="ScanServer"):
    return LogRecord(
        service=service,
        level="ERROR",
        level_no=40,
        timestamp=1_760_000_000.0 + record_id,
        message=message,
        source="bec_server.scan_server.scan_worker:run",
        id=record_id,
    )


@pytest.fixture
def records(src):
    failure = f"Failed to move samx.\n{_raise_in(src, path_prefix='/opt/venv/site-packages')}"
    return [
        _record(5, failure),
        _record(6, "scan 12 started", service="ScanBundler"),
        _record(7, failure),
        _record(8, "Failed to move samx again, no traceback this time"),
    ]


def _hints(records):
    return code_hints.TracebackHints(code_hints.TracebackRefs(), lambda: records)


def test_a_log_record_id_resolves_its_traceback(tree, records):
    answer = code.traceback_call(tree, _hints(records), "", record=7)

    assert answer["summary"].startswith(
        "Log record 7 (ScanServer): KeyError: 'samx' raised in Worker._move"
    )
    assert answer["data"]["record"]["id"] == 7
    assert answer["data"]["frames"][-1]["file"] == "src/beclike/worker.py"


def test_a_record_that_is_gone_or_has_no_traceback_is_explained(tree, records):
    with pytest.raises(LookupError, match="holds ids 5 to 8"):
        code.traceback_call(tree, _hints(records), "", record=99)
    with pytest.raises(ValueError, match="no Python traceback"):
        code.traceback_call(tree, _hints(records), "", record=8)
    with pytest.raises(LookupError, match="no log cache"):
        code.traceback_call(
            tree, code_hints.TracebackHints(code_hints.TracebackRefs()), "", record=7
        )


def test_log_range_records_point_at_their_record_id(records):
    data = {"records": [log_analysis.record_digest(record) for record in records]}

    findings = _hints(records).hints(data)

    # records 5 and 7 hold the same traceback, so it is hinted once
    assert [(f.next_tool, f.next_args) for f in findings] == [("bec_code_traceback", {"record": 5})]
    assert findings[0].message.startswith("Code mode can show the source of the 3 frames")


def test_incidents_point_at_the_record_that_holds_their_traceback(records):
    incidents = log_analysis.find_incidents(records, log_analysis.analysis_request())
    groups = log_analysis.clusters(incidents)
    data = {
        "incidents": [
            log_analysis.incident_digest(records, groups, incident)
            for incident in incidents
            if incident.traceback is not None
        ]
    }

    findings = _hints(records).hints(data)

    assert [f.next_args for f in findings] == [{"record": 7}]


def test_record_hints_and_lookup_through_mcp(call_tool, src, records):
    class _Cache:
        def view(self):
            return type("View", (), {"records": records})()

    class _Connection:
        log_cache = _Cache()

        def connect(self):
            pass

        def shutdown(self):
            pass

        def state(self):
            return {"state": "connected"}

    app = build_app(_code_settings(src), connection=_Connection())

    @app.server.tool(description="Log records like bec_log_range returns them.")
    async def fake_range() -> dict:
        digests = [log_analysis.record_digest(record) for record in records]
        return tool_result(True, "records", data={"records": digests})

    listed = _payload(call_tool(app.server, "fake_range"))
    assert listed["findings"][0].endswith("Next: bec_code_traceback(record=5).")

    resolved = _payload(call_tool(app.server, "bec_code_traceback", {"record": 5}))
    assert resolved["summary"].startswith("Log record 5 (ScanServer): KeyError: 'samx'")
