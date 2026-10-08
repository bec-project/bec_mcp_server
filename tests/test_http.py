"""HTTP transport: bearer token, Host and Origin checks, against a real uvicorn server."""

from __future__ import annotations

import http.client
import json
import threading

import anyio
import pytest
import uvicorn

from bec_mcp_server.app import build_app
from bec_mcp_server.settings import Settings, parse_host_list, parse_origin_list, resolve_token
from bec_mcp_server.transport import BearerTokenMiddleware, http_app, require_auth

TOKEN = "s3cret-token"

INITIALIZE = {
    "jsonrpc": "2.0",
    "id": 1,
    "method": "initialize",
    "params": {
        "protocolVersion": "2025-06-18",
        "capabilities": {},
        "clientInfo": {"name": "curl", "version": "0"},
    },
}


async def _ok_app(scope, receive, send):
    await send({"type": "http.response.start", "status": 200, "headers": []})
    await send({"type": "http.response.body", "body": b"ok"})


def _asgi_call(middleware, headers=None, scope_type="http"):
    scope = {
        "type": scope_type,
        "headers": [
            (key.lower().encode(), value.encode("latin-1"))
            for key, value in (headers or {}).items()
        ],
    }
    sent = []

    async def receive():
        return {"type": "http.request"}

    async def send(message):
        sent.append(message)

    anyio.run(middleware, scope, receive, send)
    return sent


def test_missing_token_is_rejected_with_www_authenticate():
    sent = _asgi_call(BearerTokenMiddleware(_ok_app, token=TOKEN))
    assert sent[0]["status"] == 401
    assert (b"www-authenticate", b'Bearer realm="bec-mcp-server"') in sent[0]["headers"]


@pytest.mark.parametrize(
    "authorization", ["Bearer nope", f"Basic {TOKEN}", TOKEN, f"Bearer {TOKEN}x", "Bearer t\xf6ken"]
)
def test_wrong_credentials_are_rejected_not_crashed(authorization):
    sent = _asgi_call(BearerTokenMiddleware(_ok_app, token=TOKEN), {"authorization": authorization})
    assert sent[0]["status"] == 401


def test_correct_token_is_accepted_case_insensitive_scheme():
    for scheme in ("Bearer", "bearer"):
        sent = _asgi_call(
            BearerTokenMiddleware(_ok_app, token=TOKEN), {"authorization": f"{scheme} {TOKEN}"}
        )
        assert sent[0]["status"] == 200


def test_non_http_scope_passes_through_untouched():
    reached = []

    async def lifespan_app(scope, receive, send):
        reached.append(scope["type"])

    _asgi_call(BearerTokenMiddleware(lifespan_app, token=TOKEN), scope_type="lifespan")
    assert reached == ["lifespan"]


def test_http_requires_a_token_even_on_loopback():
    with pytest.raises(SystemExit, match="without a bearer token"):
        require_auth(Settings(transport="streamable-http", host="127.0.0.1"))
    require_auth(Settings(transport="streamable-http", token=TOKEN))
    require_auth(Settings(transport="streamable-http", allow_unauthenticated=True))


def test_token_file_wins_over_environment(tmp_path, monkeypatch):
    token_file = tmp_path / "token"
    token_file.write_text("file-secret\n", encoding="utf-8")
    monkeypatch.setenv("BEC_MCP_TOKEN", "env-secret")
    assert resolve_token(str(token_file)) == "file-secret"
    monkeypatch.setenv("BEC_MCP_TOKEN_FILE", str(token_file))
    assert resolve_token(None) == "file-secret"
    monkeypatch.delenv("BEC_MCP_TOKEN_FILE")
    assert resolve_token(None) == "env-secret"
    monkeypatch.delenv("BEC_MCP_TOKEN")
    assert resolve_token(None) is None


@pytest.mark.parametrize(
    ("content", "message"), [("\n", "is empty"), ("t\xf6ken", "must be ASCII")]
)
def test_unusable_token_files_are_refused(tmp_path, content, message):
    token_file = tmp_path / "token"
    token_file.write_text(content, encoding="utf-8")
    with pytest.raises(SystemExit, match=message):
        resolve_token(str(token_file))


def test_missing_token_file_is_reported(tmp_path):
    with pytest.raises(SystemExit, match="could not read token file"):
        resolve_token(str(tmp_path / "missing"))


def test_allowed_hosts_accept_names_ports_and_wildcards():
    settings = Settings(
        allowed_hosts=parse_host_list(
            "host.containers.internal, beamline:8000,[fd00::1]:*", option="--allowed-hosts"
        )
    )
    hosts = settings.transport_allowed_hosts()

    assert {"127.0.0.1:*", "localhost:*", "[::1]:*"} <= set(hosts)
    assert {"host.containers.internal", "host.containers.internal:*"} <= set(hosts)
    assert "beamline:8000" in hosts and "beamline:*" not in hosts
    assert "[fd00::1]:*" in hosts


