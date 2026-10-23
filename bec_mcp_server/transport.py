"""stdio and streamable-HTTP transports: bearer token, allowed hosts and origins."""

from __future__ import annotations

import hmac
import logging
from typing import Any

import uvicorn
from mcp.server.mcpserver import MCPServer
from mcp.server.transport_security import TransportSecuritySettings

from bec_mcp_server.settings import LOOPBACK_HOSTS, Settings

logger = logging.getLogger(__name__)


class BearerTokenMiddleware:  # pylint: disable=too-few-public-methods
    """ASGI middleware that requires a static bearer token on every HTTP request.

    The comparison is constant-time and runs on raw header bytes: hmac.compare_digest raises
    TypeError on non-ASCII str input, which would turn a malformed Authorization header into
    a 500 instead of a 401. Host and Origin checks are left to the SDK's transport security.
    """

    def __init__(self, app: Any, *, token: str) -> None:
        self.app = app
        self._token = token.encode("ascii")

    async def __call__(self, scope: dict, receive: Any, send: Any) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        authorization = b""
        for key, value in scope.get("headers") or []:
            if key.lower() == b"authorization":
                authorization = value
                break
        scheme, _, credential = authorization.partition(b" ")
        if scheme.lower() != b"bearer" or not hmac.compare_digest(credential.strip(), self._token):
            await _plain_response(send, 401, "missing or invalid bearer token")
            return
        await self.app(scope, receive, send)


async def _plain_response(send: Any, status: int, message: str) -> None:
    headers = [
        (b"content-type", b"text/plain; charset=utf-8"),
        (b"www-authenticate", b'Bearer realm="bec-mcp-server"'),
    ]
    await send({"type": "http.response.start", "status": status, "headers": headers})
    await send({"type": "http.response.body", "body": message.encode("utf-8")})


def require_auth(settings: Settings) -> None:
    """Refuse to serve HTTP without a token unless the operator explicitly accepted that.

    Loopback is no exception: a container reaches the host's 127.0.0.1 through
    host.containers.internal, and every local user can reach it too.
    """
    if settings.token is not None or settings.allow_unauthenticated:
        return
    raise SystemExit(
        f"refusing to serve streamable-http on {settings.host!r} without a bearer token: set "
        "--token-file (or BEC_MCP_TOKEN_FILE / BEC_MCP_TOKEN), or pass --allow-unauthenticated "
        "to accept that anyone who can reach the port can read beamline state"
    )


def transport_security(settings: Settings) -> TransportSecuritySettings:
    """DNS-rebinding protection for the HTTP transport, always on.

    Loopback names are always allowed. A container reaching the host sends its own name for
    the host (host.containers.internal with podman), which must be listed with --allowed-hosts.
    """
    return TransportSecuritySettings(
        enable_dns_rebinding_protection=True,
        allowed_hosts=settings.transport_allowed_hosts(),
        allowed_origins=settings.transport_allowed_origins(),
    )


def http_app(server: MCPServer, settings: Settings) -> Any:
    """The ASGI app served on HTTP: the SDK's streamable-HTTP app behind the bearer check."""
    app = server.streamable_http_app(
        streamable_http_path=settings.path,
        transport_security=transport_security(settings),
        host=settings.host,
    )
    if settings.token is None:
        return app
    return BearerTokenMiddleware(app, token=settings.token)


def run_http(server: MCPServer, settings: Settings) -> None:
    """Serve streamable HTTP until uvicorn is stopped (SIGINT or SIGTERM)."""
    require_auth(settings)
    bind = settings.host.strip("[]").lower()
    if bind not in LOOPBACK_HOSTS:
        logger.warning(
            "serving on non-loopback host %s; only these Host headers are accepted: %s",
            settings.host,
            ", ".join(settings.transport_allowed_hosts()),
        )
    logger.info("serving MCP over streamable HTTP at %s", settings.http_url)
    uvicorn.run(
        http_app(server, settings),
        host=settings.host.strip("[]"),
        port=settings.port,
        log_level=settings.log_level.lower(),
    )


def run_stdio(server: MCPServer) -> None:
    """Serve one session on stdin/stdout until the client closes it."""
    server.run(transport="stdio")
