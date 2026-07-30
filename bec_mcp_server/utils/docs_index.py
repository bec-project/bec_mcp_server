from __future__ import annotations

import json
import math
import re
import shutil
import tomllib
from collections import Counter
from datetime import datetime, timezone
from html import unescape
from pathlib import Path
from typing import Any

from qdrant_client import QdrantClient, models

_LINK_RE = re.compile(r"\[([^\]]+)\]\([^)]+\)")
_IMAGE_RE = re.compile(r"!\[([^\]]*)\]\([^)]+\)")
_TAG_RE = re.compile(r"<[^>]+>")
_HEADING_RE = re.compile(r"^(#{1,6})\s+(.*?)\s*$")
_INLINE_CODE_RE = re.compile(r"`([^`]+)`")
_ATTR_LIST_RE = re.compile(r"\s*\{[^{}\n]*\}\s*$")
_TOKEN_RE = re.compile(r"[A-Za-z0-9_][A-Za-z0-9_:-]*")
_QDRANT_COLLECTION = "bec_docs_sections"
_QDRANT_VECTOR = "terms"
_BM25_K1 = 1.2
_BM25_B = 0.75


def _strip_front_matter(text: str) -> str:
    if not text.startswith("---\n"):
        return text

    marker = "\n---\n"
    end = text.find(marker, 4)
    if end == -1:
        return text
    return text[end + len(marker) :]


def _strip_markdown_line(line: str) -> str:
    stripped = line.strip()
    if stripped.startswith("!!! ") or stripped.startswith("??? "):
        parts = stripped.split(maxsplit=2)
        if len(parts) == 3:
            return parts[2].strip('"')
        if len(parts) == 2:
            return parts[1]
        return ""

    if stripped.startswith("==="):
        return ""

    text = _IMAGE_RE.sub(r"\1", line)
    text = _LINK_RE.sub(r"\1", text)
    text = _INLINE_CODE_RE.sub(r"\1", text)
    text = _TAG_RE.sub(" ", text)
    text = _ATTR_LIST_RE.sub("", text)
    text = text.replace("|", " ")
    text = re.sub(r"^\s*[-*+]\s+", "", text)
    text = re.sub(r"^\s*\d+\.\s+", "", text)
    text = re.sub(r"^\s*>\s?", "", text)
    text = re.sub(r"\s+", " ", text)
    return unescape(text.strip())


def _clean_heading(text: str) -> str:
    return _strip_markdown_line(text).strip("# ").strip()


def _doc_url(site_url: str | None, relative_path: str) -> str | None:
    if not site_url:
        return None
    return f"{site_url.rstrip('/')}/{relative_path.removesuffix('.md')}.html"


def _tokenize(text: str) -> list[str]:
    return [token.lower() for token in _TOKEN_RE.findall(text)]


def _weighted_section_text(title: str, heading: str | None, text: str) -> str:
    parts = [title, title, title, heading or "", heading or "", text]
    return "\n".join(part for part in parts if part)


def _read_docs_project_config(repo_path: Path) -> tuple[Path, str | None]:
    zensical_path = repo_path / "zensical.toml"
    docs_dir = repo_path / "docs"
    site_url = None
    if zensical_path.exists():
        with zensical_path.open("rb") as stream:
            config = tomllib.load(stream)
        project = config.get("project", {})
        docs_dir = repo_path / project.get("docs_dir", "docs")
        site_url = project.get("site_url")
    return docs_dir, site_url


def parse_markdown_docs(repo_path: Path, *, site_url: str | None = None) -> list[dict[str, Any]]:
    """
    Parse markdown documentation files in a repository.

    Args:
        repo_path (Path): The path to the repository.
        site_url (str | None, optional): The base URL of the site. Defaults to None.

    Returns:
        list[dict[str, Any]]: A list of parsed markdown pages.
    """
    docs_dir, config_site_url = _read_docs_project_config(repo_path)
    resolved_site_url = site_url or config_site_url

    if not docs_dir.exists():
        raise FileNotFoundError(f"Docs directory not found: {docs_dir}")

    pages: list[dict[str, Any]] = []
    for path in sorted(docs_dir.rglob("*.md")):
        if path.is_dir():
            continue
        relative_parts = path.relative_to(docs_dir).parts
        if any(part.startswith(".") for part in relative_parts):
            continue
        pages.append(_parse_markdown_page(path, docs_dir=docs_dir, site_url=resolved_site_url))

    return pages


def _parse_markdown_page(path: Path, *, docs_dir: Path, site_url: str | None) -> dict[str, Any]:
    """
    Parse a single markdown page. The parsed content includes the page title, headings, and sections with their text content.

    Args:
        path (Path): The path to the markdown file.
        docs_dir (Path): The root directory of the documentation.
        site_url (str | None): The base URL of the site.

    Returns:
        dict[str, Any]: The parsed markdown page.
    """
    relative_path = path.relative_to(docs_dir).as_posix()
    text = _strip_front_matter(path.read_text(encoding="utf-8"))
    title = path.stem.replace("-", " ").title()
    headings: list[str] = []
    sections: list[dict[str, Any]] = []
    current_heading: str | None = None
    current_level = 1
    current_lines: list[str] = []
    in_code_block = False

    def flush_section() -> None:
        nonlocal title, current_heading, current_level, current_lines
        cleaned_lines = [_strip_markdown_line(line) for line in current_lines]
        section_text = "\n".join(line for line in cleaned_lines if line).strip()
        current_lines = []
        if not section_text:
            return
        sections.append({"heading": current_heading, "level": current_level, "text": section_text})

    for raw_line in text.splitlines():
        stripped = raw_line.strip()
        if stripped.startswith("```") or stripped.startswith("~~~"):
            in_code_block = not in_code_block
            continue

        if not in_code_block:
            heading_match = _HEADING_RE.match(raw_line)
            if heading_match:
                flush_section()
                current_level = len(heading_match.group(1))
                current_heading = _clean_heading(heading_match.group(2))
                if current_level == 1 and current_heading:
                    title = current_heading
                if current_heading:
                    headings.append(current_heading)
                continue

        current_lines.append(raw_line)

    flush_section()

    if not sections:
        sections.append({"heading": None, "level": 1, "text": ""})

    return {
        "path": relative_path,
        "title": title,
        "url": _doc_url(site_url, relative_path),
        "headings": headings,
        "sections": sections,
    }


