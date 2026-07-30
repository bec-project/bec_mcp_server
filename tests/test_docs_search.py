from __future__ import annotations

import json
from types import SimpleNamespace

from bec_mcp_server.docs_search import BecDocumentation, DocsSearchConfig
from bec_mcp_server import docs_search
from bec_mcp_server.utils import docs_index as docs_index_utils
from bec_mcp_server.utils.docs_index import build_docs_index


def _install_fake_qdrant(monkeypatch, *, query_points_result=None):
    class FakeSparseVector:
        def __init__(self, indices, values):
            self.indices = indices
            self.values = values

    class FakePointStruct:
        def __init__(self, id, vector, payload):
            self.id = id
            self.vector = vector
            self.payload = payload

    class FakeSparseVectorParams:
        pass

    class FakeQdrantClient:
        def __init__(self, path):
            self.path = path

        def create_collection(self, **kwargs):
            self.collection_kwargs = kwargs

        def upsert(self, **kwargs):
            self.upsert_kwargs = kwargs

        def query_points(self, **kwargs):
            if query_points_result is None:
                return SimpleNamespace(points=[])
            return query_points_result

    fake_models = SimpleNamespace(
        PointStruct=FakePointStruct,
        SparseVector=FakeSparseVector,
        SparseVectorParams=FakeSparseVectorParams,
    )
    monkeypatch.setattr(docs_index_utils, "QdrantClient", FakeQdrantClient)
    monkeypatch.setattr(docs_index_utils, "models", fake_models)
    monkeypatch.setattr(docs_search, "QdrantClient", FakeQdrantClient)
    monkeypatch.setattr(docs_search, "models", fake_models)


def test_build_docs_index_and_search(tmp_path, monkeypatch):
    query_points_result = SimpleNamespace(
        points=[
            SimpleNamespace(
                payload={
                    "path": "how-to/gui.md",
                    "title": "Dock Area Profiles",
                    "heading": "Switch profiles",
                    "text": "Use the Dock Area profile picker to switch layouts.",
                    "url": "https://bec.example.org/docs/how-to/gui.html",
                },
                score=4.2,
            )
        ]
    )
    _install_fake_qdrant(monkeypatch, query_points_result=query_points_result)

    repo = tmp_path / "bec_docs"
    docs_dir = repo / "docs"
    docs_dir.mkdir(parents=True)
    (repo / "zensical.toml").write_text(
        """
[project]
site_url = "https://bec.example.org/docs"
docs_dir = "docs"
""".strip(),
        encoding="utf-8",
    )
    (docs_dir / "index.md").write_text(
        """
---
hide:
  - toc
---

# Welcome to BEC

BEC coordinates scans and devices.
""".strip(),
        encoding="utf-8",
    )
    guide_path = docs_dir / "how-to" / "gui.md"
    guide_path.parent.mkdir(parents=True)
    guide_path.write_text(
        """
# Dock Area Profiles

## Switch profiles

Use the Dock Area profile picker to switch layouts.
""".strip(),
        encoding="utf-8",
    )
    index_path = tmp_path / "bec-docs-index.json"

    payload = build_docs_index(repo, index_path)
    docs = BecDocumentation(DocsSearchConfig(index_path=index_path))
    results = docs.search("dock area profile", limit=3)

    assert payload["page_count"] == 2
    assert results["count"] == 1
    assert results["results"][0]["path"] == "how-to/gui.md"
    assert results["results"][0]["title"] == "Dock Area Profiles"
    assert results["results"][0]["url"] == "https://bec.example.org/docs/how-to/gui.html"


def test_page_returns_text_from_prebuilt_index(tmp_path, monkeypatch):
    _install_fake_qdrant(monkeypatch)

    repo = tmp_path / "bec_docs"
    docs_dir = repo / "docs"
    docs_dir.mkdir(parents=True)
    page_path = docs_dir / "learn" / "widgets.md"
    page_path.parent.mkdir(parents=True)
    page_path.write_text(
        """
# Widget guide

## Remote control

Use [RPC](rpc.md) to control widgets.
""".strip(),
        encoding="utf-8",
    )
    index_path = tmp_path / "bec-docs-index.json"

    build_docs_index(repo, index_path, site_url="https://bec.example.org/docs")
    docs = BecDocumentation(DocsSearchConfig(index_path=index_path))
    page = docs.page("learn/widgets.md")

    assert page["found"] is True
    assert page["title"] == "Widget guide"
    assert "Use RPC to control widgets." in page["text"]
    assert page["headings"] == ["Widget guide", "Remote control"]


def test_status_and_search_report_missing_index(tmp_path):
    index_path = tmp_path / "missing-index.json"
    docs = BecDocumentation(DocsSearchConfig(index_path=index_path))

    status = docs.status()
    results = docs.search("queue")
    page = docs.page("learn/widgets.md")

    assert status["index_exists"] is False
    assert "Build it with bec-mcp-build-docs-index" in status["message"]
    assert results["index_available"] is False
    assert page["index_available"] is False


