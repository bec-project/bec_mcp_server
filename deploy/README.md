# Deploying the BEC MCP server

This guide takes a BEC host from nothing to an AI agent in a container that can answer questions
about the beamline. The server runs on the host, next to BEC. The agent runs in a rootless podman
container and gets exactly two things: the server's URL and a token.

```text
  podman container                      BEC host
 ┌────────────────────┐  HTTP + token  ┌──────────────────────────┐  mcp_ro   ┌────────────┐
 │ Claude Code,       │ ─────────────► │ bec-mcp-server           │ ────────► │ BEC Redis  │
 │ qwen-code, ollmcp  │                │ 127.0.0.1:8000/mcp       │           │            │
 └────────────────────┘                └──────────────────────────┘           └────────────┘
   URL and token only                    one BECClient, read-only
```

The container holds no BEC source code, no Redis credentials and no beamline files. Every tool the
server offers is read-only, so the worst a confused agent can do is give a wrong answer.

| Step | Who | What |
| --- | --- | --- |
| [1](#1-create-the-mcp_ro-redis-account) | BEC Redis admin | Create the `mcp_ro` account |
| [2](#2-install-the-server) | Host admin | Install the server in a venv |
| [3](#3-write-the-token-and-the-redis-credentials) | Host admin | Write the token and the Redis credentials |
| [4](#4-start-the-service) | Host admin | Configure and start `bec-mcp.service` |
| [5](#5-check-the-server-from-a-container) | Agent user | Check the server from a container |
| [6](#6-run-an-agent) | Agent user | Run Claude Code, qwen-code or ollmcp in a container |

You need a BEC 4 deployment with Redis 7 or newer, Python 3.11 or newer on the host (on RHEL 9:
`dnf install python3.12`), systemd, and rootless podman for the agents. To try all of this on a
laptop first, see [Development on a Mac](#development-on-a-mac).

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

## 5. Check the server from a container

From here on, work as the user who will run the agents, with rootless podman and a checkout of
this repository. First, store the token as a podman secret, so it never appears in a command line,
an image or `podman inspect`:

```bash
sudo cat /etc/bec-mcp/token | tr -d '\n' | podman secret create bec_mcp_token -
```

Without `sudo`, ask the host admin for the token and pipe it in the same way. `tr` drops a trailing
newline, which some clients would otherwise send as part of the `Authorization` header and fail
with `Illegal header value`.

Containers reach the server as `host.containers.internal`. On Linux they need two extra flags for
that, explained in [Networking on Linux](#networking-on-linux); on a Mac they need none. Keep them
in an array, which works in bash and zsh alike:

```bash
NET=(--network pasta:-T,8000 --add-host host.containers.internal:127.0.0.1)   # Linux
NET=()                                                                          # macOS
```

Build the check image and run it. It proves that a request without the token is refused, then
connects with the token, lists the tools and calls `mcp_status`, `bec_ping` and `bec_status`:

```bash
podman build -f deploy/agent/Containerfile --target check -t bec-mcp-check deploy
podman run --rm "${NET[@]}" \
    --secret bec_mcp_token,type=env,target=BEC_MCP_TOKEN \
    -e BEC_MCP_URL=http://host.containers.internal:8000/mcp \
    bec-mcp-check
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

## 6. Run an agent

Every agent image reads the server from `BEC_MCP_URL` and `BEC_MCP_TOKEN` and nothing else. The
client configuration baked into each image comes from [`clients/`](clients/), which also shows how
to connect the same clients outside a container.

### Claude Code

Claude Code needs its own credential: an Anthropic API key, or for a Claude subscription a token
from `claude setup-token` on any machine where you are logged in. Store it as a second secret:

```bash
podman secret create anthropic_api_key ./key-file          # or: claude_oauth_token
podman build -f deploy/agent/Containerfile --target claude -t bec-agent-claude deploy
podman run --rm -it "${NET[@]}" \
    --secret bec_mcp_token,type=env,target=BEC_MCP_TOKEN \
    --secret anthropic_api_key,type=env,target=ANTHROPIC_API_KEY \
    -e BEC_MCP_URL=http://host.containers.internal:8000/mcp \
    bec-agent-claude
```

With a subscription token, use `--secret claude_oauth_token,type=env,target=CLAUDE_CODE_OAUTH_TOKEN`
instead. Type `/mcp` to see `bec` connected, then ask, for example, "Is BEC healthy?". Append
`-p "Is BEC healthy?"` to the `podman run` line for a single answer without the interface. The
image allows the `bec` tools without asking and denies Bash, WebFetch and WebSearch, so the agent
works only through the server.

### qwen-code or ollmcp with a local model

Both talk to a model served by [Ollama](https://ollama.com) on the host. On Linux, forward
Ollama's port as well:

```bash
NET=(--network pasta:-T,8000,-T,11434 --add-host host.containers.internal:127.0.0.1)
```

```bash
podman build -f deploy/agent/Containerfile --target qwen -t bec-agent-qwen deploy
podman run --rm -it "${NET[@]}" \
    --secret bec_mcp_token,type=env,target=BEC_MCP_TOKEN \
    -e BEC_MCP_URL=http://host.containers.internal:8000/mcp \
    -e OPENAI_BASE_URL=http://host.containers.internal:11434/v1 \
    -e OPENAI_API_KEY=ollama -e OPENAI_MODEL=qwen3:8b \
    bec-agent-qwen
```

```bash
podman build -f deploy/agent/Containerfile --target ollama -t bec-agent-ollama deploy
podman run --rm -it "${NET[@]}" \
    --secret bec_mcp_token,type=env,target=BEC_MCP_TOKEN \
    -e BEC_MCP_URL=http://host.containers.internal:8000/mcp \
    bec-agent-ollama --model qwen3:8b
```

`ollmcp` reaches Ollama at `http://host.containers.internal:11434` unless `OLLAMA_HOST` says
otherwise. Models below about 8B parameters pick the wrong tool often; see the plan's notes on
local models before judging the server by them.

## Networking on Linux

The server listens on the host's `127.0.0.1` only, so nothing on the beamline network can reach it.
A container has its own loopback, so it needs a way to reach the host's.

On a Mac, podman machine routes `host.containers.internal` to the Mac's loopback, and nothing
extra is needed. On Linux, rootless podman points `host.containers.internal` at the host's public
address instead (with pasta, `169.254.1.2`, which pasta maps to that address), where the server
does not listen. The fix is to forward exactly the server's port into the container and point the
name at it:

| Rootless network | Podman | Flags | Tested |
| --- | --- | --- | --- |
| pasta | 5 and 6 (the default), 4 with `passt` installed | `--network pasta:-T,8000 --add-host host.containers.internal:127.0.0.1` | Podman 6.0.2 on Fedora 44 |
| slirp4netns | 4 (the default) | `--network slirp4netns:allow_host_loopback=true --add-host host.containers.internal:10.0.2.2` | No |
| podman machine (macOS) | any | none | Podman 6.0.2 |

RHEL 9 ships Podman 4 or 5 depending on the minor release, and
`podman info --format '{{.Host.RootlessNetworkCmd}}'` shows which network a host uses. Only the
pasta row and the Mac have been tried; check the slirp4netns row with the check image from step 5
before relying on it. Notes:

- `-T 8000` makes port 8000 on the container's own loopback lead to port 8000 on the host's
  loopback, and only that port. The name has to point at `127.0.0.1` rather than `localhost`,
  because `localhost` resolves to `::1` first and the server listens on IPv4 only; the connection
  is then reset.
- pasta's `--map-host-loopback` and `--map-gw`, and slirp4netns's `allow_host_loopback`, also work,
  but they open every port on the host's loopback to the container, including a Redis listening
  there. Prefer pasta with `-T` where podman offers it. `--network host` shares the whole host
  network and is not an option.
- Using `host.containers.internal` everywhere keeps the URL and the server's allowed hosts the same
  on Mac and Linux. With another name, add it to `BEC_MCP_ALLOWED_HOSTS` in
  `/etc/bec-mcp/bec-mcp.env`, or the server answers `421 Invalid Host header`.

## What the agent can reach

- **The server:** only with the token, and only the read-only tools. The server checks the Host
  header against its allowed hosts, so a web page cannot reach it through DNS rebinding.
- **BEC and Redis:** not through the server, which never hands out its account. The container still
  has ordinary outbound network access, because the agent needs its model. Nothing in it holds
  Redis credentials, but a Redis that accepts the passwordless default user would still answer it;
  closing that is the Redis ACL's job (disable the default user) or a firewall's.
- **Files:** none of the host's. The images mount nothing.

## Rotating the token

```bash
( umask 077; python3 -c 'import secrets; print(secrets.token_urlsafe(32))' > /etc/bec-mcp/token )
systemctl restart bec-mcp
podman secret rm bec_mcp_token
sudo cat /etc/bec-mcp/token | tr -d '\n' | podman secret create bec_mcp_token -
```

## Troubleshooting

| Symptom | Cause and fix |
| --- | --- |
| `421 Invalid Host header` | The name in the URL is not in `BEC_MCP_ALLOWED_HOSTS`. Add it with `:*` for any port and restart the service. |
| `401` with the token | The secret differs from `/etc/bec-mcp/token`, e.g. after a rotation. Recreate the secret. |
| `Connection refused` or `reset` from a container | The network flags are missing or wrong for this host's network mode; see [Networking on Linux](#networking-on-linux). |
| Tools answer, but BEC is unreachable | Check `BEC_REDIS_HOST`/`BEC_REDIS_PORT` and the `mcp_ro` password; `redis-cli ACL LOG` on the Redis shows refused commands. |
| The service does not start | `journalctl -u bec-mcp -e`. `could not read token file` means a file in `/etc/bec-mcp` is missing or not readable by the group `bec-mcp`. |

Every tool call is logged to `/var/lib/bec-mcp/audit.jsonl` with its arguments, duration and
outcome.

## Development on a Mac

For a local BEC (for example a `bec_demo.sh` stack), run the server directly instead of the
service, with a token file in your home directory:

```bash
mkdir -p ~/.config/bec-mcp
( umask 077; python3 -c 'import secrets; print(secrets.token_urlsafe(32))' > ~/.config/bec-mcp/token )
bec-mcp-server --transport streamable-http --host 127.0.0.1 --port 8000 \
    --token-file ~/.config/bec-mcp/token --allowed-hosts 'host.containers.internal:*' \
    --redis localhost:6379
tr -d '\n' < ~/.config/bec-mcp/token | podman secret create bec_mcp_token -
```

Then follow steps 5 and 6 with `NET=()`. Start the podman VM first with `podman machine start`.
