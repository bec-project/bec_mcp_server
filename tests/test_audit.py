"""The per-call audit log, observed at the MCP protocol boundary."""

from __future__ import annotations

import json
import threading

from bec_mcp_server.app import build_app
from bec_mcp_server.audit import MAX_ARGUMENT_CHARS, AuditLog
from bec_mcp_server.settings import Settings
from bec_mcp_server.tools import READ_ONLY_TOOL


def _lines(path):
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]


def _audited_app(tmp_path, **settings):
    path = tmp_path / "audit.jsonl"
    app = build_app(Settings(audit_log=path, **settings))

    @app.server.tool(description="Echo probe.", annotations=READ_ONLY_TOOL)
    async def echo(text: str = "", count: int = 1) -> dict:
        return {"text": text, "count": count}

    @app.server.tool(description="Crash probe.", annotations=READ_ONLY_TOOL)
    async def crash() -> dict:
        def fail():
            raise KeyError("missing internal key")  # anticipated: LookupError

        return await app.context.bec_call(fail)

    @app.server.tool(description="Bug probe.", annotations=READ_ONLY_TOOL)
    async def bug() -> dict:
        def fail():
            raise ZeroDivisionError("division by zero")

        return await app.context.bec_call(fail)

    return app, path


def test_every_tool_call_gets_one_line(tmp_path, call_tool):
    app, path = _audited_app(tmp_path)

    call_tool(app.server, "echo", {"text": "samx", "count": 2})
    call_tool(app.server, "mcp_status")
    app.audit_log.flush()

    first, second = _lines(path)
    assert first["tool"] == "echo"
    assert first["arguments"] == {"text": "samx", "count": 2}
    assert first["outcome"] == "ok"
    assert first["transport"] == "stdio"
    assert first["client"] == "mcp"
    assert first["duration_ms"] >= 0
    assert "error" not in first
    assert second["tool"] == "mcp_status"


def test_mistakes_a_model_makes_are_recorded(tmp_path, call_tool):
    # these never reach a tool function, which is why the log sits at the protocol boundary
    app, path = _audited_app(tmp_path)

    call_tool(app.server, "bec_devicez")
    call_tool(app.server, "echo", {"count": "many"})
    app.audit_log.flush()

    unknown, invalid = _lines(path)
    assert unknown["outcome"] == "error"
    assert "Unknown tool" in unknown["error"]
    assert invalid["outcome"] == "error"
    assert invalid["arguments"] == {"count": "many"}
    assert "count" in invalid["error"]


def test_anticipated_errors_and_crashes_are_told_apart(tmp_path, call_tool):
    app, path = _audited_app(tmp_path)

    call_tool(app.server, "crash")
    call_tool(app.server, "bug")
    app.audit_log.flush()

    anticipated, crashed = _lines(path)
    assert "missing internal key" in anticipated["error"]
    assert "cause" not in anticipated
    # the agent saw only the generic text; the operator's log keeps the real cause
    assert "division by zero" not in crashed["error"]
    assert crashed["cause"] == "ZeroDivisionError: division by zero"
    assert app.context.runtime.calls_failed == 2


def test_oversized_arguments_are_cut(tmp_path, call_tool):
    app, path = _audited_app(tmp_path)

    call_tool(app.server, "echo", {"text": "x" * (MAX_ARGUMENT_CHARS * 2)})
    app.audit_log.flush()

    (line,) = _lines(path)
    assert set(line["arguments"]) == {"_truncated"}
    assert len(line["arguments"]["_truncated"]) <= MAX_ARGUMENT_CHARS + 3


def test_an_unwritable_log_disables_itself_without_failing_calls(tmp_path, call_tool):
    blocker = tmp_path / "not-a-dir"
    blocker.write_text("", encoding="utf-8")
    app = build_app(Settings(audit_log=blocker / "audit.jsonl"))

    first = call_tool(app.server, "mcp_status")
    app.audit_log.flush()
    second = call_tool(app.server, "mcp_status")

    assert not first.is_error and not second.is_error
    assert app.audit_log.enabled is False


def test_log_rotates_at_its_size_limit(tmp_path):
    path = tmp_path / "audit.jsonl"
    log = AuditLog(path, max_bytes=200, backups=2)
    for index in range(12):
        log.record({"tool": "echo", "index": index, "pad": "x" * 40})
    log.close()

    assert path.exists()
    assert path.with_name("audit.jsonl.1").exists()
    assert path.with_name("audit.jsonl.2").exists()
    assert not path.with_name("audit.jsonl.3").exists()
    indices = [entry["index"] for entry in _lines(path)]
    assert indices[-1] == 11
    assert path.stat().st_size <= 200


def test_record_never_blocks_on_the_writer(tmp_path):
    log = AuditLog(tmp_path / "audit.jsonl")
    done = threading.Event()

    def burst():
        for index in range(1000):
            log.record({"index": index})
        done.set()

    thread = threading.Thread(target=burst)
    thread.start()
    assert done.wait(2)
    thread.join()
    log.close()
    assert len(_lines(tmp_path / "audit.jsonl")) == 1000