def test_non_loopback_bind_host_is_allowed_automatically():
    hosts = Settings(host="bec-host.psi.ch").transport_allowed_hosts()
    assert "bec-host.psi.ch:*" in hosts
    assert not any(
        host.startswith("0.0.0.0") for host in Settings(host="0.0.0.0").transport_allowed_hosts()
    )


@pytest.mark.parametrize("entry", ["http://host", "*", "host/path", "fd00::1"])
def test_malformed_allowed_hosts_are_refused(entry):
    with pytest.raises(SystemExit, match="allowed"):
        parse_host_list(entry, option="--allowed-hosts")


def test_allowed_origins_expand_bare_names_and_keep_full_origins():
    settings = Settings(
        allowed_origins=parse_origin_list(
            "console.psi.ch, https://ui.psi.ch:8443", option="--allowed-origins"
        )
    )
    origins = settings.transport_allowed_origins()
    assert {"http://console.psi.ch:*", "https://console.psi.ch:*"} <= set(origins)
    assert "https://ui.psi.ch:8443" in origins


@pytest.mark.parametrize("entry", ["null", "*", "ftp://x", "http://[bad", "http://host/path"])
def test_malformed_allowed_origins_are_refused(entry):
    with pytest.raises(SystemExit, match="origin"):
        parse_origin_list(entry, option="--allowed-origins")


class _LiveServer:
    """The real HTTP app (SDK transport security behind the bearer check) on a free port."""

    def __init__(self, settings: Settings) -> None:
        app = build_app(settings)
        config = uvicorn.Config(
            http_app(app.server, settings), host="127.0.0.1", port=0, log_level="warning"
        )
        self.server = uvicorn.Server(config)
        self.thread = threading.Thread(target=self.server.run, daemon=True)

    def __enter__(self):
        self.thread.start()
        for _ in range(500):
            if self.server.started:
                break
            threading.Event().wait(0.01)
        assert self.server.started
        self.port = self.server.servers[0].sockets[0].getsockname()[1]
        return self

    def __exit__(self, *exc):
        self.server.should_exit = True
        self.thread.join(5)

    def post(self, *, host: str, token: str | None = TOKEN, origin: str | None = None):
        connection = http.client.HTTPConnection("127.0.0.1", self.port, timeout=5)
        connection.putrequest("POST", "/mcp", skip_host=True, skip_accept_encoding=True)
        headers = {
            "Host": host,
            "Content-Type": "application/json",
            "Accept": "application/json, text/event-stream",
        }
        if token is not None:
            headers["Authorization"] = f"Bearer {token}"
        if origin is not None:
            headers["Origin"] = origin
        body = json.dumps(INITIALIZE).encode()
        headers["Content-Length"] = str(len(body))
        for key, value in headers.items():
            connection.putheader(key, value)
        connection.endheaders(body)
        response = connection.getresponse()
        status, text = response.status, response.read().decode()
        connection.close()
        return status, text


@pytest.fixture
def container_ready_server():
    settings = Settings(
        transport="streamable-http",
        token=TOKEN,
        allowed_hosts=parse_host_list("host.containers.internal:*", option="--allowed-hosts"),
    )
    with _LiveServer(settings) as server:
        yield server


def test_container_host_name_is_served_with_the_token(container_ready_server):
    host = f"host.containers.internal:{container_ready_server.port}"
    status, text = container_ready_server.post(host=host)

    assert status == 200, text
    assert '"serverInfo"' in text and '"bec"' in text


def test_container_host_name_without_the_token_gets_401(container_ready_server):
    host = f"host.containers.internal:{container_ready_server.port}"
    status, _ = container_ready_server.post(host=host, token=None)

    assert status == 401


def test_loopback_host_is_always_served(container_ready_server):
    status, text = container_ready_server.post(host=f"127.0.0.1:{container_ready_server.port}")

    assert status == 200, text


def test_unlisted_host_name_gets_421(container_ready_server):
    status, text = container_ready_server.post(host=f"evil.example:{container_ready_server.port}")

    assert status == 421
    assert "Invalid Host header" in text


def test_foreign_origin_gets_403_even_with_the_token(container_ready_server):
    status, _ = container_ready_server.post(
        host=f"127.0.0.1:{container_ready_server.port}", origin="http://evil.example"
    )

    assert status == 403


def test_without_allowed_hosts_the_container_name_gets_421():
    # what a container sees today: the request arrives but its Host header is refused
    settings = Settings(transport="streamable-http", token=TOKEN)
    with _LiveServer(settings) as server:
        status, _ = server.post(host=f"host.containers.internal:{server.port}")

    assert status == 421
