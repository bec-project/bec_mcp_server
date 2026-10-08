"""The bec-mcp-server command: parse flags and BEC_MCP_* variables, run a transport, shut down."""

from __future__ import annotations

import argparse
import logging
import os
import signal
import threading
import time

from bec_mcp_server.app import BecMcpApp, build_app
from bec_mcp_server.bec.connection import (
    DEFAULT_CLIENT_MAX_AGE_S,
    BecConnection,
    BecConnectionConfig,
    BecUnavailableError,
)
from bec_mcp_server.settings import (
    DEFAULT_MAX_BEC_CALLS,
    DEFAULT_TOOL_TIMEOUT_S,
    LOG_LEVELS,
    MODE_TIERS,
    TRANSPORTS,
    Settings,
    env_bool,
    env_float,
    env_int,
    env_optional,
    env_str,
    parse_host_list,
    parse_origin_list,
    resolve_audit_log,
    resolve_token,
)
from bec_mcp_server.transport import run_http, run_stdio

logger = logging.getLogger(__name__)

#: how long the process waits at exit for worker threads still busy with abandoned BEC calls
EXIT_GRACE_S = 5.0


def build_parser() -> argparse.ArgumentParser:
    """The argument parser; each flag's default comes from its BEC_MCP_* variable."""
    parser = argparse.ArgumentParser(
        prog="bec-mcp-server",
        description="Serve read-only BEC diagnostics to an AI agent over MCP. Every flag can "
        "also be set with the BEC_MCP_* environment variable named in its help.",
    )
    parser.add_argument(
        "--transport",
        default=env_str("BEC_MCP_TRANSPORT", "stdio"),
        help="stdio (default) for an agent that starts the server itself, as `claude mcp add` "
        "does; streamable-http for an agent that connects over HTTP, e.g. from a container. "
        "[BEC_MCP_TRANSPORT]",
    )
    parser.add_argument(
        "--host",
        default=env_str("BEC_MCP_HOST", "127.0.0.1"),
        help="HTTP bind address (default 127.0.0.1). [BEC_MCP_HOST]",
    )
    parser.add_argument(
        "--port",
        type=int,
        default=env_int("BEC_MCP_PORT", 8000),
        help="HTTP port (default 8000). [BEC_MCP_PORT]",
    )
    parser.add_argument(
        "--path", default=env_str("BEC_MCP_PATH", "/mcp"), help="HTTP path. [BEC_MCP_PATH]"
    )
    parser.add_argument(
        "--mode",
        default=env_str("BEC_MCP_MODE", "diagnose"),
        help=f"Tool set to serve: {', '.join(MODE_TIERS)}. [BEC_MCP_MODE]",
    )
    parser.add_argument(
        "--token-file",
        default=None,
        metavar="PATH",
        help="File holding the bearer token HTTP clients must send. Tokens are never taken from "
        "the command line, where other users can see them. [BEC_MCP_TOKEN_FILE, or the token "
        "itself in BEC_MCP_TOKEN]",
    )
    parser.add_argument(
        "--allow-unauthenticated",
        action="store_true",
        default=env_bool("BEC_MCP_ALLOW_UNAUTHENTICATED"),
        help="Serve HTTP without a bearer token. Anyone who can reach the port, including any "
        "container on this host, can then read beamline state. [BEC_MCP_ALLOW_UNAUTHENTICATED]",
    )
    parser.add_argument(
        "--allowed-hosts",
        default=env_optional("BEC_MCP_ALLOWED_HOSTS"),
        metavar="HOSTS",
        help="Comma-separated extra Host header values the HTTP transport accepts, e.g. "
        "host.containers.internal:* for an agent in a podman container. Loopback names are "
        "always accepted; a name without a port matches any port. [BEC_MCP_ALLOWED_HOSTS]",
    )
    parser.add_argument(
        "--allowed-origins",
        default=env_optional("BEC_MCP_ALLOWED_ORIGINS"),
        metavar="ORIGINS",
        help="Comma-separated extra Origin header values the HTTP transport accepts, for "
        "browser-based clients. Requests without an Origin header are not affected. "
        "[BEC_MCP_ALLOWED_ORIGINS]",
    )
    parser.add_argument(
        "--tool-timeout",
        type=float,
        default=env_float("BEC_MCP_TOOL_TIMEOUT", DEFAULT_TOOL_TIMEOUT_S),
        metavar="SECONDS",
        help="Deadline for each tool call, so a wedged Redis becomes an error instead of a "
        f"hang (default {DEFAULT_TOOL_TIMEOUT_S:.0f}). [BEC_MCP_TOOL_TIMEOUT]",
    )
    parser.add_argument(
        "--max-bec-calls",
        type=int,
        default=env_int("BEC_MCP_MAX_BEC_CALLS", DEFAULT_MAX_BEC_CALLS),
        metavar="N",
        help="Tool calls that may wait on BEC at the same time, across all sessions "
        f"(default {DEFAULT_MAX_BEC_CALLS}). [BEC_MCP_MAX_BEC_CALLS]",
    )
    parser.add_argument(
        "--audit-log",
        default=env_optional("BEC_MCP_AUDIT_LOG"),
        metavar="PATH",
        help="JSONL file that gets one line per tool call (tool, arguments, duration, outcome); "
        "'off' disables it. Default: $XDG_STATE_HOME/bec-mcp-server/audit.jsonl, i.e. "
        "~/.local/state/bec-mcp-server/audit.jsonl. [BEC_MCP_AUDIT_LOG]",
    )
    parser.add_argument(
        "--log-level",
        default=env_str("BEC_MCP_LOG_LEVEL", "INFO").upper(),
        help="Server log level; logs go to stderr. [BEC_MCP_LOG_LEVEL]",
    )
    bec = parser.add_argument_group(
        "BEC connection",
        "The server reaches BEC through one BECClient. Without --redis or --config, Redis is "
        "found like for any BEC client: BEC_SERVICE_CONFIG, then the deployment config, then "
        "localhost:6379.",
    )
    bec.add_argument(
        "--redis",
        default=None,
        metavar="HOST:PORT",
        help="BEC's Redis. [BEC_REDIS_HOST, BEC_REDIS_PORT]",
    )
    bec.add_argument(
        "--config",
        default=None,
        metavar="PATH",
        help="BEC service config YAML, the same file the other BEC services use. "
        "[BEC_MCP_SERVICE_CONFIG]",
    )
    bec.add_argument(
        "--acl-file",
        default=None,
        metavar="PATH",
        help="Env file with REDIS_USER= and REDIS_PASSWORD= lines for this server's own Redis "
        "account, e.g. mcp_ro. BEC_REDIS_USERNAME and BEC_REDIS_PASSWORD, set together, win "
        "over it. [BEC_MCP_ACL_FILE]",
    )
    bec.add_argument(
        "--connect-on-startup",
        action="store_true",
        default=env_bool("BEC_MCP_CONNECT_ON_STARTUP"),
        help="Connect to BEC before serving and exit if that fails. By default the client "
        "starts on the first tool call, so the server answers while BEC is down. "
        "[BEC_MCP_CONNECT_ON_STARTUP]",
    )
    bec.add_argument(
        "--client-max-age",
        type=float,
        default=None,
        metavar="SECONDS",
        help="Restart the BEC client after this long, because some of its internal structures "
        f"only grow (default {DEFAULT_CLIENT_MAX_AGE_S:.0f}; 0 never restarts). "
        "[BEC_MCP_CLIENT_MAX_AGE]",
    )
    parser.add_argument(
        "--docs-index",
        default=env_optional("BEC_MCP_DOCS_INDEX"),
        metavar="PATH_OR_URL",
        help="Docs search index built from bec_docs: a .json or .json.gz file, or an http(s) URL "
        "(default: the latest index bec_docs publishes to gitea.psi.ch). [BEC_MCP_DOCS_INDEX]",
    )
    return parser


