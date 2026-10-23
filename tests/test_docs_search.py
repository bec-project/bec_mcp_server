import gzip
import http.server
import json
import os
import shutil
import subprocess
import sys
import textwrap
import threading
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from bec_mcp_server.docs import build, search
from bec_mcp_server.docs.build import build_docs_index
from bec_mcp_server.docs.search import DocsIndex, DocsIndexConfig


def _write(path, content):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(textwrap.dedent(content).strip() + "\n", encoding="utf-8")


def _make_docs_repo(root, nav=""):
    """A small bec_docs-like repo; ``nav`` is an optional TOML nav line for zensical.toml."""
    repo = root / "bec_docs"
    _write(
        repo / "zensical.toml",
        f"""
        [project]
        site_url = "https://bec.example.org/docs/"
        docs_dir = "docs"
        {nav}
        """,
    )
    _write(
        repo / "docs" / "index.md",
        """
        # Welcome to BEC

        BEC coordinates scans and devices.
        """,
    )
    _write(
        repo / "docs" / "how-to" / "gui.md",
        """
        # Dock Area Profiles

        ## Switch profiles

        Use the Dock Area profile picker to switch layouts.

        ## Delete a profile

        Remove a profile you no longer need.
        """,
    )
    return repo


def _docs(tmp_path, repo=None, name="index.json"):
    repo = repo or _make_docs_repo(tmp_path)
    index_path = tmp_path / name
    build_docs_index(repo, index_path)
    return DocsIndex(DocsIndexConfig(location=str(index_path))), index_path


# ---------------------------------------------------------------------- builder


def test_build_writes_versioned_index_with_page_urls(tmp_path):
    repo = _make_docs_repo(tmp_path)

    payload = build_docs_index(repo, tmp_path / "index.json")

    assert payload["format"] == "bec-docs-index"
    assert payload["schema_version"] == 1
    assert payload["source"]["name"] == "bec_docs"
    assert payload["page_count"] == 2
    gui = next(page for page in payload["pages"] if page["path"] == "how-to/gui.md")
    assert gui["title"] == "Dock Area Profiles"
    assert gui["url"] == "https://bec.example.org/docs/how-to/gui.html"


def test_build_skips_pages_missing_from_the_nav(tmp_path):
    repo = _make_docs_repo(
        tmp_path, nav='nav = [{ Home = "index.md" }, { GUI = ["how-to/gui.md"] }]'
    )
    _write(repo / "docs" / "draft.md", "# Draft\n\nPlaceholder.")

    payload = build_docs_index(repo, tmp_path / "index.json")

    assert sorted(page["path"] for page in payload["pages"]) == ["how-to/gui.md", "index.md"]


def test_gzip_output_is_reproducible_and_loads(tmp_path):
    repo = _make_docs_repo(tmp_path)
    build_docs_index(repo, tmp_path / "a.json.gz")
    build_docs_index(repo, tmp_path / "b.json.gz")
    first = json.loads(gzip.decompress((tmp_path / "a.json.gz").read_bytes()))
    second = json.loads(gzip.decompress((tmp_path / "b.json.gz").read_bytes()))

    assert first["pages"] == second["pages"]
    docs = DocsIndex(DocsIndexConfig(location=str(tmp_path / "a.json.gz")))
    assert docs.search("profile picker")["results"][0]["path"] == "how-to/gui.md"


def test_builder_runs_as_a_plain_script(tmp_path):
    # bec_docs CI runs the builder without installing the server
    repo = _make_docs_repo(tmp_path)
    output = tmp_path / "out.json.gz"

    subprocess.run(
        [sys.executable, "-I", build.__file__, str(repo), "--output", str(output)],
        check=True,
        capture_output=True,
        cwd=tmp_path,
    )

    assert json.loads(gzip.decompress(output.read_bytes()))["page_count"] == 2


def test_git_commit_marks_dirty_trees(tmp_path):
    if shutil.which("git") is None:
        pytest.skip("git not available")
    repo = _make_docs_repo(tmp_path)
    git = ["git", "-C", str(repo), "-c", "user.email=t@t", "-c", "user.name=t"]
    subprocess.run(git + ["init", "-q"], check=True)
    subprocess.run(git + ["add", "-A"], check=True)
    subprocess.run(git + ["commit", "-q", "-m", "docs"], check=True)

    clean = build_docs_index(repo, tmp_path / "index.json")
    (repo / "docs" / "index.md").write_text("# Changed\n\nEdited content.\n", encoding="utf-8")
    dirty = build_docs_index(repo, tmp_path / "index.json")

    assert not clean["source"]["git_commit"].endswith("-dirty")
    assert dirty["source"]["git_commit"].endswith("-dirty")