def _build_qdrant_index(output_path: Path, pages: list[dict[str, Any]]) -> dict[str, Any]:
    """
    Given a list of parsed markdown pages, build a Qdrant index for
    searching the documentation. The index is stored in the specified output directory.

    Args:
        output_path (Path): The path to the output directory for the Qdrant index.
        pages (list[dict[str, Any]]): The list of parsed markdown pages.

    Returns:
        dict[str, Any]: The built Qdrant index metadata.
    """
    qdrant_root = (
        output_path.with_suffix(".qdrant") if output_path.suffix else Path(f"{output_path}.qdrant")
    )
    qdrant_storage_path = qdrant_root / "storage"
    metadata_path = qdrant_root / "metadata.json"

    sections: list[dict[str, Any]] = []
    for page in pages:
        for section in page.get("sections", []):
            section_text = section.get("text", "").strip()
            if not section_text:
                continue
            tokens = _tokenize(
                _weighted_section_text(page["title"], section.get("heading"), section_text)
            )
            if not tokens:
                continue
            sections.append(
                {
                    "path": page["path"],
                    "title": page["title"],
                    "heading": section.get("heading"),
                    "level": int(section.get("level", 1)),
                    "text": section_text,
                    "url": page.get("url"),
                    "tokens": tokens,
                }
            )

    if not sections:
        raise RuntimeError("No searchable sections found while building the BEC docs index")

    doc_count = len(sections)
    doc_frequency: Counter[str] = Counter()
    for section in sections:
        doc_frequency.update(set(section["tokens"]))

    vocabulary = {term: index for index, term in enumerate(sorted(doc_frequency))}
    idf = {
        term: math.log(1 + (doc_count - frequency + 0.5) / (frequency + 0.5))
        for term, frequency in doc_frequency.items()
    }
    average_doc_length = sum(len(section["tokens"]) for section in sections) / doc_count

    shutil.rmtree(qdrant_root, ignore_errors=True)
    qdrant_storage_path.mkdir(parents=True, exist_ok=True)
    client = QdrantClient(path=str(qdrant_storage_path))
    client.create_collection(
        collection_name=_QDRANT_COLLECTION,
        vectors_config={},
        sparse_vectors_config={_QDRANT_VECTOR: models.SparseVectorParams()},
    )

    points = []
    for point_id, section in enumerate(sections, start=1):
        token_counts: Counter[str] = Counter(section["tokens"])
        doc_length = len(section["tokens"])
        sparse_items = []
        for term, tf in token_counts.items():
            bm25_weight = idf[term] * (
                (tf * (_BM25_K1 + 1))
                / (tf + _BM25_K1 * (1 - _BM25_B + _BM25_B * doc_length / average_doc_length))
            )
            sparse_items.append((vocabulary[term], float(bm25_weight)))

        sparse_items.sort(key=lambda item: item[0])
        points.append(
            models.PointStruct(
                id=point_id,
                vector={
                    _QDRANT_VECTOR: models.SparseVector(
                        indices=[index for index, _ in sparse_items],
                        values=[value for _, value in sparse_items],
                    )
                },
                payload={
                    "path": section["path"],
                    "title": section["title"],
                    "heading": section["heading"],
                    "level": section["level"],
                    "text": section["text"],
                    "url": section["url"],
                },
            )
        )

    for start in range(0, len(points), 256):
        client.upsert(collection_name=_QDRANT_COLLECTION, points=points[start : start + 256])

    metadata = {
        "backend": "qdrant-bm25",
        "collection_name": _QDRANT_COLLECTION,
        "vector_name": _QDRANT_VECTOR,
        "avgdl": average_doc_length,
        "k1": _BM25_K1,
        "b": _BM25_B,
        "section_count": len(sections),
        "vocabulary": vocabulary,
        "idf": idf,
    }
    metadata_path.write_text(
        json.dumps(metadata, indent=2, ensure_ascii=True) + "\n", encoding="utf-8"
    )
    return {
        "enabled": True,
        "backend": "qdrant-bm25",
        "qdrant_index_path": str(qdrant_root),
        "section_count": len(sections),
    }


def build_docs_index(
    repo_path: Path, output_path: Path, *, site_url: str | None = None
) -> dict[str, Any]:
    pages = parse_markdown_docs(repo_path, site_url=site_url)
    search_backend = _build_qdrant_index(output_path, pages)
    payload = {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "source_repo": str(repo_path.resolve()),
        "page_count": len(pages),
        "search_backend": search_backend,
        "pages": pages,
    }
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(
        json.dumps(payload, indent=2, ensure_ascii=True) + "\n", encoding="utf-8"
    )
    return payload