def parse_args(argv: list[str] | None = None) -> Settings:
    """Build the server settings from flags and the environment, refusing invalid values."""
    return settings_from_args(build_parser().parse_args(argv))


def settings_from_args(args: argparse.Namespace) -> Settings:
    """The server settings for parsed arguments, refusing invalid values."""
    # argparse would validate choices only for flags, not for defaults read from the
    # environment, so a typo in BEC_MCP_TRANSPORT must be caught here, not fall back to stdio
    if args.transport not in TRANSPORTS:
        raise SystemExit(
            f"invalid transport {args.transport!r} (--transport / BEC_MCP_TRANSPORT): use one of "
            f"{', '.join(TRANSPORTS)}"
        )
    if args.mode not in MODE_TIERS:
        raise SystemExit(
            f"invalid mode {args.mode!r} (--mode / BEC_MCP_MODE): use one of "
            f"{', '.join(MODE_TIERS)}"
        )
    if args.log_level.upper() not in LOG_LEVELS:
        raise SystemExit(f"invalid log level {args.log_level!r}: use one of {LOG_LEVELS}")
    return Settings(
        transport=args.transport,
        host=args.host,
        port=args.port,
        path=args.path,
        mode=args.mode,
        # the token is only read for HTTP, so a stdio server never fails on a stale token file
        token=resolve_token(args.token_file) if args.transport == "streamable-http" else None,
        allow_unauthenticated=args.allow_unauthenticated,
        allowed_hosts=parse_host_list(args.allowed_hosts, option="--allowed-hosts"),
        allowed_origins=parse_origin_list(args.allowed_origins, option="--allowed-origins"),
        tool_timeout=args.tool_timeout,
        max_bec_calls=args.max_bec_calls,
        audit_log=resolve_audit_log(args.audit_log),
        log_level=args.log_level.upper(),
        docs_index=args.docs_index,
    )


