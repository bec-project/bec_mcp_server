from __future__ import annotations

import argparse
import os
import re
from importlib import metadata

from mcp.server.fastmcp import FastMCP
from mcp.server.transport_security import TransportSecuritySettings
from mcp.types import ToolAnnotations

from bec_mcp_server.client import BecConnection
from bec_mcp_server.docs.search import DocsIndex, DocsIndexConfig, DocsUnavailableError


def _env_str(name: str, default: str) -> str:
    return os.environ.get(name, default)


def _env_int(name: str, default: int) -> int:
    return int(os.environ.get(name, str(default)))


READ_ONLY_TOOL = ToolAnnotations(readOnlyHint=True, destructiveHint=False, idempotentHint=True)

#: Host and Origin values the SDK accepts on a loopback bind; --allowed-hosts adds to these
_LOOPBACK_HOSTS = ("127.0.0.1:*", "localhost:*", "[::1]:*")
_LOOPBACK_ORIGINS = ("http://127.0.0.1:*", "http://localhost:*", "http://[::1]:*")
_PORT_SUFFIX = re.compile(r"(?::\d+|:\*)$")


def server_version() -> str:
    """The installed version of this package."""
    try:
        return metadata.version("bec-mcp-server")
    except metadata.PackageNotFoundError:
        return "0+unknown"


def host_patterns(entries: list[str]) -> list[str]:
    """Host header values for --allowed-hosts entries.

    ``name`` allows that name on any port; ``name:port`` and ``name:*`` are used as given.
    """
    patterns: list[str] = []
    for entry in (e.strip() for e in entries):
        if not entry:
            continue
        if _PORT_SUFFIX.search(entry.rsplit("]", 1)[-1]):
            patterns.append(entry)
        else:
            patterns.extend([entry, f"{entry}:*"])
    return patterns


def transport_security(host: str, allowed_hosts: list[str]) -> TransportSecuritySettings | None:
    """DNS-rebinding protection with extra Host values, or None to keep the SDK's default.

    The SDK only accepts loopback names on a loopback bind, so an agent in a container that
    reaches the host as host.docker.internal or host.containers.internal gets 421 Invalid Host
    header unless that name is listed.
    """
    extra = host_patterns(allowed_hosts)
    if not extra:
        return None
    hosts = list(_LOOPBACK_HOSTS)
    bind = host.strip("[]").lower()
    if bind not in ("127.0.0.1", "localhost", "::1", "0.0.0.0", "::", ""):
        hosts.extend(host_patterns([f"[{bind}]" if ":" in bind else bind]))
    hosts.extend(h for h in extra if h not in hosts)
    return TransportSecuritySettings(
        enable_dns_rebinding_protection=True,
        allowed_hosts=hosts,
        allowed_origins=list(_LOOPBACK_ORIGINS),
    )


def build_server(  # pylint: disable=too-many-locals,too-many-arguments  # one local per tool
    *,
    host: str = "127.0.0.1",
    port: int = 8000,
    streamable_http_path: str = "/mcp",
    connect_on_startup: bool = True,
    docs_index: str | None = None,
    allowed_hosts: list[str] | None = None,
) -> FastMCP:
    """Create the MCP server and register the read-only BEC and documentation tools.

    Args:
        docs_index: Path or http(s) URL of the bec_docs search index; None downloads the latest
            one bec_docs publishes to gitea.psi.ch.
        allowed_hosts: Extra Host header values the HTTP transport accepts, e.g.
            ``host.docker.internal`` for an agent in a container.
    """
    mcp = FastMCP(
        "bec",
        host=host,
        port=port,
        streamable_http_path=streamable_http_path,
        transport_security=transport_security(host, allowed_hosts or []),
    )
    # FastMCP in mcp 1.x takes no version and would report the mcp SDK's as serverInfo.version.
    mcp._mcp_server.version = server_version()  # pylint: disable=protected-access
    bec = BecConnection()
    docs = DocsIndex(DocsIndexConfig(location=docs_index))
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
        prefix: str | None = None, base_class: str | None = None, include_internal: bool = False
    ) -> dict:
        """List available scans published by the BEC scan server with optional filtering."""
        return bec.scans(prefix=prefix, base_class=base_class, include_internal=include_internal)

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
        description="Safe to run. Read-only. Search the official BEC documentation for a question "
        "or keywords and return the best-matching pages with their URLs; without a query, list "
        "every page.",
        annotations=READ_ONLY_TOOL,
    )
    def bec_docs_search(query: str = "", limit: int = 5) -> dict:
        """Search the BEC documentation index; an empty query returns its table of contents."""
        try:
            found = docs.search(query, limit) if query.strip() else docs.toc()
        except DocsUnavailableError as exc:
            return {"available": False, "message": str(exc), "docs_index": docs.describe()}
        return {**found, "docs_index": docs.describe()}

    @mcp.tool(
        description="Safe to run. Read-only. Return the text of one BEC documentation page by its "
        "path or URL, or of one section when a heading is given.",
        annotations=READ_ONLY_TOOL,
    )
    def bec_docs_page(path: str, heading: str = "") -> dict:
        """Return one documentation page, or one section of it."""
        try:
            return {"found": True, **docs.page(path, heading or None)}
        except (LookupError, DocsUnavailableError) as exc:
            return {"found": False, "path": path, "message": str(exc)}

    return mcp


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    """Parse command-line arguments, taking defaults from BEC_MCP_* environment variables."""
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
        "--allowed-hosts",
        type=lambda value: [entry.strip() for entry in value.split(",") if entry.strip()],
        default=[
            entry.strip()
            for entry in os.environ.get("BEC_MCP_ALLOWED_HOSTS", "").split(",")
            if entry.strip()
        ],
        metavar="HOSTS",
        help="Extra Host header values HTTP accepts, comma-separated, e.g. "
        "'host.docker.internal'; a name without port matches any port.",
    )
    parser.add_argument(
        "--lazy-connect",
        action="store_true",
        help="Defer creating the BEC client until the first MCP tool call.",
    )
    parser.add_argument(
        "--docs-index",
        default=os.environ.get("BEC_MCP_DOCS_INDEX") or None,
        metavar="PATH_OR_URL",
        help="Docs search index built from bec_docs: a .json or .json.gz file, or an http(s) URL "
        "(default: the latest index bec_docs publishes to gitea.psi.ch).",
    )
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> None:
    """Entry point of the bec-mcp-server console script."""
    args = parse_args(argv)
    mcp = build_server(
        host=args.host,
        port=args.port,
        streamable_http_path=args.path,
        connect_on_startup=not args.lazy_connect,
        docs_index=args.docs_index,
        allowed_hosts=args.allowed_hosts,
    )
    mcp.run(transport=args.transport)