def test_code_blocks_are_indexed(tmp_path):
    repo = _make_docs_repo(tmp_path)
    _write(
        repo / "docs" / "how-to" / "scan.md",
        """
        # Run a scan

        Run this command:

        ```python
        scans.mcp_marker_line_scan(dev.samx, -5, 5, steps=10)
        ```
        """,
    )
    docs, _ = _docs(tmp_path, repo)

    assert docs.search("mcp_marker_line_scan")["results"][0]["path"] == "how-to/scan.md"
    text = docs.page("how-to/scan.md")["text"]
    assert "scans.mcp_marker_line_scan(dev.samx, -5, 5, steps=10)" in text


def test_snippet_directives_are_resolved_and_broken_ones_dropped(tmp_path):
    repo = _make_docs_repo(tmp_path)
    _write(
        repo / "tests" / "snippet_tests" / "test_quickstart.py",
        """
        def test_samx_line_scan(bec):
            hidden_setup()  # docs-hide
            scans.line_scan(dev.samx, -1, 1, steps=5)
            print(dev.samx.read())  # docs-display
        """,
    )
    _write(
        repo / "docs" / "tutorial.md",
        """
        # Tutorial

        Run a short line scan:

        --[]->[]--test_snippet--test_quickstart.py:test_samx_line_scan:Run a line scan

        --[]->[]--test_snippet--missing.py:test_nope:Gone
        """,
    )
    docs, _ = _docs(tmp_path, repo)

    text = docs.page("tutorial.md")["text"]
    assert "scans.line_scan(dev.samx, -1, 1, steps=5)" in text
    assert "dev.samx.read()" in text and "print(" not in text
    assert "hidden_setup" not in text
    assert "test_snippet" not in text


def test_markdown_markup_is_stripped_but_headings_keep_ordinals(tmp_path):
    repo = _make_docs_repo(tmp_path)
    _write(
        repo / "docs" / "steps.md",
        """
        # Steps

        ## 1. Check how many scans are available

        !!! learn "[Learn about signals](../learn/signals.md){ data-preview }"
            Use the history property.

        ```text
        ~~~ this tilde line is content, not a closing fence
        ```

        Prose after the block with a [link](x.md).
        """,
    )
    docs, _ = _docs(tmp_path, repo)

    section = docs.page("steps.md", heading="1. Check how many scans are available")
    assert section["heading"] == "1. Check how many scans are available"
    assert "Learn about signals" in section["text"]
    assert "~~~ this tilde line is content" in section["text"]
    assert "](" not in section["text"] and "data-preview" not in section["text"]


# ---------------------------------------------------------------------- search


def test_search_returns_one_result_per_page_with_url(tmp_path):
    docs, _ = _docs(tmp_path)

    result = docs.search("dock area profile", limit=5)

    paths = [hit["path"] for hit in result["results"]]
    assert paths[0] == "how-to/gui.md"
    assert len(paths) == len(set(paths))
    assert result["results"][0]["url"] == "https://bec.example.org/docs/how-to/gui.html"
    assert result["index"].startswith("bec_docs@")


def test_search_matches_parts_of_compound_names(tmp_path):
    repo = _make_docs_repo(tmp_path)
    _write(repo / "docs" / "scan.md", "# Scans\n\nCall scans.line_scan(dev.samx, -1, 1).")
    docs, _ = _docs(tmp_path, repo)

    assert docs.search("line scan")["results"][0]["path"] == "scan.md"


def test_search_without_matches_returns_no_results(tmp_path):
    docs, _ = _docs(tmp_path)

    assert docs.search("zzz_nothing_matches")["results"] == []


def test_toc_lists_every_page(tmp_path):
    docs, _ = _docs(tmp_path)

    toc = docs.toc()

    assert toc["pages"] == [
        {"path": "how-to/gui.md", "title": "Dock Area Profiles"},
        {"path": "index.md", "title": "Welcome to BEC"},
    ]


# ---------------------------------------------------------------------- pages


