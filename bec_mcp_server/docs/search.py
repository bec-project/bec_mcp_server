"""In-process BM25 search over the BEC documentation index.

The index is a JSON file built from bec_docs by :mod:`bec_mcp_server.docs.build`, normally in the
bec_docs CI. The server loads it from a path or an http(s) URL and falls back to the small index
bundled with the package when none is configured or the configured one cannot be loaded.

Search needs no external service and keeps no state on disk, so any number of server processes
can share one index file without locking.
"""

from __future__ import annotations

import gzip
import json
import math
import re
import threading
import time
import urllib.request
from collections import Counter, defaultdict
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from urllib.parse import unquote, urlsplit

from bec_mcp_server.docs.build import INDEX_FORMAT, SCHEMA_VERSION

#: index shipped with the package, used when no index is configured or it cannot be loaded
BUNDLED_INDEX = Path(__file__).resolve().parent / "bec_docs_index.json.gz"

_TOKEN_RE = re.compile(r"[A-Za-z0-9_][A-Za-z0-9_:-]*")
_TOKEN_SPLIT_RE = re.compile(r"[_:-]+")
_BM25_K1 = 1.2
_BM25_B = 0.75

#: larger downloads are refused; the bec_docs index is well below 1 MB
_MAX_INDEX_BYTES = 64 * 1024 * 1024
#: after a failed download, wait this long before trying the URL again
_RETRY_AFTER_S = 300.0
#: age after which describe() warns that the index may be out of date
_STALE_INDEX_AFTER_DAYS = 60
#: full pages are cut here; a section can always be requested on its own
_PAGE_MAX_CHARS = 12_000
_MAX_RESULTS = 20


def _tokenize(text: str) -> list[str]:
    """Lower-case word tokens; compound names also yield their parts (line_scan -> line, scan)."""
    tokens: list[str] = []
    for token in _TOKEN_RE.findall(text):
        token = token.lower()
        tokens.append(token)
        parts = [part for part in _TOKEN_SPLIT_RE.split(token) if part]
        if len(parts) > 1:
            tokens.extend(parts)
    return tokens