def connection_from_args(args: argparse.Namespace, settings: Settings) -> BecConnection | None:
    """The BEC connection for modes that read BEC; flags win over the environment."""
    if "diagnose" not in settings.tiers:
        return None
    environ = dict(os.environ)
    if args.acl_file:
        # --acl-file replaces BEC_MCP_ACL_FILE, like every other flag replaces its variable
        environ.pop("BEC_MCP_ACL_FILE", None)
    config = BecConnectionConfig.from_env(environ).with_overrides(
        redis=args.redis,
        service_config_path=args.config,
        acl_file=args.acl_file,
        client_max_age_s=args.client_max_age,
    )
    return BecConnection(config)


def run(app: BecMcpApp) -> None:
    """Serve the app on its configured transport until the client or a signal ends it."""
    settings = app.context.settings
    if settings.transport == "streamable-http":
        run_http(app.server, settings)
    else:
        run_stdio(app.server)


def main(argv: list[str] | None = None) -> None:
    """Entry point of the bec-mcp-server command."""
    args = build_parser().parse_args(argv)
    settings = settings_from_args(args)
    connection = connection_from_args(args, settings)
    # SIGTERM (systemd stop, podman stop, an MCP client ending the session) must take the same
    # path as Ctrl-C: uvicorn re-raises the signal after its graceful shutdown, and with the
    # default handler the process would die there without closing the BEC connection
    signal.signal(signal.SIGTERM, signal.default_int_handler)
    app = build_app(settings, connection=connection)
    try:
        if connection is not None and args.connect_on_startup:
            _connect_or_exit(connection)
        run(app)
    except KeyboardInterrupt:
        pass
    finally:
        # BEC connector threads are not daemons: without an explicit shutdown the process would
        # not exit after stdio EOF or SIGINT
        app.close()
    _exit_if_stuck()


def _connect_or_exit(connection: BecConnection) -> None:
    try:
        connection.connect()
    except BecUnavailableError as exc:
        raise SystemExit(f"bec-mcp-server: cannot connect to BEC at startup: {exc}") from None


def _exit_if_stuck(grace: float = EXIT_GRACE_S) -> None:
    """Exit even if a worker thread is still blocked in a call whose caller gave up.

    Calls that pass their deadline are abandoned, not killed, and AnyIO's worker threads are
    not daemons, so one call stuck on a wedged Redis would otherwise keep the process alive
    after the transport has ended.
    """
    deadline = time.monotonic() + grace
    for thread in _other_live_threads():
        thread.join(max(0.0, deadline - time.monotonic()))
    stuck = [thread.name for thread in _other_live_threads()]
    if stuck:
        logger.warning("exiting with threads still blocked: %s", ", ".join(stuck))
        logging.shutdown()
        os._exit(0)


def _other_live_threads() -> list[threading.Thread]:
    current = threading.current_thread()
    return [
        thread
        for thread in threading.enumerate()
        if thread is not current and not thread.daemon and thread.is_alive()
    ]