@pytest.mark.parametrize(
    "path",
    [
        "how-to/gui.md",
        "/how-to/gui",
        "how-to/gui.html",
        "bec_docs/how-to/gui.md",
        "https://bec.example.org/docs/how-to/gui.html#switch-profiles",
    ],
)
def test_page_accepts_paths_and_urls_from_search_results(tmp_path, path):
    docs, _ = _docs(tmp_path)

    page = docs.page(path)

    assert page["path"] == "how-to/gui.md"
    assert page["truncated"] is False
    assert page["headings"] == ["Switch profiles", "Delete a profile"]
    assert "## Switch profiles" in page["text"]


def test_page_returns_one_section_by_heading(tmp_path):
    docs, _ = _docs(tmp_path)

    section = docs.page("how-to/gui.md", heading="switch PROFILES")

    assert section["heading"] == "Switch profiles"
    assert "profile picker" in section["text"]
    with pytest.raises(LookupError, match="headings are: Switch profiles; Delete a profile"):
        docs.page("how-to/gui.md", heading="nope")


def test_unknown_page_suggests_similar_pages(tmp_path):
    docs, _ = _docs(tmp_path)

    with pytest.raises(LookupError, match="Similar pages: how-to/gui.md"):
        docs.page("how-to/dock-area-profile.md")


def test_long_page_is_cut(tmp_path, monkeypatch):
    monkeypatch.setattr(search, "_PAGE_MAX_CHARS", 40)
    docs, _ = _docs(tmp_path)

    page = docs.page("how-to/gui.md")

    assert len(page["text"]) == 40
    assert page["truncated"] is True


# ---------------------------------------------------------------------- loading


def test_published_index_is_the_default_and_nothing_loads_until_asked():
    docs = DocsIndex(DocsIndexConfig())

    assert docs.location == search.DEFAULT_INDEX_URL
    assert docs.describe() == {"configured": search.DEFAULT_INDEX_URL, "state": "not_loaded"}


def test_index_reloads_when_the_file_changes(tmp_path):
    repo = _make_docs_repo(tmp_path)
    docs, index_path = _docs(tmp_path, repo)
    assert docs.search("zzz_fresh_marker")["results"] == []

    _write(repo / "docs" / "fresh.md", "# Fresh page\n\nThe zzz_fresh_marker content.")
    build_docs_index(repo, index_path)
    os.utime(index_path, (0, 0))  # a visible mtime change even on coarse filesystems

    assert docs.search("zzz_fresh_marker")["results"][0]["path"] == "fresh.md"


def test_missing_index_is_reported_as_unavailable(tmp_path):
    docs = DocsIndex(DocsIndexConfig(location=str(tmp_path / "missing.json")))

    with pytest.raises(search.DocsUnavailableError, match="missing.json"):
        docs.search("line scan")

    described = docs.describe()
    assert described["state"] == "unavailable"
    assert "missing.json" in described["error"]


def test_broken_rebuild_keeps_serving_the_last_good_index(tmp_path):
    docs, index_path = _docs(tmp_path)
    assert docs.search("profile picker")["results"]

    index_path.write_text("{not json", encoding="utf-8")
    os.utime(index_path, (0, 0))

    assert docs.search("profile picker")["results"][0]["path"] == "how-to/gui.md"
    described = docs.describe()
    assert described["state"] == "loaded"
    assert "error" in described


def test_unknown_schema_version_is_rejected(tmp_path):
    index_path = tmp_path / "index.json"
    index_path.write_text(
        json.dumps({"format": "bec-docs-index", "schema_version": 99, "pages": []}),
        encoding="utf-8",
    )
    docs = DocsIndex(DocsIndexConfig(location=str(index_path)))

    with pytest.raises(search.DocsUnavailableError, match="schema_version"):
        docs.search("scan")


def test_old_index_carries_a_warning(tmp_path):
    _, index_path = _docs(tmp_path)
    payload = json.loads(index_path.read_text(encoding="utf-8"))
    payload["generated_at"] = (datetime.now(timezone.utc) - timedelta(days=200)).isoformat()
    index_path.write_text(json.dumps(payload), encoding="utf-8")
    docs = DocsIndex(DocsIndexConfig(location=str(index_path)))

    docs.search("profile")

    assert "200 days" in docs.describe()["warning"]


