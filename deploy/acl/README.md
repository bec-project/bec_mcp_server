# Redis account for the MCP server

The server authenticates to the beamline Redis with its **own least-privilege account**, never
with the open `default` user and never with the full-access account the BEC services use. In
diagnose mode that account is `mcp_ro`. It belongs to the server; the agent never sees it.

## What the account may do

A read-only account is not enough: the server's `BECClient` registers as a BEC service, so it
publishes its own status and metrics and appends its log records to BEC's log stream. The grant
therefore uses Redis 7 read and write key selectors (`%R~`, `%W~`). Everything is readable under
BEC's `info/`, `public/` and `user/` prefixes; the only writable keys are the client's own
service status and metrics (`user/services/*`) and the log stream (`user/log`).

Publishing is limited to the client's own status channels (`user/services/*`). This matters:
BEC 4 takes scan requests as a PUBLISH on `personal/<user>/queue/queue_request`, and queue aborts
and halts on `user/queue/queue_modification_request`. With an unrestricted `+publish` on `&*`, the
account could submit scans as any user or stop the queue.

## Creating it

On the deployment's Redis, as an admin account:

```bash
redis-cli ACL SETUSER mcp_ro on '>CHANGE-ME' \
    '%R~info/*' '%R~public/*' '%R~user/*' '%W~user/services/*' '%W~user/log' \
    '&*' +@read +@connection +@pubsub -publish -spublish \
    +keys +set +expire +xadd +multi +exec '+acl|whoami' \
    '(+publish &user/services/*)'
```

Persist it in the deployment's `aclfile` so it survives a Redis restart; the commented line
for the aclfile is in [`mcp_ro.acl.template`](mcp_ro.acl.template). Then start the server once
and check `redis-cli ACL LOG` for denials. Checks that should fail with `NOPERM`:

```bash
redis-cli --user mcp_ro --pass CHANGE-ME PUBLISH user/queue/queue_modification_request x
redis-cli --user mcp_ro --pass CHANGE-ME SET user/queue/anything 1
```

## Handing it to the server

Write the credentials to an env file only the service user can read. The format is the
`.bec_acl.env` convention of the auto-deployed BEC services:

```bash
install -m 600 /dev/null /etc/bec-mcp/mcp_ro.env
printf 'REDIS_USER=mcp_ro\nREDIS_PASSWORD=CHANGE-ME\n' > /etc/bec-mcp/mcp_ro.env
```

Point the server at it with `--acl-file /etc/bec-mcp/mcp_ro.env` or
`BEC_MCP_ACL_FILE=/etc/bec-mcp/mcp_ro.env`. `BEC_REDIS_USERNAME` and `BEC_REDIS_PASSWORD`, set
together, win over the file.

If Redis refuses the account, bec_lib quietly falls back to the `default` user, which on a
Redis without ACLs has every right. The server refuses to run like that: `bec_ping` and
`bec_status` then report that Redis did not accept the account, and `mcp_status` shows the
error.

On a development setup without Redis ACLs the server also works without an account. The
account matters once the deployment turns ACLs on.
