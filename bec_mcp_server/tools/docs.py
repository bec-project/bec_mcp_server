"""Documentation search over bec_docs: the learn tier."""

from __future__ import annotations

from typing import Any

from mcp.server.mcpserver import MCPServer

from bec_mcp_server.docs.search import DocsIndex, DocsUnavailableError
from bec_mcp_server.runtime import CallPool
from bec_mcp_server.tools import READ_ONLY_TOOL, Finding, ToolContext, tool_result

#: docs calls in flight; a pool of their own, so calls stuck on BEC never block docs search
DOCS_POOL_CAPACITY = 4


def register(server: MCPServer, context: ToolContext) -> None:
    """Register bec_docs_search and bec_docs_page; they need the index, not a BEC connection."""
    docs = context.docs
    if docs is None:
        raise RuntimeError("the learn tier needs a docs index on the tool context")
    context.runtime.pools.setdefault(
        "docs", CallPool("docs", DOCS_POOL_CAPACITY, context.settings.tool_timeout)
    )

    @server.tool(
        description="Search the official BEC documentation for a question or keywords and return "
        "the best-matching pages with their URLs.",
        annotations=READ_ONLY_TOOL,
    )
    async def bec_docs_search(query: str = "", limit: int = 5) -> dict:
        return await context.runtime.call("docs", search_answer, docs, query, limit)

    @server.tool(
        description="Return the text of one BEC documentation page by its path or URL, or of one "
        "section when a heading is given.",
        annotations=READ_ONLY_TOOL,
    )
    async def bec_docs_page(path: str = "", heading: str = "") -> dict:
        return await context.runtime.call("docs", page_answer, docs, path, heading)


def search_answer(docs: DocsIndex, query: str, limit: int = 5) -> dict[str, Any]:
    """The bec_docs_search answer; without a query, the list of all pages."""
    try:
        if not query.strip():
            return _contents(docs, "No query given")
        found = docs.search(query, limit)
    except DocsUnavailableError as exc:
        return _unavailable(docs, exc)
    hits = found["results"]
    if hits:
        summary = (
            f"{len(hits)} documentation pages match {found['query']!r}; the best is "
            f"{hits[0]['title']!r} ({hits[0]['url']})."
        )
    else:
        summary = f"No documentation matched {found['query']!r}; try other words."
    return tool_result(
        True,
        summary,
        findings=_index_findings(docs),
        data={"index": found["index"], "results": hits},
    )


def page_answer(docs: DocsIndex, path: str, heading: str = "") -> dict[str, Any]:
    """The bec_docs_page answer; without a path, the list of all pages.

    Raises:
        LookupError: for an unknown page or heading, naming what exists instead.
    """
    try:
        if not path.strip():
            return _contents(docs, "No page path given")
        page = docs.page(path, heading or None)
    except DocsUnavailableError as exc:
        return _unavailable(docs, exc)
    truncated = page.pop("truncated")
    where = f"Section {page['heading']!r} of" if "heading" in page else "Page"
    findings = _index_findings(docs)
    if truncated:
        unread = [name for name in page["headings"] if f"{name}\n" not in page["text"]]
        findings.append(
            Finding(
                "info",
                "page_truncated",
                "The page is long and was cut; read the rest one section at a time",
                subject=page["path"],
                next_tool="bec_docs_page" if unread else None,
                next_args={"path": page["path"], "heading": unread[0]} if unread else None,
            )
        )
    return tool_result(
        True,
        f"{where} {page['title']!r} from the BEC documentation ({page['url']}).",
        findings=findings,
        data=page,
        truncated=truncated,
    )


def _contents(docs: DocsIndex, reason: str) -> dict[str, Any]:
    toc = docs.toc()
    summary = (
        f"{reason}; these are all {len(toc['pages'])} documentation pages. Search them with "
        "bec_docs_search or read one with bec_docs_page."
    )
    return tool_result(True, summary, findings=_index_findings(docs), data=toc)


def _unavailable(docs: DocsIndex, exc: DocsUnavailableError) -> dict[str, Any]:
    """The answer while no docs index could be loaded yet; the index is retried later."""
    return tool_result(
        False,
        f"The BEC documentation is not available right now: {exc} It is tried again in a few "
        "minutes.",
        findings=[
            Finding(
                "error", "docs_unavailable", str(exc), subject="docs index", next_tool="mcp_status"
            )
        ],
        data={"docs_index": docs.describe()},
    )


def _index_findings(docs: DocsIndex) -> list[Finding]:
    described = docs.describe()
    findings = []
    if "warning" in described:
        findings.append(
            Finding("info", "docs_index_stale", described["warning"], subject="docs index")
        )
    return findings