@pytest.fixture
def http_index(tmp_path):
    """Serve tmp_path over HTTP on a free loopback port."""

    class Handler(http.server.SimpleHTTPRequestHandler):
        def __init__(self, *args, **kwargs):
            super().__init__(*args, directory=str(tmp_path), **kwargs)

        def log_message(self, *args):  # keep test output clean
            pass

    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_address[1]}"
    finally:
        server.shutdown()
        server.server_close()


def test_index_loads_from_a_url(tmp_path, http_index):
    build_docs_index(_make_docs_repo(tmp_path), tmp_path / "index.json.gz")
    url = f"{http_index}/index.json.gz"
    docs = DocsIndex(DocsIndexConfig(location=url))

    result = docs.search("profile picker")

    assert result["results"][0]["path"] == "how-to/gui.md"
    assert docs.describe()["loaded_from"] == url


def test_unreachable_url_is_unavailable_and_not_retried_on_every_call(
    tmp_path, http_index, monkeypatch
):
    calls = []
    real_fetch = search._fetch
    monkeypatch.setattr(search, "_fetch", lambda *a: calls.append(a) or real_fetch(*a))
    docs = DocsIndex(DocsIndexConfig(location=f"{http_index}/missing.json"))

    for _ in range(2):
        with pytest.raises(search.DocsUnavailableError, match="404"):
            docs.search("line scan")

    assert docs.describe()["state"] == "unavailable"
    assert len(calls) == 1


def test_url_is_downloaded_again_after_the_refresh_interval(tmp_path, http_index):
    repo = _make_docs_repo(tmp_path)
    build_docs_index(repo, tmp_path / "index.json")
    docs = DocsIndex(DocsIndexConfig(location=f"{http_index}/index.json", refresh_s=60))
    assert docs.search("zzz_fresh_marker")["results"] == []

    _write(repo / "docs" / "fresh.md", "# Fresh page\n\nThe zzz_fresh_marker content.")
    build_docs_index(repo, tmp_path / "index.json")
    assert docs.search("zzz_fresh_marker")["results"] == []  # still within refresh_s
    docs._next_fetch = 0.0  # the refresh interval has passed

    assert docs.search("zzz_fresh_marker")["results"][0]["path"] == "fresh.md"


# ---------------------------------------------------------------------- golden queries


def test_golden_queries_pin_bm25_rankings(tmp_path):
    # regression pin for the hand-rolled BM25 scorer on a fixed corpus: if scoring changes,
    # these top results must survive or the change must be reviewed consciously
    repo = tmp_path / "bec_docs"
    _write(repo / "zensical.toml", '[project]\ndocs_dir = "docs"')
    _write(
        repo / "docs" / "user" / "scans.md",
        """
        # Scans

        ## Line scans

        Run a line scan over one motor:

        ```python
        scans.line_scan(dev.samx, -5, 5, steps=50, exp_time=0.1, relative=False)
        ```
        """,
    )
    _write(
        repo / "docs" / "user" / "devices.md",
        """
        # Devices

        ## Reading a device

        Use dev.samx.read() for the cached readback of the samx positioner.
        """,
    )
    _write(
        repo / "docs" / "how-to" / "macros.md",
        """
        # Macros

        ## Writing a macro

        User macros are Python functions loaded from the macro directory.
        """,
    )
    docs, _ = _docs(tmp_path, repo, name="golden.json")

    golden = {
        "line_scan example": "user/scans.md",
        "read cached readback samx": "user/devices.md",
        "writing a user macro": "how-to/macros.md",
        "exp_time steps relative": "user/scans.md",
    }
    for query, expected in golden.items():
        results = docs.search(query, limit=3)["results"]
        assert results and results[0]["path"] == expected, (query, results)


