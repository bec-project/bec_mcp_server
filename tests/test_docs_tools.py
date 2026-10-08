"""bec_docs_search and bec_docs_page as an MCP client sees them."""

from __future__ import annotations

import json

import pytest

from bec_mcp_server.app import LEARN_TIER_NOTE, build_app
from bec_mcp_server.docs import search
from bec_mcp_server.docs.build import build_docs_index
from bec_mcp_server.settings import Settings

SITE = "https://bec.example.org/docs/"


def _payload(result):
    assert not result.is_error, result.content[0].text
    return json.loads(result.content[0].text)


@pytest.fixture
def docs_index(tmp_path):
    """An index built from a small bec_docs-like repository; no test downloads anything."""
    repo = tmp_path / "bec_docs"
    (repo / "docs" / "how-to").mkdir(parents=True)
    (repo / "zensical.toml").write_text(
        f'[project]\nsite_url = "{SITE}"\ndocs_dir = "docs"\n', encoding="utf-8"
    )
    (repo / "docs" / "index.md").write_text("# Index\n\nBEC runs scans.\n", encoding="utf-8")
    (repo / "docs" / "how-to" / "line-scan.md").write_text(
        "# Run a line scan\n\n## Start it\n\nCall scans.line_scan with a motor.\n\n"
        "## Watch it\n\nThe queue shows the running scan.\n",
        encoding="utf-8",
    )
    (repo / "docs" / "how-to" / "restart-the-bec-server.md").write_text(
        "# Restart the BEC server\n\n## Stop the services\n\nRun bec-server stop.\n\n"
        "## Start them again\n\nRun bec-server start.\n",
        encoding="utf-8",
    )
    index = tmp_path / "index.json.gz"
    build_docs_index(repo, index)
    return str(index)


@pytest.fixture
def app(docs_index):
    return build_app(Settings(docs_index=docs_index))


def test_search_answers_a_how_to_question_with_docs_pages(app, call_tool, stdout_guard):
    with stdout_guard():
        payload = _payload(
            call_tool(app.server, "bec_docs_search", {"query": "How do I run a line scan?"})
        )

    results = payload["data"]["results"]
    assert payload["ok"] is True
    assert results[0]["path"] == "how-to/line-scan.md"
    assert results[0]["url"] in payload["summary"]
    assert all(hit["url"].startswith(SITE) for hit in results)
    assert payload["data"]["index"].startswith("bec_docs@")
    assert payload["findings"] == []


def test_page_reads_a_search_result_by_url_and_one_section(app, call_tool):
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


def test_calls_without_arguments_list_the_pages(app, call_tool):
    for tool in ("bec_docs_search", "bec_docs_page"):
        payload = _payload(call_tool(app.server, tool))
        assert payload["ok"] is True
        assert {"path": "index.md", "title": "Index"} in payload["data"]["pages"]


def test_unknown_page_is_an_error_that_names_similar_pages(app, call_tool):
    result = call_tool(app.server, "bec_docs_page", {"path": "how-to/restart-bec.md"})

    assert result.is_error
    assert "Similar pages: how-to/restart-the-bec-server.md" in result.content[0].text


def test_long_page_is_marked_truncated_and_points_at_the_next_section(app, call_tool, monkeypatch):
    monkeypatch.setattr(search, "_PAGE_MAX_CHARS", 40)
    path = "how-to/restart-the-bec-server.md"

    payload = _payload(call_tool(app.server, "bec_docs_page", {"path": path}))

    assert payload["truncated"] is True
    assert len(payload["data"]["text"]) == 40
    unread = next(h for h in payload["data"]["headings"] if h not in payload["data"]["text"])
    assert f'Next: bec_docs_page(path="{path}", heading="{unread}")' in payload["findings"][0]


def test_unreadable_index_says_the_docs_are_unavailable(call_tool, tmp_path):
    app = build_app(Settings(docs_index=str(tmp_path / "missing.json.gz")))

    answer = _payload(call_tool(app.server, "bec_docs_search", {"query": "line scan"}))
    page = _payload(call_tool(app.server, "bec_docs_page", {"path": "index.md"}))
    status = _payload(call_tool(app.server, "mcp_status"))

    assert answer["ok"] is False and page["ok"] is False
    assert "missing.json.gz" in answer["summary"]
    assert answer["findings"][0].endswith("Next: mcp_status().")
    assert status["data"]["docs_index"]["state"] == "unavailable"
    assert any("missing.json.gz" in finding for finding in status["findings"])


def test_mcp_status_reports_the_docs_index_version(app, call_tool, docs_index):
    before = _payload(call_tool(app.server, "mcp_status"))["data"]["docs_index"]
    call_tool(app.server, "bec_docs_search", {"query": "scan"})
    after = _payload(call_tool(app.server, "mcp_status"))["data"]

    assert before == {"configured": docs_index, "state": "not_loaded"}
    assert after["docs_index"]["state"] == "loaded"
    assert after["docs_index"]["version"].startswith("bec_docs@")
    assert after["pools"]["docs"]["in_flight"] == 0


def test_the_published_index_is_the_default_and_is_not_fetched_at_start(call_tool):
    status = _payload(call_tool(build_app().server, "mcp_status"))["data"]["docs_index"]

    assert status == {"configured": search.DEFAULT_INDEX_URL, "state": "not_loaded"}


def test_primer_points_how_to_questions_at_the_docs():
    assert LEARN_TIER_NOTE in build_app().server.instructions


@pytest.mark.parametrize("flag", [["--docs-index", "/srv/docs.json.gz"], []])
def test_docs_index_location_comes_from_flag_or_environment(monkeypatch, flag):
    from bec_mcp_server import cli

    monkeypatch.setenv("BEC_MCP_DOCS_INDEX", "https://example.org/bec_docs_index.json.gz")

    settings = cli.parse_args(flag + ["--audit-log", "off"])

    expected = flag[1] if flag else "https://example.org/bec_docs_index.json.gz"
    assert settings.docs_index == expected
