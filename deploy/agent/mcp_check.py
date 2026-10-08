"""Check that an MCP client can reach the BEC MCP server with nothing but a URL and a token.

Reads BEC_MCP_URL and BEC_MCP_TOKEN from the environment, then:

1. sends an initialize request without the token and expects 401, so a server that serves
   without a token fails the check;
2. sends the same request with the token and expects 200;
3. opens an MCP session, lists the tools, and calls ``mcp_status``, ``bec_ping`` and
   ``bec_status``.

Exit status 0 means an agent with the same URL and token will work. It runs in the
``bec-mcp-check`` image (deploy/agent/Containerfile), or with any Python that has ``mcp`` 2.x.
"""

from __future__ import annotations

import asyncio
import json
import os
import sys
import urllib.error
import urllib.request

from mcp import ClientSession
from mcp.client.streamable_http import create_mcp_http_client, streamable_http_client

HINTS = {
    401: "the token was rejected: compare BEC_MCP_TOKEN with the server's token file",
    403: "the Origin header was rejected",
    421: "the server refused this Host header: add it to the server's allowed hosts",
}
PROBE_TOOLS = ("mcp_status", "bec_ping", "bec_status")
INITIALIZE = json.dumps(
    {
        "jsonrpc": "2.0",
        "id": 0,
        "method": "initialize",
        "params": {
            "protocolVersion": "2025-06-18",
            "capabilities": {},
            "clientInfo": {"name": "bec-mcp-check", "version": "1"},
        },
    }
).encode()


def post_initialize(url: str, token: str | None) -> int:
    """POST one initialize request and return the HTTP status."""
    headers = {"Content-Type": "application/json", "Accept": "application/json, text/event-stream"}
    if token is not None:
        headers["Authorization"] = f"Bearer {token}"
    request = urllib.request.Request(url, data=INITIALIZE, method="POST", headers=headers)
    try:
        with urllib.request.urlopen(request, timeout=10) as response:
            return response.status
    except urllib.error.HTTPError as exc:
        return exc.code


async def run_session(url: str, token: str) -> None:
    async with create_mcp_http_client(headers={"Authorization": f"Bearer {token}"}) as http:
        async with streamable_http_client(url, http_client=http) as streams:
            async with ClientSession(streams[0], streams[1]) as session:
                init = await session.initialize()
                print(f"connected to {init.server_info.name} {init.server_info.version}")
                tools = sorted(tool.name for tool in (await session.list_tools()).tools)
                print(f"{len(tools)} tools: {', '.join(tools)}")
                for name in PROBE_TOOLS:
                    if name not in tools:
                        continue
                    result = await session.call_tool(name, {})
                    text = " ".join(getattr(block, "text", "") for block in result.content)
                    try:
                        text = json.loads(text).get("summary", text)
                    except (ValueError, AttributeError):
                        pass
                    print(f"{name} ({'error' if result.is_error else 'ok'}): {text[:400]}")


def fail(message: str, status: int | None = None) -> int:
    print(message)
    if status in HINTS:
        print(HINTS[status])
    return 1


def main() -> int:
    url = os.environ.get("BEC_MCP_URL", "").strip()
    token = os.environ.get("BEC_MCP_TOKEN", "").strip()
    if not url or not token:
        print("set BEC_MCP_URL and BEC_MCP_TOKEN")
        return 2

    try:
        status = post_initialize(url, None)
    except OSError as exc:
        return fail(f"cannot reach {url}: {exc}\ncheck the URL and the network flags (README)")
    if status != 401:
        return fail(f"without a token the server answered {status}, expected 401", status)
    print("without a token: 401, as expected")

    status = post_initialize(url, token)
    if status != 200:
        return fail(f"with the token the server answered {status}, expected 200", status)

    try:
        asyncio.run(run_session(url, token))
    except Exception as exc:  # pylint: disable=broad-except
        while isinstance(exc, BaseExceptionGroup) and len(exc.exceptions) == 1:
            exc = exc.exceptions[0]
        return fail(f"MCP session failed: {exc!r}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
