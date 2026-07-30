from __future__ import annotations

import json
import os
import re
import threading
from collections import Counter
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from qdrant_client import QdrantClient, models


_TOKEN_RE = re.compile(r"[A-Za-z0-9_][A-Za-z0-9_:-]*")


def _tokenize(text: str) -> list[str]:
    return [token.lower() for token in _TOKEN_RE.findall(text)]


def _make_snippet(text: str, query_tokens: list[str], max_chars: int = 240) -> str:
    compact = re.sub(r"\s+", " ", text).strip()
    if not compact:
        return ""

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


def _default_index_path() -> Path:
    return Path(__file__).resolve().parent / "data" / "bec_docs_index.json"


def _qdrant_root(index_path: Path) -> Path:
    if index_path.suffix:
        return index_path.with_suffix(".qdrant")
    return Path(f"{index_path}.qdrant")


def _qdrant_storage_path(index_path: Path) -> Path:
    return _qdrant_root(index_path) / "storage"


def _qdrant_metadata_path(index_path: Path) -> Path:
    return _qdrant_root(index_path) / "metadata.json"


@dataclass(frozen=True)
class DocSection:
    path: str
    title: str
    heading: str | None
    level: int
    text: str
    url: str | None


@dataclass(frozen=True)
class DocPage:
    path: str
    title: str
    text: str
    headings: list[str]
    url: str | None


@dataclass(frozen=True)
class DocsSearchConfig:
    index_path: Path

    @classmethod
    def from_env(cls) -> "DocsSearchConfig":
        index_path = Path(os.environ.get("BEC_DOCS_INDEX_PATH", _default_index_path())).expanduser()
        return cls(index_path=index_path)


