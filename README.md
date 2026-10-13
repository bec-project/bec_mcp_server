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

To run the server as a service on a beamline host, with agents such as Claude Code, qwen-code or an
Ollama model in podman containers, follow [deploy/README.md](deploy/README.md).

### Connecting to BEC

The server starts its `BECClient` on the first tool call, so it answers even while BEC is down.
Without `--redis` or `--config` it finds Redis like any other BEC client: `BEC_SERVICE_CONFIG`,
then the deployment config, then `localhost:6379`.

```bash
bec-mcp-server --redis localhost:6379                       # a local BEC
bec-mcp-server --config /path/to/bec_config.yaml            # the deployment's service config
bec-mcp-server --acl-file /etc/bec-mcp/mcp_ro.env           # the server's own Redis account
```

On a deployment with Redis ACLs, give the server its own restricted account; see
[deploy/acl/README.md](deploy/acl/README.md). If Redis refuses that account, the server reports it
instead of silently using the `default` user.

A lost Redis makes calls fail within seconds instead of hanging: the client's Redis connections
time out after `BEC_MCP_REDIS_TIMEOUT` seconds (default 5) and never retry, and the next call
starts a fresh client. The client also restarts after `--client-max-age`, because some of its
internal structures only grow; `mcp_status` shows its age.

### The log cache

BEC services and clients publish their log records to one Redis stream, which bec_lib trims to
about 10,000 records. The server keeps its own copy of the newest records, 3000 by default
(`--log-cache-size`). When the server starts it connects to BEC in the background, seeds the cache
from the stream, and from then on bec_lib's own stream thread delivers every new record, so
following the stream costs no tool call and no worker slot. Each record gets an id, counted from 1
since the server started, which the log tools use to refer to records. The cache carries over
client restarts without losing or repeating records, and while BEC is unreachable the log tools
still serve what it held. `mcp_status` shows its state under `bec.log_cache`; `0` turns it off, and
the log tools then read the stream on every call.

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
| `--redis` | `BEC_REDIS_HOST`, `BEC_REDIS_PORT` | from the BEC config | BEC's Redis as `HOST:PORT` |
| `--config` | `BEC_MCP_SERVICE_CONFIG` | unset | BEC service config YAML |
| `--acl-file` | `BEC_MCP_ACL_FILE` | unset | Env file with `REDIS_USER` and `REDIS_PASSWORD` (or `BEC_REDIS_USERNAME` and `BEC_REDIS_PASSWORD`) |
| `--connect-on-startup` | `BEC_MCP_CONNECT_ON_STARTUP` | off | Connect before serving and exit if BEC is unreachable |
| `--client-max-age` | `BEC_MCP_CLIENT_MAX_AGE` | `21600` | Seconds before the BEC client is restarted; `0` never |
| `--log-cache-size` | `BEC_MCP_LOG_CACHE_SIZE` | `3000` | Log records the server keeps from BEC's log stream; `0` turns the cache off |
| | `BEC_MCP_REDIS_TIMEOUT` | `5` | Seconds before a Redis connect or reply gives up |
| | `BEC_MCP_LOG_DIR` | `~/.cache/bec-mcp-server/logs` | bec_lib's log files, unless the service config sets them |
| `--docs-index` | `BEC_MCP_DOCS_INDEX` | published index | Docs search index: an http(s) URL or a `.json`/`.json.gz` file |

Each tool call runs on a worker thread with a deadline, so a wedged Redis turns into an error
message instead of a hung session. The audit log records the tool, its arguments, the duration and
the outcome of every call, including calls to unknown tools or with invalid arguments.

## Tools

| Tool | Returns |
| --- | --- |
| `mcp_status` | Whether the server is up: version, transport, mode, uptime, BEC connection state and client age, and recent failed calls |
| `bec_ping` | Whether BEC's Redis answers, the round trip in ms, and how many services and clients publish a status |
| `bec_status` | Whether BEC is healthy: each core service's status, version, host, CPU and memory, plus other services and clients |
| `bec_scans` | The scans this BEC instance offers, each with a one-line description |
| `bec_scan_help` | How to run one scan: its arguments with types, units, bounds and defaults, and an example call built from its signature with the session's own motors |
| `bec_scan_history` | The latest finished scans with number, status and duration, and findings for scans that did not close cleanly; file paths are for the user |
| `bec_logs` | Recent log records filtered by service, minimum level, text and time window, with repeats collapsed into counts and alarms merged into one list with findings |
| `bec_log_range` | Consecutive log records in full around one record id, between two ids or two times, or the newest, with Python tracebacks parsed into the exception chain, frames and the BEC frame where the failure started |
| `bec_log_analysis` | Errors, tracebacks and alarms grouped into incidents, each with its count, traceback origin, the records just before it, the devices and scan it names, the incidents that started with it, a likely cause and a suggested next step |
| `bec_docs_search` | The bec_docs pages that best match a question or keywords, with their URLs |
| `bec_docs_page` | The text of one bec_docs page, or of one section of it |

Every tool answers with the same top-level keys: `ok`, `summary`, `findings`, `data` and
`truncated`. When BEC is unreachable, `bec_ping` and `bec_status` answer `ok: false` with the
reason rather than failing.

## Documentation search

`bec_docs_search` and `bec_docs_page` search the [BEC documentation](https://bec-project.github.io/bec_docs/)
with BM25, in memory, over a small index file (about 100 KB). No copy ships with the server: the `bec_docs`
deploy builds the file from every published version of the docs and serves it at
`https://bec-project.github.io/bec_docs/bec_docs_index.json.gz`, which the server downloads by default.
Hosts that only reach a git mirror can read the same file from the `mcp-docs-index` branch of `bec_docs`:

```bash
bec-mcp-server --docs-index https://<mirror>/<org>/bec_docs/raw/branch/mcp-docs-index/bec_docs_index.json.gz
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
black --check --line-length=100 --skip-magic-trailing-comma .
isort --check --line-length=100 --profile=black --multi-line=3 --trailing-comma .
pylint bec_mcp_server
```

CI runs the tests on Python 3.11 to 3.14, the three linters, and a wheel build that is installed into
a clean environment. Contributor and agent guidelines are in [AGENTS.md](AGENTS.md).
