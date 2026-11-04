# Deploying the BEC MCP server

This guide takes a BEC host from nothing to an AI agent that can answer questions about the
beamline. The server runs on the host as a systemd service, next to BEC, and listens on the host's
`127.0.0.1` only. The agent runs on the same host, usually OpenCode in bob's sandbox, and gets
exactly two things: the server's URL and a token.

```text
  same host                              BEC host
 ┌────────────────────┐  HTTP + token  ┌──────────────────────────┐  mcp_ro   ┌────────────┐
 │ bob (OpenCode),    │ ─────────────► │ bec-mcp-server           │ ────────► │ BEC Redis  │
 │ Claude Code, ...   │                │ 127.0.0.1:8000/mcp       │           │            │
 └────────────────────┘                └──────────────────────────┘           └────────────┘
   URL and token only                    one BECClient, read-only
```

The agent needs no BEC source code, no Redis credentials and no beamline files. Every tool the
server offers is read-only, so the worst a confused agent can do is give a wrong answer.

| Step | Who | What |
| --- | --- | --- |
| [1](#1-create-the-mcp_ro-redis-account) | BEC Redis admin | Create the `mcp_ro` account |
| [2](#2-install-the-server) | Host admin | Install the server in a venv |
| [3](#3-write-the-token-and-the-redis-credentials) | Host admin | Write the token and the Redis credentials |
| [4](#4-start-the-service) | Host admin | Configure and start `bec-mcp.service` |
| [5](#5-check-the-server) | Agent user | Check the server with the token |
| [6](#6-connect-an-agent) | Agent user | Connect bob or another MCP client |

You need a BEC 4 deployment with Redis 7 or newer, Python 3.11 or newer on the host (on RHEL 9:
`dnf install python3.12`) and systemd. To try all of this on a laptop first, see
[Development on a laptop](#development-on-a-laptop).

## 1. Create the `mcp_ro` Redis account

The server logs in to the beamline's Redis with its own account, never with the `bec` services
account or the passwordless default user. Whoever administers the beamline's BEC Redis (the person
holding its admin password) creates the account once per beamline and gives the password to the
host admin in step 3.

The account can read BEC's `info/`, `public/` and `user/` keys and subscribe to any channel. It can
write only the status keys and the log stream a `BECClient` keeps about itself, and publish only on
its own status channels. BEC delivers scan requests and queue changes by publishing on channels,
so that last limit is what stops the account from submitting a scan or aborting the queue.
[`acl/README.md`](acl/README.md) explains each grant. As the Redis admin:

```bash
read -rs -p 'new mcp_ro password: ' MCP_RO_PW; echo
redis-cli -h <redis-host> --user <admin> --askpass ACL SETUSER mcp_ro on ">$MCP_RO_PW" \
    '%R~info/*' '%R~public/*' '%R~user/*' '%W~user/services/*' '%W~user/log' \
    '&*' +@read +@connection +@pubsub -publish -spublish \
    +keys +set +expire +xadd +multi +exec '+acl|whoami' \
    '(+publish &user/services/*)'
redis-cli -h <redis-host> --user mcp_ro --pass "$MCP_RO_PW" PING   # PONG
redis-cli -h <redis-host> --user mcp_ro --pass "$MCP_RO_PW" \
    PUBLISH user/queue/queue_modification_request x                # NOPERM
```

Then persist it: add the line from [`acl/mcp_ro.acl.template`](acl/mcp_ro.acl.template) to the
Redis `aclfile`, with the password's SHA-256 (`printf %s "$MCP_RO_PW" | sha256sum`), or run
`ACL SAVE` if Redis already loads its users from an `aclfile`. After the server's first start in
step 4, `redis-cli ACL LOG` should list nothing for `mcp_ro` except that refused `PUBLISH`.

On a development instance without ACLs, skip this step and leave `/etc/bec-mcp/mcp_ro.env` out
in step 3.

## 2. Install the server

As root on the BEC host:

```bash
useradd --system --home-dir /var/lib/bec-mcp --shell /sbin/nologin bec-mcp
python3.12 -m venv /opt/bec-mcp/venv
/opt/bec-mcp/venv/bin/pip install --upgrade pip
/opt/bec-mcp/venv/bin/pip install /path/to/bec_mcp_server    # a checkout of the release tag
/opt/bec-mcp/venv/bin/bec-mcp-server --help
```

The venv belongs to root and the service user can only run it. To upgrade, install the new release
into the same venv and restart the service.

## 3. Write the token and the Redis credentials

Only root and the service can read the two files:

```bash
install -d -m 750 -g bec-mcp /etc/bec-mcp
( umask 077; python3 -c 'import secrets; print(secrets.token_urlsafe(32))' > /etc/bec-mcp/token )

read -rs -p 'mcp_ro password: ' MCP_RO_PW; echo
( umask 077; printf 'REDIS_USER=mcp_ro\nREDIS_PASSWORD=%s\n' "$MCP_RO_PW" > /etc/bec-mcp/mcp_ro.env )
unset MCP_RO_PW

chgrp bec-mcp /etc/bec-mcp/token /etc/bec-mcp/mcp_ro.env
chmod 640 /etc/bec-mcp/token /etc/bec-mcp/mcp_ro.env
```

Without ACLs (step 1 skipped), skip `mcp_ro.env` and delete the `BEC_MCP_ACL_FILE` line from
the unit in step 4.

## 4. Start the service

```bash
install -m 600 deploy/host/bec-mcp.env /etc/bec-mcp/bec-mcp.env
vi /etc/bec-mcp/bec-mcp.env              # set BEC_REDIS_HOST and BEC_REDIS_PORT
install -m 644 deploy/host/bec-mcp.service /etc/systemd/system/bec-mcp.service
systemctl daemon-reload
systemctl enable --now bec-mcp
journalctl -u bec-mcp -f
```

[`host/bec-mcp.service`](host/bec-mcp.service) serves streamable HTTP on `127.0.0.1:8000/mcp`,
reachable only from this host, and refuses requests without the token. Its settings are commented
in the file: the hardening, the 2 GB memory cap with automatic restart, and the state directory
`/var/lib/bec-mcp`, which holds the BEC client logs and `audit.jsonl`, one line per tool call.

A quick check on the host, which should print `401`:

```bash
curl -s -o /dev/null -w '%{http_code}\n' -X POST http://127.0.0.1:8000/mcp
```

## 5. Check the server

From here on, work as the user who will run the agents, with a checkout of this repository. Put the
token in the environment without echoing it; `tr` drops a trailing newline, which some clients
would otherwise send as part of the `Authorization` header and fail with `Illegal header value`:

```bash
export BEC_MCP_URL=http://127.0.0.1:8000/mcp
export BEC_MCP_TOKEN="$(sudo cat /etc/bec-mcp/token | tr -d '\n')"
```

Without `sudo`, ask the host admin for the token. Then run the check with the server's own venv.
It proves that a request without the token is refused, then connects with the token, lists the
tools and calls `mcp_status`, `bec_ping` and `bec_status`:

```bash
/opt/bec-mcp/venv/bin/python deploy/mcp_check.py
```

```text
without a token: 401, as expected
connected to bec 0.1.0
16 tools: bec_beamline_states, bec_device_diagnose, bec_device_info, bec_device_read, ...
mcp_status (ok): MCP server 0.1.0 is up (streamable-http, diagnose mode) for 12 s; ...
bec_ping (ok): Redis at localhost:6379 answered in 0.4 ms; 7 services and 3 clients publish a status.
bec_status (ok): BEC is healthy: all 4 core services are running (...)
```

If it fails, [Troubleshooting](#troubleshooting) maps each error to its cause.

## 6. Connect an agent

### bob

bob runs OpenCode in Anthropic's sandbox-runtime and names its MCP servers in `bob.toml`; see
bob's `deploy/bob.example.toml`. For this service:

```toml
[mcp.bec]
url = "http://localhost:8000/mcp"
plugin = "bec"
```

Inside the sandbox, OpenCode reaches the server through sandbox-runtime's proxy as
`localhost4.localdomain4:8000`, a name RHEL's `/etc/hosts` maps to `127.0.0.1`. The server answers
names it does not know with `421 Invalid Host header`, so
[`host/bec-mcp.env`](host/bec-mcp.env) lists that name in `BEC_MCP_ALLOWED_HOSTS`. bob's GUI
plugins call `http://127.0.0.1:8000/mcp` directly, which the server always accepts.

bob does not send a bearer token yet; its documentation describes token support as separate work.
Until it does, bob can start a server of its own for a development BEC with its `host_command`
setting, as `bob.example.toml` shows.

### Other MCP clients

[`clients/`](clients/) has configuration for Claude Code, qwen-code and ollmcp running on the host.
Every file refers to the server through `BEC_MCP_URL` and to the token through `BEC_MCP_TOKEN`, as
set in step 5.

## What the agent can reach

- **The server:** only with the token, and only the read-only tools. The server checks the Host
  header against its allowed hosts, so a web page cannot reach it through DNS rebinding.
- **BEC and Redis:** not through the server, which never hands out its account. An agent running
  directly on the host has whatever its user has, including a Redis that accepts the passwordless
  default user; closing that is the Redis ACL's job (disable the default user) or a firewall's.
  bob's sandbox lets OpenCode reach only the endpoints listed in `bob.toml`.
- **Files:** the server returns no file contents, only paths as pointers for a human. bob's
  sandbox denies reads outside the folders its settings list.

## Rotating the token

```bash
( umask 077; python3 -c 'import secrets; print(secrets.token_urlsafe(32))' > /etc/bec-mcp/token )
systemctl restart bec-mcp
```

Then give every client the new token, for example by exporting `BEC_MCP_TOKEN` again.

## Troubleshooting

| Symptom | Cause and fix |
| --- | --- |
| `421 Invalid Host header` | The name the client sends is not in `BEC_MCP_ALLOWED_HOSTS`. OpenCode in bob's sandbox sends `localhost4.localdomain4`; add the name the client uses and restart the service. |
| `401` with the token | The client's token differs from `/etc/bec-mcp/token`, e.g. after a rotation. |
| `Connection refused` | The service is not running, or the client uses another port; `systemctl status bec-mcp`. |
| Tools answer, but BEC is unreachable | Check `BEC_REDIS_HOST`/`BEC_REDIS_PORT` and the `mcp_ro` password; `redis-cli ACL LOG` on the Redis shows refused commands. |
| Docs tools answer that the docs are unavailable | The host cannot download the docs index from gitea.psi.ch. Check `curl -sI https://gitea.psi.ch/api/packages/bec/generic/bec_docs_index/latest/bec_docs_index.json.gz` from the host, or set `BEC_MCP_DOCS_INDEX` in `bec-mcp.env` to a reachable URL or a local copy; the server tries again every five minutes. |
| The service does not start | `journalctl -u bec-mcp -e`. `could not read token file` means a file in `/etc/bec-mcp` is missing or not readable by the group `bec-mcp`. |

Every tool call is logged to `/var/lib/bec-mcp/audit.jsonl` with its arguments, duration and
outcome.

## Development on a laptop

For a local BEC (for example a `bec_demo.sh` stack), run the server directly instead of the
service, with a token file in your home directory:

```bash
mkdir -p ~/.config/bec-mcp
( umask 077; python3 -c 'import secrets; print(secrets.token_urlsafe(32))' > ~/.config/bec-mcp/token )
bec-mcp-server --transport streamable-http --host 127.0.0.1 --port 8000 \
    --token-file ~/.config/bec-mcp/token --allowed-hosts localhost4.localdomain4 \
    --redis localhost:6379
export BEC_MCP_URL=http://127.0.0.1:8000/mcp
export BEC_MCP_TOKEN="$(tr -d '\n' < ~/.config/bec-mcp/token)"
```

Then run the check from step 5 with any Python that has `mcp` 2.x, and connect a client from step 6.
