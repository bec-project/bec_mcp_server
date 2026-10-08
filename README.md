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