def _make_snippet(text: str, query_tokens: list[str], max_chars: int = 240) -> str:
    compact = re.sub(r"\s+", " ", text).strip()
    lowered = compact.lower()
    positions = [lowered.find(token) for token in query_tokens if token]
    positions = [position for position in positions if position >= 0]
    if not positions:
        return compact[:max_chars].rstrip()

    start = max(0, min(positions) - max_chars // 4)
    end = min(len(compact), start + max_chars)
    snippet = compact[start:end].strip()
    if start > 0:
        snippet = f"...{snippet}"
    if end < len(compact):
        snippet = f"{snippet}..."
    return snippet


def _is_url(location: str) -> bool:
    return urlsplit(location).scheme in ("http", "https")


def _age_days(generated_at: str | None) -> int | None:
    if not generated_at:
        return None
    try:
        generated = datetime.fromisoformat(generated_at)
    except (TypeError, ValueError):
        return None
    if generated.tzinfo is None:
        generated = generated.replace(tzinfo=timezone.utc)
    return max(0, (datetime.now(timezone.utc) - generated).days)


def _decode_index(raw: bytes) -> dict[str, Any]:
    if raw[:2] == b"\x1f\x8b":
        raw = gzip.decompress(raw)
    payload = json.loads(raw)
    if not isinstance(payload, dict) or payload.get("format") != INDEX_FORMAT:
        raise ValueError("not a BEC docs index")
    if payload.get("schema_version") != SCHEMA_VERSION:
        raise ValueError(
            f"unsupported docs index schema_version {payload.get('schema_version')!r} "
            f"(this server reads version {SCHEMA_VERSION})"
        )
    return payload


def _fetch(url: str, timeout: float) -> bytes:
    request = urllib.request.Request(url, headers={"User-Agent": "bec-mcp-server"})
    with urllib.request.urlopen(request, timeout=timeout) as response:
        raw = response.read(_MAX_INDEX_BYTES + 1)
    if len(raw) > _MAX_INDEX_BYTES:
        raise ValueError(f"docs index at {url} is larger than {_MAX_INDEX_BYTES} bytes")
    return raw


@dataclass(frozen=True)
class DocsIndexConfig:
    #: path or http(s) URL of the index; None uses the bundled index
    location: str | None = None
    #: how often a URL is downloaded again; paths reload whenever their mtime changes
    refresh_s: float = 3600.0
    #: download timeout, kept well below the tool-call deadline
    timeout_s: float = 10.0


@dataclass(frozen=True)
class _Section:
    page: str
    heading: str | None
    level: int
    text: str


@dataclass(frozen=True)
class _Page:
    path: str
    title: str
    url: str | None
    sections: tuple[_Section, ...]


@dataclass(frozen=True)
class _Snapshot:
    """One immutable view of a loaded index.

    Readers take the reference once and use only it, so a reload on another worker thread can
    never mix old scoring state with new sections.
    """

    location: str
    source: dict[str, Any]
    generated_at: str | None
    pages: dict[str, _Page]
    sections: list[_Section]
    postings: dict[str, list[tuple[int, int]]]
    idf: dict[str, float]
    doc_lengths: list[int]
    avgdl: float

    @property
    def version(self) -> str:
        """Source name and commit, e.g. bec_docs@127f371."""
        return f"{self.source.get('name') or 'docs'}@{self.source.get('git_commit') or 'unknown'}"

    def score(self, query_tokens: list[str]) -> dict[int, float]:
        """BM25 score of every section that shares a token with the query."""
        scores: dict[int, float] = defaultdict(float)
        for token, query_tf in Counter(query_tokens).items():
            idf = self.idf.get(token, 0.0)
            for section_index, tf in self.postings.get(token, ()):
                length_norm = 1 - _BM25_B + _BM25_B * self.doc_lengths[section_index] / self.avgdl
                scores[section_index] += (
                    query_tf * idf * (tf * (_BM25_K1 + 1)) / (tf + _BM25_K1 * length_norm)
                )
        return scores


def _read_pages(payload: dict[str, Any]) -> dict[str, _Page]:
    pages: dict[str, _Page] = {}
    for page_data in payload.get("pages", []):
        path = page_data["path"]
        page_sections = tuple(
            _Section(
                page=path,
                heading=section.get("heading"),
                level=int(section.get("level", 1)),
                text=section["text"].strip(),
            )
            for section in page_data.get("sections", [])
            if section.get("text", "").strip()
        )
        if page_sections:
            pages[path] = _Page(
                path=path,
                title=page_data["title"],
                url=page_data.get("url"),
                sections=page_sections,
            )
    return pages


def _build_snapshot(payload: dict[str, Any], location: str) -> _Snapshot:
    pages = _read_pages(payload)
    sections = [section for page in pages.values() for section in page.sections]
    if not sections:
        raise ValueError("docs index holds no searchable sections")

    postings: dict[str, list[tuple[int, int]]] = defaultdict(list)
    doc_lengths: list[int] = []
    for index, section in enumerate(sections):
        # the page title counts three times and the heading twice, so a query naming the topic
        # of a page ranks that page above pages that merely mention it
        title = pages[section.page].title
        heading = section.heading or ""
        tokens = _tokenize("\n".join([title, title, title, heading, heading, section.text]))
        doc_lengths.append(max(1, len(tokens)))
        for token, tf in Counter(tokens).items():
            postings[token].append((index, tf))

    doc_count = len(sections)
    idf = {
        token: math.log(1 + (doc_count - len(entries) + 0.5) / (len(entries) + 0.5))
        for token, entries in postings.items()
    }
    return _Snapshot(
        location=location,
        source=dict(payload.get("source") or {}),
        generated_at=payload.get("generated_at"),
        pages=pages,
        sections=sections,
        postings=dict(postings),
        idf=idf,
        doc_lengths=doc_lengths,
        avgdl=sum(doc_lengths) / len(doc_lengths),
    )


class DocsIndex:
    """Query side of the docs index: search, page lookup and a description for mcp_status."""

    def __init__(self, config: DocsIndexConfig | None = None) -> None:
        self.config = config or DocsIndexConfig()
        self._lock = threading.Lock()
        self._snapshot: _Snapshot | None = None
        self._error: str | None = None
        #: mtime of the configured path at the last load attempt
        self._seen_mtime: float | None = None
        #: monotonic time of the next download of the configured URL
        self._next_fetch = 0.0

    # ------------------------------------------------------------------ loading

    def _needs_reload(self) -> bool:
        location = self.config.location
        if self._snapshot is None:
            return True
        if location is None:
            return False
        if _is_url(location):
            return time.monotonic() >= self._next_fetch
        try:
            mtime = Path(location).expanduser().stat().st_mtime
        except OSError:
            mtime = None
        return mtime != self._seen_mtime

    def _load_configured(self, location: str) -> _Snapshot:
        if _is_url(location):
            # schedule the next download before trying, so an unreachable URL is retried after
            # a pause rather than on every call
            delay = min(_RETRY_AFTER_S, self.config.refresh_s)
            self._next_fetch = time.monotonic() + delay
            snapshot = _build_snapshot(
                _decode_index(_fetch(location, self.config.timeout_s)), location
            )
            self._next_fetch = time.monotonic() + self.config.refresh_s
            return snapshot

        path = Path(unquote(urlsplit(location).path) if location.startswith("file:") else location)
        path = path.expanduser()
        # stat before reading: if a rebuild replaces the file mid-load, the recorded mtime stays
        # older than the new file and the next call loads again
        try:
            self._seen_mtime = path.stat().st_mtime
        except OSError:
            self._seen_mtime = None
        return _build_snapshot(_decode_index(path.read_bytes()), str(path))

    def _reload(self) -> _Snapshot:
        location = self.config.location
        previous = self._snapshot
        if location is not None:
            try:
                snapshot = self._load_configured(location)
            except (OSError, ValueError) as exc:
                self._error = f"Could not load the docs index from {location}: {exc}."
            else:
                self._error = None
                return snapshot
            if previous is not None:
                # keep serving the last good index (or the bundled fallback already loaded)
                return previous
        return _build_snapshot(_decode_index(BUNDLED_INDEX.read_bytes()), "bundled")

    def _ensure_loaded(self) -> _Snapshot:
        snapshot = self._snapshot
        if snapshot is not None and not self._needs_reload():
            return snapshot
        with self._lock:
            if self._snapshot is None or self._needs_reload():
                self._snapshot = self._reload()
            return self._snapshot

    def _fallback_note(self, snapshot: _Snapshot) -> str | None:
        if self.config.location is None or snapshot.location != "bundled":
            return None
        return (
            f"The configured docs index could not be loaded; answers come from the docs bundled "
            f"with the server ({snapshot.version}, generated {snapshot.generated_at}), which may "
            f"be out of date."
        )

    # ------------------------------------------------------------------ queries

    def describe(self) -> dict[str, Any]:
        """What is loaded right now, without loading or downloading anything.

        Safe to call on the event loop, so mcp_status can report the docs index version.
        """
        snapshot = self._snapshot
        described: dict[str, Any] = {"configured": self.config.location or "bundled"}
        if snapshot is None:
            described["state"] = "not_loaded"
            return described
        fallback = self._fallback_note(snapshot)
        described.update(
            state="fallback" if fallback else "loaded",
            loaded_from=snapshot.location,
            version=snapshot.version,
            generated_at=snapshot.generated_at,
        )
        if fallback:
            described["fallback"] = fallback
        age_days = _age_days(snapshot.generated_at)
        if age_days is not None and age_days > _STALE_INDEX_AFTER_DAYS:
            described["warning"] = f"The docs index is {age_days} days old and may be out of date."
        if self._error:
            described["error"] = self._error
        return described

    def search(self, query: str, limit: int = 5) -> dict[str, Any]:
        """Return the best-matching documentation sections for a free-text query."""
        snapshot = self._ensure_loaded()
        query = query.strip()
        query_tokens = _tokenize(query)
        scores = snapshot.score(query_tokens)

        limit = min(max(1, int(limit)), _MAX_RESULTS)
        results: list[dict[str, Any]] = []
        seen_pages: set[str] = set()
        # one result per page, its best section: distinct pages are what an answer cites
        for section_index, score in sorted(scores.items(), key=lambda item: -item[1]):
            section = snapshot.sections[section_index]
            if section.page in seen_pages:
                continue
            seen_pages.add(section.page)
            page = snapshot.pages[section.page]
            results.append(
                {
                    "path": page.path,
                    "title": page.title,
                    "heading": section.heading,
                    "url": page.url,
                    "score": round(score, 3),
                    "snippet": _make_snippet(section.text, query_tokens),
                }
            )
            if len(results) == limit:
                break

        return {"query": query, "index": snapshot.version, "results": results}

    def toc(self) -> dict[str, Any]:
        """Every page's path and title, sorted by path."""
        snapshot = self._ensure_loaded()
        pages = [
            {"path": path, "title": snapshot.pages[path].title} for path in sorted(snapshot.pages)
        ]
        return {"index": snapshot.version, "pages": pages}

    def _resolve_page(self, snapshot: _Snapshot, path: str) -> _Page | None:
        wanted = path.strip()
        site_url = (snapshot.source.get("site_url") or "").rstrip("/")
        if site_url and wanted.startswith(site_url):
            wanted = wanted[len(site_url) :]
        wanted = wanted.split("#", 1)[0].strip().lstrip("/")
        name = snapshot.source.get("name")
        if name and wanted.startswith(f"{name}/"):
            wanted = wanted[len(name) + 1 :]
        if wanted.endswith(".html"):
            wanted = wanted[: -len(".html")]
        if not wanted.endswith(".md"):
            wanted = f"{wanted}.md"
        return snapshot.pages.get(wanted)

    def page(self, path: str, heading: str | None = None) -> dict[str, Any]:
        """Return one page, or one section of it when a heading is given.

        Raises:
            LookupError: with a message naming similar pages, or the page's headings.
        """
        snapshot = self._ensure_loaded()
        page = self._resolve_page(snapshot, path)
        if page is None:
            words = re.sub(r"[/._-]+", " ", path.removesuffix(".md").removesuffix(".html"))
            similar = [hit["path"] for hit in self.search(words, limit=5)["results"]]
            hint = f" Similar pages: {', '.join(similar)}." if similar else ""
            raise LookupError(
                f"No documentation page at {path!r}.{hint} Use a path from bec_docs_search."
            )

        headings = [section.heading for section in page.sections if section.heading]
        result: dict[str, Any] = {"path": page.path, "title": page.title, "url": page.url}
        if heading is not None:
            wanted = heading.strip().lower()
            matches = [s for s in page.sections if (s.heading or "").strip().lower() == wanted]
            if not matches:
                matches = [s for s in page.sections if wanted in (s.heading or "").lower()]
            if len(matches) != 1:
                raise LookupError(
                    f"{page.title!r} has no single section matching {heading!r}; its headings "
                    f"are: {'; '.join(headings)}."
                )
            result.update(heading=matches[0].heading, text=matches[0].text, truncated=False)
            return result

        parts = [
            (
                f"{'#' * section.level} {section.heading}\n{section.text}"
                if section.heading
                else section.text
            )
            for section in page.sections
        ]
        text = "\n\n".join(parts)
        result.update(
            headings=headings, text=text[:_PAGE_MAX_CHARS], truncated=len(text) > _PAGE_MAX_CHARS
        )
        return result