def test_runtime_reads_existing_json_index(tmp_path, monkeypatch):
    index_path = tmp_path / "bec-docs-index.json"
    index_path.write_text(
        json.dumps(
            {
                "generated_at": "2026-07-24T00:00:00+00:00",
                "source_repo": "/tmp/bec_docs",
                "page_count": 1,
                "search_backend": {"backend": "qdrant-bm25"},
                "pages": [
                    {
                        "path": "references/api.md",
                        "title": "API reference",
                        "headings": ["API reference", "Queue"],
                        "url": "https://bec.example.org/docs/references/api.html",
                        "sections": [
                            {
                                "heading": "Queue",
                                "level": 2,
                                "text": "The queue exposes pending scan requests.",
                            }
                        ],
                    }
                ],
            }
        ),
        encoding="utf-8",
    )
    qdrant_dir = index_path.with_suffix(".qdrant")
    (qdrant_dir / "storage").mkdir(parents=True)
    (qdrant_dir / "metadata.json").write_text(
        json.dumps(
            {
                "collection_name": "bec_docs_sections",
                "vector_name": "terms",
                "vocabulary": {"pending": 1, "queue": 3},
                "idf": {"pending": 0.9, "queue": 1.4},
            }
        ),
        encoding="utf-8",
    )
    query_points_result = SimpleNamespace(
        points=[
            SimpleNamespace(
                payload={
                    "path": "references/api.md",
                    "title": "API reference",
                    "heading": "Queue",
                    "text": "The queue exposes pending scan requests.",
                    "url": "https://bec.example.org/docs/references/api.html",
                },
                score=2.3456,
            )
        ]
    )
    _install_fake_qdrant(monkeypatch, query_points_result=query_points_result)

    docs = BecDocumentation(DocsSearchConfig(index_path=index_path))
    status = docs.status()
    results = docs.search("pending queue")

    assert status["generated_at"] == "2026-07-24T00:00:00+00:00"
    assert status["source_repo"] == "/tmp/bec_docs"
    assert results["results"][0]["path"] == "references/api.md"


def test_build_docs_index_writes_qdrant_metadata_with_fake_client(tmp_path, monkeypatch):
    _install_fake_qdrant(monkeypatch)

    repo = tmp_path / "bec_docs"
    docs_dir = repo / "docs"
    docs_dir.mkdir(parents=True)
    (docs_dir / "index.md").write_text(
        """
# Welcome to BEC

BEC coordinates scans and devices.
""".strip(),
        encoding="utf-8",
    )
    index_path = tmp_path / "bec-docs-index.json"

    payload = build_docs_index(repo, index_path)
    metadata_path = index_path.with_suffix(".qdrant") / "metadata.json"

    assert payload["search_backend"]["backend"] == "qdrant-bm25"
    assert metadata_path.exists()
    metadata = json.loads(metadata_path.read_text(encoding="utf-8"))
    assert metadata["collection_name"] == "bec_docs_sections"
    assert metadata["section_count"] == 1


def test_search_uses_qdrant_when_index_is_available(tmp_path, monkeypatch):
    index_path = tmp_path / "bec-docs-index.json"
    index_path.write_text(
        json.dumps(
            {
                "generated_at": "2026-07-24T00:00:00+00:00",
                "source_repo": "/tmp/bec_docs",
                "page_count": 1,
                "search_backend": {"backend": "qdrant-bm25"},
                "pages": [
                    {
                        "path": "references/api.md",
                        "title": "API reference",
                        "headings": ["API reference", "Queue"],
                        "url": "https://bec.example.org/docs/references/api.html",
                        "sections": [
                            {
                                "heading": "Queue",
                                "level": 2,
                                "text": "The queue exposes pending scan requests.",
                            }
                        ],
                    }
                ],
            }
        ),
        encoding="utf-8",
    )
    qdrant_dir = index_path.with_suffix(".qdrant")
    (qdrant_dir / "storage").mkdir(parents=True)
    (qdrant_dir / "metadata.json").write_text(
        json.dumps(
            {
                "collection_name": "bec_docs_sections",
                "vector_name": "terms",
                "vocabulary": {"pending": 1, "queue": 3},
                "idf": {"pending": 0.9, "queue": 1.4},
            }
        ),
        encoding="utf-8",
    )

    class FakeSparseVector:
        def __init__(self, indices, values):
            self.indices = indices
            self.values = values

    class FakeQdrantClient:
        def __init__(self, path):
            self.path = path

        def query_points(self, **kwargs):
            return SimpleNamespace(
                points=[
                    SimpleNamespace(
                        payload={
                            "path": "references/api.md",
                            "title": "API reference",
                            "heading": "Queue",
                            "text": "The queue exposes pending scan requests.",
                            "url": "https://bec.example.org/docs/references/api.html",
                        },
                        score=2.3456,
                    )
                ]
            )

    monkeypatch.setattr(docs_search, "QdrantClient", FakeQdrantClient)
    monkeypatch.setattr(docs_search, "models", SimpleNamespace(SparseVector=FakeSparseVector))

    docs = BecDocumentation(DocsSearchConfig(index_path=index_path))
    results = docs.search("pending queue")

    assert results["search_backend"] == "qdrant"
    assert results["results"][0]["path"] == "references/api.md"
    assert results["results"][0]["score"] == 2.3456
