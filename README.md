# bec-mcp-server

An [MCP](https://modelcontextprotocol.io) server that lets an AI agent inspect a running
[BEC (Beamline Experiment Control)](https://github.com/bec-project/bec) instance. Every tool is
read-only: the server reports on BEC, but it cannot move a device, start a scan or change a
configuration.

The server talks to BEC through a single `BECClient`. An agent only ever reaches the MCP server; it
never needs BEC's source code or direct access to Redis.

> **Status:** early. v1 is being built in small steps on `main`.

## Installation

Requires Python 3.11 or newer and a BEC 4 deployment to talk to.

```bash
python -m venv .venv
source .venv/bin/activate
python -m pip install -e .
bec-mcp-server --help
```

## Running

```bash
bec-mcp-server                         # stdio, for a local agent that starts the server itself
claude mcp add bec -- "$(command -v bec-mcp-server)"   # register it with Claude Code
```

For an agent that connects over HTTP, for example from a podman container on the same host, serve
streamable HTTP with a bearer token and name the host the container uses:

```bash
openssl rand -hex 32 > ~/.config/bec-mcp/token && chmod 600 ~/.config/bec-mcp/token
bec-mcp-server --transport streamable-http --token-file ~/.config/bec-mcp/token \
  --allowed-hosts 'host.containers.internal:*'
```

The container then reaches `http://host.containers.internal:8000/mcp` with the header
`Authorization: Bearer <token>`. HTTP always needs a token, even on `127.0.0.1`, because a container
reaches the host's loopback address too; `--allow-unauthenticated` turns that off explicitly. The
server accepts only the `Host` headers it knows: loopback names, the bind address, and whatever
`--allowed-hosts` adds. Anything else gets `421 Invalid Host header`, which protects against DNS
rebinding.

Every option can also be set through the environment:

| Option | Variable | Default | Meaning |
| --- | --- | --- | --- |
| `--transport` | `BEC_MCP_TRANSPORT` | `stdio` | `stdio` or `streamable-http` |
| `--host` | `BEC_MCP_HOST` | `127.0.0.1` | HTTP bind address |
| `--port` | `BEC_MCP_PORT` | `8000` | HTTP port |
| `--path` | `BEC_MCP_PATH` | `/mcp` | HTTP path |
| `--mode` | `BEC_MCP_MODE` | `diagnose` | Tool set; v1 has only `diagnose` |
| `--token-file` | `BEC_MCP_TOKEN_FILE` | unset | File with the HTTP bearer token (`BEC_MCP_TOKEN` holds the token itself) |
| `--allow-unauthenticated` | `BEC_MCP_ALLOW_UNAUTHENTICATED` | off | Serve HTTP without a token |
| `--allowed-hosts` | `BEC_MCP_ALLOWED_HOSTS` | none | Extra `Host` values, comma-separated; `name` matches any port |
| `--allowed-origins` | `BEC_MCP_ALLOWED_ORIGINS` | none | Extra `Origin` values for browser clients |
| `--tool-timeout` | `BEC_MCP_TOOL_TIMEOUT` | `30` | Deadline in seconds for each tool call |
| `--max-bec-calls` | `BEC_MCP_MAX_BEC_CALLS` | `8` | Tool calls that may wait on BEC at once |
| `--audit-log` | `BEC_MCP_AUDIT_LOG` | `~/.local/state/bec-mcp-server/audit.jsonl` | One JSON line per tool call; `off` disables it |
| `--log-level` | `BEC_MCP_LOG_LEVEL` | `INFO` | Server log level; logs go to stderr |

Each tool call runs on a worker thread with a deadline, so a wedged Redis turns into an error
message instead of a hung session. The audit log records the tool, its arguments, the duration and
the outcome of every call, including calls to unknown tools or with invalid arguments.

## Tools

| Tool | Returns |
| --- | --- |
| `mcp_status` | Whether the server is up: version, transport, mode, uptime, BEC connection state and recent failed calls |

Every tool answers with the same top-level keys: `ok`, `summary`, `findings`, `data` and
`truncated`. The BEC tools arrive in the next steps of the v1 plan.

## Documentation search

The docs tools `bec_docs_search` and `bec_docs_page` come back on this runtime in a later step of the v1 plan;
the search package and the index builder are already here. They search the [BEC documentation](https://bec-project.github.io/bec_docs/)
with BM25, in memory, over a small index file (about 100 KB). No copy ships with the server: `bec_docs`
builds the file from every published version of the docs and uploads it to PSI's gitea as the generic
package `bec_docs_index`. By default the server downloads the latest one:

```bash
curl -OJ https://gitea.psi.ch/api/packages/bec/generic/bec_docs_index/latest/bec_docs_index.json.gz
```

`--docs-index` (or `BEC_MCP_DOCS_INDEX`) points the server at another URL, such as a pinned package
version, or at a local file:

```bash
bec-mcp-server --docs-index /var/lib/bec-mcp/bec_docs_index.json.gz
```

A URL is downloaded again every hour and a local file is reloaded when it changes; if a later download
fails, the last good copy keeps serving. Until the first download succeeds, the docs tools answer that the
docs are unavailable and retry after five minutes. To build an index from a local `bec_docs` checkout:

```bash
bec-mcp-build-docs-index path/to/bec_docs --output bec_docs_index.json.gz
```

## Development

```bash
python -m pip install -e '.[dev]'
python -m pytest --random-order tests/
ruff check .
ruff format --check .
pylint bec_mcp_server
```

CI runs the tests on Python 3.11 to 3.14, ruff and Pylint, and a wheel build that is installed into
a clean environment. Contributor and agent guidelines are in [AGENTS.md](AGENTS.md).

A merge to `main` releases a new version with python-semantic-release: `feat` commits raise the
minor version, `fix` and `perf` commits the patch version, and the tag gets a GitHub release with
the wheel. The server reports its version to MCP clients as `serverInfo.version`.