#: how-to questions a user asks, and the bec_docs pages a good answer cites; the expected
#: page must be among the top three results
GOLDEN_DOCS_QUERIES = {
    "How do I run a line scan?": {"getting-started/quick-start/04-run-your-first-scan.md"},
    "How do I move a motor?": {"getting-started/quick-start/03-move-a-device.md"},
    "How do I add an EPICS motor to the device config?": {"how-to/devices/add-an-epics-motor.md"},
    "How can I open a BEC HDF5 file in Python?": {"how-to/scans/open-bec-hdf5-files-with-h5py.md"},
    "How do I restart the BEC server?": {"how-to/general/restart-the-bec-server.md"},
    "Which readout priority should my device use?": {
        "how-to/devices/how-to-select-readout-priority.md",
        "learn/devices/readout-priority.md",
    },
    "How do I load a device config from a YAML file?": {
        "getting-started/quick-start/02-load-your-first-config.md",
        "how-to/devices/load-and-save-a-device-session-from-the-bec-ipython-client.md",
        "learn/devices/managing-yaml-configs.md",
    },
    "How do I send a message to SciLog?": {"how-to/general/send-messages-to-scilog.md"},
    "How do I get data from previous scans?": {
        "how-to/scans/access-bec-history.md",
        "getting-started/next-steps/access-scan-data.md",
    },
    "How do I fit a gaussian to waveform data?": {"how-to/gui/fit-waveform-data-with-dap.md"},
    "Where are the data files written?": {"learn/file-writer/where-files-are-written.md"},
    "How do I add a pseudo motor?": {
        "how-to/devices/add-a-pseudo-motor.md",
        "learn/devices/pseudo-positioners.md",
    },
    "How do I check my YAML config for errors?": {"how-to/devices/validate-a-yaml-config-file.md"},
    "How do I plot a waveform from the IPython client?": {
        "getting-started/quick-start/05-plot-your-first-waveform.md",
        "how-to/gui/control-waveform-from-ipython.md",
    },
    "How do I switch to another dock area profile?": {"how-to/gui/switch-dock-area-profile.md"},
    "How do I update BEC to a new version?": {"how-to/general/update-deployment.md"},
    "How do I use simulated devices?": {
        "learn/devices/simulated-devices.md",
        "how-to/devices/use-simulated-models-from-ipython.md",
    },
    "How do I report progress from my custom device?": {"how-to/devices/add-a-progress-signal.md"},
}


# no copy of the bec_docs index ships with the package and the unit tests stay offline, so these
# run only against an index given in BEC_MCP_DOCS_INDEX: a freshly built file or the published URL
_GOLDEN_LOCATION = os.environ.get("BEC_MCP_DOCS_INDEX")
needs_bec_docs_index = pytest.mark.skipif(
    not _GOLDEN_LOCATION, reason="set BEC_MCP_DOCS_INDEX to a built or published bec_docs index"
)


@needs_bec_docs_index
@pytest.mark.parametrize("query", sorted(GOLDEN_DOCS_QUERIES))
def test_golden_queries_on_the_bec_docs_index(query):
    docs = _golden_docs_index()

    results = docs.search(query, limit=3)

    assert docs.describe()["state"] == "loaded", docs.describe()
    paths = [hit["path"] for hit in results["results"]]
    assert GOLDEN_DOCS_QUERIES[query] & set(paths), (query, paths)
    assert all(hit["url"].startswith(_SITE_URL) for hit in results["results"])


_SITE_URL = "https://bec-project.github.io/bec_docs/"
_GOLDEN_INDEX: list[DocsIndex] = []


def _golden_docs_index() -> DocsIndex:
    if not _GOLDEN_INDEX:
        _GOLDEN_INDEX.append(DocsIndex(DocsIndexConfig(location=_GOLDEN_LOCATION)))
    return _GOLDEN_INDEX[0]


@needs_bec_docs_index
def test_bec_docs_index_holds_only_published_pages():
    snapshot = _golden_docs_index()._ensure_loaded()

    assert snapshot.source["name"] == "bec_docs"
    assert snapshot.source["site_url"] == _SITE_URL
    assert not (snapshot.source.get("git_commit") or "").endswith("-dirty")
    assert all(page.url.startswith(_SITE_URL) for page in snapshot.pages.values())
    assert not any("Placeholder" in page.sections[0].text for page in snapshot.pages.values())


def test_autodoc_directive_and_its_options_are_dropped(tmp_path):
    page = tmp_path / "page.md"
    page.write_text(
        "# API\n\nBefore the class.\n\n"
        "::: bec_server.scan_server.scans.scan_base.ScanBase\n"
        "    options:\n"
        "      show_root_heading: false\n\n"
        "After the class.\n"
    )

    parsed = build.parse_markdown_page(page, docs_dir=tmp_path, site_url=None)

    assert parsed["sections"] == [
        {"heading": "API", "level": 1, "text": "Before the class.\nAfter the class."}
    ]
