"""bec_docs_search and bec_docs_page as an MCP client sees them."""

from __future__ import annotations

import json

import pytest

from bec_mcp_server.app import LEARN_TIER_NOTE, build_app
from bec_mcp_server.docs import search
from bec_mcp_server.settings import Settings

DOCS_SITE = "https://bec-project.github.io/bec_docs/"


def _payload(result):
    assert not result.is_error, result.content[0].text
    return json.loads(result.content[0].text)


def test_search_answers_a_how_to_question_with_bec_docs_pages(call_tool, stdout_guard):
    app = build_app()

    with stdout_guard():
        payload = _payload(
            call_tool(app.server, "bec_docs_search", {"query": "How do I run a line scan?"})
        )

    results = payload["data"]["results"]
    assert payload["ok"] is True
    assert results[0]["path"] == "getting-started/quick-start/04-run-your-first-scan.md"
    assert results[0]["url"] in payload["summary"]
    assert all(hit["url"].startswith(DOCS_SITE) for hit in results)
    assert payload["data"]["index"].startswith("bec_docs@")
    assert payload["findings"] == []


def test_page_reads_a_search_result_by_url_and_one_section(call_tool):
    app = build_app()
    hit = _payload(call_tool(app.server, "bec_docs_search", {"query": "restart the BEC server"}))[
        "data"
    ]["results"][0]

    page = _payload(call_tool(app.server, "bec_docs_page", {"path": hit["url"]}))
    heading = page["data"]["headings"][-1]
    section = _payload(
        call_tool(app.server, "bec_docs_page", {"path": hit["path"], "heading": heading})
    )

    assert page["data"]["path"] == hit["path"]
    assert page["data"]["text"]
    assert section["data"]["heading"] == heading
    assert heading in section["summary"]


def test_calls_without_arguments_list_the_pages(call_tool):
    app = build_app()

    for tool in ("bec_docs_search", "bec_docs_page"):
        payload = _payload(call_tool(app.server, tool))
        assert payload["ok"] is True
        assert {"path": "index.md", "title": "Index"} in payload["data"]["pages"]


def test_unknown_page_is_an_error_that_names_similar_pages(call_tool):
    result = call_tool(build_app().server, "bec_docs_page", {"path": "how-to/restart-bec.md"})

    assert result.is_error
    message = result.content[0].text
    assert "Similar pages: how-to/general/restart-the-bec-server.md" in message


def test_long_page_is_marked_truncated_and_points_at_the_next_section(call_tool, monkeypatch):
    monkeypatch.setattr(search, "_PAGE_MAX_CHARS", 100)
    path = "how-to/general/restart-the-bec-server.md"

    payload = _payload(call_tool(build_app().server, "bec_docs_page", {"path": path}))

    assert payload["truncated"] is True
    assert len(payload["data"]["text"]) == 100
    unread = next(h for h in payload["data"]["headings"] if h not in payload["data"]["text"])
    assert f'Next: bec_docs_page(path="{path}", heading="{unread}")' in payload["findings"][0]


def test_unreadable_index_falls_back_and_says_so(call_tool, tmp_path):
    app = build_app(Settings(docs_index=str(tmp_path / "missing.json.gz")))

    answer = _payload(call_tool(app.server, "bec_docs_search", {"query": "line scan"}))
    status = _payload(call_tool(app.server, "mcp_status"))

    assert answer["data"]["results"]
    assert "bundled" in answer["findings"][0]
    assert status["data"]["docs_index"]["state"] == "fallback"
    assert any("missing.json.gz" in finding for finding in status["findings"])


def test_mcp_status_reports_the_docs_index_version(call_tool, tmp_path):
    app = build_app()

    before = _payload(call_tool(app.server, "mcp_status"))["data"]["docs_index"]
    call_tool(app.server, "bec_docs_search", {"query": "scan"})
    after = _payload(call_tool(app.server, "mcp_status"))["data"]

    assert before == {"configured": "bundled", "state": "not_loaded"}
    assert after["docs_index"]["state"] == "loaded"
    assert after["docs_index"]["version"].startswith("bec_docs@")
    assert after["pools"]["docs"]["in_flight"] == 0


def test_primer_points_how_to_questions_at_the_docs():
    assert LEARN_TIER_NOTE in build_app().server.instructions


@pytest.mark.parametrize("flag", [["--docs-index", "/srv/docs.json.gz"], []])
def test_docs_index_location_comes_from_flag_or_environment(monkeypatch, flag):
    from bec_mcp_server import cli

    monkeypatch.setenv("BEC_MCP_DOCS_INDEX", "https://example.org/bec_docs_index.json.gz")

    settings = cli.parse_args(flag + ["--audit-log", "off"])

    expected = flag[1] if flag else "https://example.org/bec_docs_index.json.gz"
    assert settings.docs_index == expected
