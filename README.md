# bec-mcp-server

An [MCP](https://modelcontextprotocol.io) server that lets an AI agent inspect a running
[BEC (Beamline Experiment Control)](https://github.com/bec-project/bec) instance. Every tool is
read-only: the server can report on services, devices, scans and the scan queue, but it cannot move
a device, start a scan or change a configuration.

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
bec-mcp-server                                   # streamable HTTP on http://127.0.0.1:8000/mcp
bec-mcp-server --transport stdio --lazy-connect  # for a local agent that spawns the server
```

The server connects to BEC on startup unless `--lazy-connect` is given, in which case it connects on
the first tool call. Command-line options can also be set through the environment:

| Variable | Default | Meaning |
| --- | --- | --- |
| `BEC_MCP_TRANSPORT` | `streamable-http` | `streamable-http` or `stdio` |
| `BEC_MCP_HOST` | `127.0.0.1` | HTTP bind address |
| `BEC_MCP_PORT` | `8000` | HTTP port |
| `BEC_MCP_PATH` | `/mcp` | HTTP path |
| `BEC_REDIS_HOST` | `localhost` | Redis host of the BEC instance |
| `BEC_REDIS_PORT` | `6379` | Redis port of the BEC instance |
| `BEC_REDIS_USERNAME` | unset | Redis ACL user |
| `BEC_REDIS_PASSWORD` | unset | Redis ACL password |
| `BEC_MCP_DOCS_INDEX` | latest index on gitea.psi.ch | Docs search index: an http(s) URL or a `.json`/`.json.gz` file (`--docs-index`) |

The HTTP transport has no authentication yet, so keep it bound to `127.0.0.1`.

## Tools

| Tool | Returns |
| --- | --- |
| `bec_ping` | Whether Redis answers |
| `bec_summary` | A compact overview of services, devices, scans and queues |
| `bec_services` | The status message each BEC service publishes |
| `bec_devices` | All devices with class, enabled flag, readout priority and tags |
| `bec_device_config` | The full configuration of one device |
| `bec_scans` | Available scans, optionally filtered by prefix or base class |
| `bec_scan_details` | One scan's signature and arguments |
| `bec_scan_interlock` | The scan interlock configuration and the states it watches |
| `bec_queue` | A compact summary of the scan queues |
| `bec_docs_search` | The best-matching pages of the BEC documentation, or all pages without a query |
| `bec_docs_page` | One documentation page, or one section of it |

## Documentation search

`bec_docs_search` and `bec_docs_page` search the [BEC documentation](https://bec-project.github.io/bec_docs/)
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