class BecDocumentation:
    def __init__(self, config: DocsSearchConfig | None = None) -> None:
        self.config = config or DocsSearchConfig.from_env()
        self._lock = threading.Lock()
        self._sections: list[DocSection] | None = None
        self._pages: dict[str, DocPage] | None = None
        self._loaded_at: str | None = None
        self._generated_at: str | None = None
        self._source_repo: str | None = None
        self._search_backend: dict[str, Any] | None = None
        self._qdrant_client: Any | None = None
        self._qdrant_metadata: dict[str, Any] | None = None

    def _load_index(self) -> None:
        with self.config.index_path.open("r", encoding="utf-8") as stream:
            payload = json.load(stream)

        pages: dict[str, DocPage] = {}
        sections: list[DocSection] = []
        for page_data in payload.get("pages", []):
            path = page_data["path"]
            title = page_data["title"]
            url = page_data.get("url")
            sections_data = page_data.get("sections", [])
            headings = page_data.get("headings") or [
                section.get("heading") for section in sections_data if section.get("heading")
            ]
            text = "\n\n".join(section.get("text", "") for section in sections_data if section.get("text"))
            pages[path] = DocPage(path=path, title=title, text=text, headings=headings, url=url)

            for section in sections_data:
                section_text = section.get("text", "").strip()
                if not section_text:
                    continue
                sections.append(
                    DocSection(
                        path=path,
                        title=title,
                        heading=section.get("heading"),
                        level=int(section.get("level", 1)),
                        text=section_text,
                        url=url,
                    )
                )

        self._sections = sections
        self._pages = pages
        self._loaded_at = datetime.now(timezone.utc).isoformat()
        self._generated_at = payload.get("generated_at")
        self._source_repo = payload.get("source_repo")
        self._search_backend = payload.get("search_backend") or {"backend": "qdrant-bm25"}

    def _ensure_loaded(self) -> None:
        if self._sections is not None and self._pages is not None:
            return

        with self._lock:
            if self._sections is not None and self._pages is not None:
                return
            self._load_index()

    @property
    def index_exists(self) -> bool:
        return self.config.index_path.exists()

    @property
    def sections(self) -> list[DocSection]:
        self._ensure_loaded()
        return self._sections or []

    @property
    def pages(self) -> dict[str, DocPage]:
        self._ensure_loaded()
        return self._pages or {}

    def _load_qdrant_metadata(self) -> dict[str, Any] | None:
        if self._qdrant_metadata is not None:
            return self._qdrant_metadata

        metadata_path = _qdrant_metadata_path(self.config.index_path)
        if not metadata_path.exists():
            return None

        with metadata_path.open("r", encoding="utf-8") as stream:
            self._qdrant_metadata = json.load(stream)
        return self._qdrant_metadata

    def _qdrant_ready(self) -> bool:
        return _qdrant_metadata_path(self.config.index_path).exists() and _qdrant_storage_path(
            self.config.index_path
        ).exists()

    def _get_qdrant_client(self) -> Any:
        if self._qdrant_client is None:
            self._qdrant_client = QdrantClient(path=str(_qdrant_storage_path(self.config.index_path)))
        return self._qdrant_client

    def _search_qdrant(self, query_tokens: list[str], limit: int) -> list[dict[str, Any]]:
        if not self._qdrant_ready():
            raise RuntimeError("Qdrant docs index not found. Rebuild it with bec-mcp-build-docs-index.")
        if not query_tokens:
            return []

        metadata = self._load_qdrant_metadata()
        if metadata is None:
            raise RuntimeError("Qdrant docs metadata not found. Rebuild it with bec-mcp-build-docs-index.")

        vocabulary = metadata.get("vocabulary", {})
        idf = metadata.get("idf", {})
        query_counts: Counter[str] = Counter(query_tokens)
        sparse_items = []
        for token, tf in query_counts.items():
            index = vocabulary.get(token)
            if index is None:
                continue
            sparse_items.append((int(index), float(tf) * float(idf.get(token, 1.0))))

        if not sparse_items:
            return []

        sparse_items.sort(key=lambda item: item[0])

        response = self._get_qdrant_client().query_points(
            collection_name=metadata.get("collection_name", "bec_docs_sections"),
            using=metadata.get("vector_name", "terms"),
            query=models.SparseVector(
                indices=[index for index, _ in sparse_items],
                values=[value for _, value in sparse_items],
            ),
            limit=max(1, limit),
            with_payload=True,
        )

        points = getattr(response, "points", response)
        results = []
        for point in points:
            payload = getattr(point, "payload", None) or {}
            results.append(
                {
                    "path": payload.get("path"),
                    "title": payload.get("title"),
                    "heading": payload.get("heading"),
                    "url": payload.get("url"),
                    "score": round(float(getattr(point, "score", 0.0)), 4),
                    "snippet": _make_snippet(payload.get("text", ""), query_tokens),
                }
            )
        return results

    def status(self) -> dict[str, Any]:
        status = {
            "index_path": str(self.config.index_path),
            "index_exists": self.index_exists,
        }
        if not self.index_exists:
            status["message"] = "Documentation index not found. Build it with bec-mcp-build-docs-index."
            return status

        self._ensure_loaded()
        status.update(
            {
                "loaded_at": self._loaded_at,
                "generated_at": self._generated_at,
                "source_repo": self._source_repo,
                "page_count": len(self.pages),
                "section_count": len(self.sections),
                "search_backend": self._search_backend,
                "qdrant_index_path": str(_qdrant_root(self.config.index_path)),
                "qdrant_index_exists": self._qdrant_ready(),
            }
        )
        return status

    def search(self, query: str, limit: int = 5) -> dict[str, Any]:
        normalized_query = query.strip()
        if not self.index_exists:
            return {
                "query": normalized_query,
                "count": 0,
                "results": [],
                "index_available": False,
                "message": "Documentation index not found. Build it with bec-mcp-build-docs-index.",
            }

        if not normalized_query:
            return {"query": query, "count": 0, "results": [], "index_available": True}

        query_tokens = _tokenize(normalized_query)
        try:
            qdrant_results = self._search_qdrant(query_tokens=query_tokens, limit=limit)
        except RuntimeError as exc:
            return {
                "query": normalized_query,
                "count": 0,
                "results": [],
                "index_available": False,
                "search_backend": "qdrant",
                "message": str(exc),
            }

        return {
            "query": normalized_query,
            "count": len(qdrant_results),
            "results": qdrant_results,
            "index_available": True,
            "search_backend": "qdrant",
        }

    def page(self, path: str) -> dict[str, Any]:
        normalized_path = path.strip().lstrip("/")
        if normalized_path.endswith(".html"):
            normalized_path = f"{normalized_path[:-5]}.md"

        if not self.index_exists:
            return {
                "found": False,
                "path": normalized_path,
                "index_available": False,
                "message": "Documentation index not found. Build it with bec-mcp-build-docs-index.",
            }

        page = self.pages.get(normalized_path)
        if page is None:
            return {
                "found": False,
                "path": normalized_path,
                "index_available": True,
                "available_paths": sorted(self.pages.keys())[:200],
            }

        return {
            "found": True,
            "path": page.path,
            "title": page.title,
            "url": page.url,
            "headings": page.headings,
            "text": page.text,
            "index_available": True,
        }
