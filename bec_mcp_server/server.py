from __future__ import annotations

import argparse
import os

from mcp.server.fastmcp import FastMCP
from mcp.types import ToolAnnotations

from bec_mcp_server.client import BecConnection
from bec_mcp_server.docs_search import BecDocumentation


def _env_str(name: str, default: str) -> str:
    return os.environ.get(name, default)


def _env_int(name: str, default: int) -> int:
    return int(os.environ.get(name, str(default)))


READ_ONLY_TOOL = ToolAnnotations(
    readOnlyHint=True,
    destructiveHint=False,
    idempotentHint=True,
)


def build_server(
    *,
    host: str = "127.0.0.1",
    port: int = 8000,
    streamable_http_path: str = "/mcp",
    connect_on_startup: bool = True,
) -> FastMCP:
    mcp = FastMCP(
        "bec",
        host=host,
        port=port,
        streamable_http_path=streamable_http_path,
    )
    bec = BecConnection()
    docs = BecDocumentation()
    if connect_on_startup:
        bec.connect()

    @mcp.tool(
        description="Safe to run. Read-only. Check Redis connectivity for the configured BEC instance.",
        annotations=READ_ONLY_TOOL,
    )
    def bec_ping() -> dict:
        """Check Redis connectivity for the configured BEC instance."""
        return bec.ping()

    @mcp.tool(
        description="Safe to run. Read-only. Return a compact overview of services, devices, scans, and queues.",
        annotations=READ_ONLY_TOOL,
    )
    def bec_summary() -> dict:
        """Return a compact overview of services, devices, scans, and queues."""
        return bec.summary()

    @mcp.tool(
        description="Safe to run. Read-only. List currently published BEC service status messages.",
        annotations=READ_ONLY_TOOL,
    )
    def bec_services() -> list[dict]:
        """List currently published BEC service status messages."""
        return bec.services()

    @mcp.tool(
        description="Safe to run. Read-only. List known BEC devices with a compact summary for each entry.",
        annotations=READ_ONLY_TOOL,
    )
    def bec_devices() -> dict:
        """List known BEC devices with a compact summary for each entry."""
        return bec.devices()

    @mcp.tool(
        description="Safe to run. Read-only. Return the full BEC device config payload for one device.",
        annotations=READ_ONLY_TOOL,
    )
    def bec_device_config(device_name: str) -> dict:
        """Return the full BEC device config payload for one device."""
        return bec.device_config(device_name)

    @mcp.tool(
        description="Safe to run. Read-only. List available scans published by the BEC scan server with optional filtering.",
        annotations=READ_ONLY_TOOL,
    )
    def bec_scans(
        prefix: str | None = None,
        base_class: str | None = None,
        include_internal: bool = False,
    ) -> dict:
        """List available scans published by the BEC scan server with optional filtering."""
        return bec.scans(
            prefix=prefix,
            base_class=base_class,
            include_internal=include_internal,
        )

    @mcp.tool(
        description="Safe to run. Read-only. Return the full scan metadata payload for one scan.",
        annotations=READ_ONLY_TOOL,
    )
    def bec_scan_details(scan_name: str) -> dict:
        """Return the full scan metadata payload for one scan."""
        return bec.scan_details(scan_name)

    @mcp.tool(
        description="Safe to run. Read-only. Return the current scan interlock configuration and watched states.",
        annotations=READ_ONLY_TOOL,
    )
    def bec_scan_interlock() -> dict:
        """Return the current scan interlock configuration and watched states."""
        return bec.scan_interlock()

    @mcp.tool(
        description="Safe to run. Read-only. Return a compact summary of the current BEC scan queues.",
        annotations=READ_ONLY_TOOL,
    )
    def bec_queue() -> dict:
        """Return a compact summary of the current BEC scan queues."""
        return bec.queue()

    @mcp.tool(
        description="Safe to run. Read-only. Return the current documentation index configuration and load status.",
        annotations=READ_ONLY_TOOL,
    )
    def bec_docs_status() -> dict:
        """Return the current documentation index configuration and load status."""
        return docs.status()

    @mcp.tool(
        description="Safe to run. Read-only. Search the local BEC documentation index for relevant sections.",
        annotations=READ_ONLY_TOOL,
    )
    def bec_docs_search(query: str, limit: int = 5) -> dict:
        """Search the local BEC documentation index for relevant sections."""
        return docs.search(query=query, limit=limit)

    @mcp.tool(
        description="Safe to run. Read-only. Return the parsed text content for one documentation page.",
        annotations=READ_ONLY_TOOL,
    )
    def bec_docs_page(path: str) -> dict:
        """Return the parsed text content for one documentation page."""
        return docs.page(path=path)

    return mcp


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Run the BEC MCP server.")
    parser.add_argument(
        "--transport",
        choices=["streamable-http", "stdio"],
        default=_env_str("BEC_MCP_TRANSPORT", "streamable-http"),
        help="MCP transport to use.",
    )
    parser.add_argument(
        "--host",
        default=_env_str("BEC_MCP_HOST", "127.0.0.1"),
        help="Host for streamable HTTP transport.",
    )
    parser.add_argument(
        "--port",
        type=int,
        default=_env_int("BEC_MCP_PORT", 8000),
        help="Port for streamable HTTP transport.",
    )
    parser.add_argument(
        "--path",
        default=_env_str("BEC_MCP_PATH", "/mcp"),
        help="HTTP path for streamable HTTP transport.",
    )
    parser.add_argument(
        "--lazy-connect",
        action="store_true",
        help="Defer creating the BEC client until the first MCP tool call.",
    )
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> None:
    args = parse_args(argv)
    mcp = build_server(
        host=args.host,
        port=args.port,
        streamable_http_path=args.path,
        connect_on_startup=not args.lazy_connect,
    )
    mcp.run(transport=args.transport)
