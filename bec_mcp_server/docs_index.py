from __future__ import annotations

import argparse
from pathlib import Path

from bec_mcp_server.utils.docs_index import build_docs_index


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Build the BEC documentation search index.")
    parser.add_argument(
        "docs_repo",
        help="Path to the bec_docs checkout.",
    )
    parser.add_argument(
        "--output",
        type=Path,
        default=Path(__file__).resolve().parent / "data" / "bec_docs_index.json",
        help="Output path for the generated JSON index.",
    )
    parser.add_argument(
        "--site-url",
        default=None,
        help="Optional site URL override used to generate documentation links.",
    )
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> None:
    args = parse_args(argv)
    payload = build_docs_index(
        Path(args.docs_repo).expanduser(),
        args.output.expanduser(),
        site_url=args.site_url,
    )
    print(
        f"Wrote docs index with {payload['page_count']} pages to {args.output.expanduser()}"
    )


if __name__ == "__main__":
    main()
