"""Server settings: one frozen object built from CLI flags and BEC_MCP_* environment variables."""

from __future__ import annotations

import dataclasses
import os
from pathlib import Path
from typing import Literal
from urllib.parse import urlsplit

Transport = Literal["stdio", "streamable-http"]

TRANSPORTS: tuple[str, ...] = ("stdio", "streamable-http")

#: server modes and the capability tiers each one exposes; tools are labeled by tier and
#: graduate by re-labeling, never by renaming. v1 ships diagnose only, which includes the "learn"
#: tier (docs search); write tiers come back later behind their own Redis account
MODE_TIERS: dict[str, frozenset[str]] = {"diagnose": frozenset({"diagnose", "learn"})}

LOG_LEVELS: tuple[str, ...] = ("DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL")

#: per-tool-call deadline; a wedged Redis becomes an actionable error instead of a hang
DEFAULT_TOOL_TIMEOUT_S = 30.0

#: in-flight calls in the BEC pool across all sessions
DEFAULT_MAX_BEC_CALLS = 8

LOOPBACK_HOSTS = frozenset({"localhost", "127.0.0.1", "::1"})

_LOOPBACK_ALLOWED_HOSTS = ("127.0.0.1:*", "localhost:*", "[::1]:*")
_LOOPBACK_ALLOWED_ORIGINS = ("http://127.0.0.1:*", "http://localhost:*", "http://[::1]:*")

#: values of BEC_MCP_AUDIT_LOG / --audit-log that switch the audit log off
_AUDIT_OFF = frozenset({"off", "none", "no", "false", "0"})


@dataclasses.dataclass(frozen=True)
class Settings:
    """Everything the server needs to run; defaults match a local stdio session.

    Embedders and tests build this directly; ``cli.parse_args`` builds it from flags and the
    environment. The audit log is off unless a path is given, so building a server in a test
    never writes to the user's state directory.
    """

    transport: Transport = "stdio"
    host: str = "127.0.0.1"
    port: int = 8000
    path: str = "/mcp"
    mode: str = "diagnose"
    token: str | None = dataclasses.field(default=None, repr=False)
    allow_unauthenticated: bool = False
    allowed_hosts: tuple[str, ...] = ()
    allowed_origins: tuple[str, ...] = ()
    tool_timeout: float = DEFAULT_TOOL_TIMEOUT_S
    max_bec_calls: int = DEFAULT_MAX_BEC_CALLS
    audit_log: Path | None = None
    log_level: str = "INFO"
    #: path or http(s) URL of the docs index; None downloads the one the bec_docs deploy publishes
    docs_index: str | None = None

    def __post_init__(self) -> None:
        if self.transport not in TRANSPORTS:
            raise SystemExit(
                f"invalid transport {self.transport!r}: use one of {', '.join(TRANSPORTS)}"
            )
        if self.mode not in MODE_TIERS:
            raise SystemExit(f"invalid mode {self.mode!r}: use one of {', '.join(MODE_TIERS)}")
        if self.tool_timeout <= 0:
            raise SystemExit("BEC_MCP_TOOL_TIMEOUT/--tool-timeout must be greater than 0")
        if self.max_bec_calls < 1:
            raise SystemExit("BEC_MCP_MAX_BEC_CALLS/--max-bec-calls must be at least 1")
        if self.log_level not in LOG_LEVELS:
            raise SystemExit(f"invalid log level {self.log_level!r}: use one of {LOG_LEVELS}")

    @property
    def tiers(self) -> frozenset[str]:
        """Capability tiers whose tools this server registers."""
        return MODE_TIERS[self.mode]

    @property
    def http_url(self) -> str:
        """The URL an HTTP client on this host would use."""
        host = f"[{self.host}]" if ":" in self.host and not self.host.startswith("[") else self.host
        return f"http://{host}:{self.port}{self.path}"

    def transport_allowed_hosts(self) -> list[str]:
        """Host header values the HTTP transport accepts (DNS-rebinding protection)."""
        hosts = list(_LOOPBACK_ALLOWED_HOSTS)
        bind = self.host.strip("[]").lower()
        # wildcard binds are not names a client can send; list those with --allowed-hosts
        if bind not in LOOPBACK_HOSTS and bind not in ("0.0.0.0", "::", ""):
            hosts.extend(_host_patterns(f"[{bind}]" if ":" in bind else bind))
        for entry in self.allowed_hosts:
            hosts.extend(_host_patterns(entry))
        return _unique(hosts)

    def transport_allowed_origins(self) -> list[str]:
        """Origin header values the HTTP transport accepts; requests without Origin pass."""
        origins = list(_LOOPBACK_ALLOWED_ORIGINS)
        for entry in self.allowed_origins:
            origins.extend(_origin_patterns(entry))
        return _unique(origins)


def _unique(values: list[str]) -> list[str]:
    return list(dict.fromkeys(values))


def _host_patterns(entry: str) -> list[str]:
    """``name`` allows that name on any port; ``name:port`` and ``name:*`` are used as given."""
    if entry.startswith("["):
        # IPv6 literal: "[::1]", "[::1]:8000" or "[::1]:*"
        closing = entry.find("]")
        rest = entry[closing + 1 :] if closing != -1 else None
        if rest is None or (rest and not rest.startswith(":")):
            raise SystemExit(f"invalid allowed host {entry!r}")
        has_port = bool(rest)
    elif entry.count(":") > 1:
        raise SystemExit(f"invalid allowed host {entry!r}: write IPv6 addresses as [addr]")
    else:
        has_port = ":" in entry
    return [entry] if has_port else [entry, f"{entry}:*"]


def _origin_patterns(entry: str) -> list[str]:
    """Bare names allow http and https on any port; full origins are used as given."""
    if "://" not in entry:
        patterns = _host_patterns(entry)
        return [f"{scheme}://{pattern}" for scheme in ("http", "https") for pattern in patterns]
    origin = entry.rstrip("/")
    wildcard_port = origin.endswith(":*")
    try:
        parsed = urlsplit(origin[:-2] if wildcard_port else origin)
        port = parsed.port
    except ValueError:
        raise SystemExit(f"invalid allowed origin {entry!r}") from None
    if parsed.scheme not in ("http", "https") or not parsed.hostname or parsed.path:
        raise SystemExit(f"invalid allowed origin {entry!r}: give http(s)://host[:port] only")
    return [origin] if wildcard_port or port is not None else [origin, f"{origin}:*"]


def parse_host_list(raw: str | None, *, option: str) -> tuple[str, ...]:
    """Split a comma-separated --allowed-hosts value and reject entries that are not host names."""
    entries = []
    for entry in (raw or "").split(","):
        entry = entry.strip()
        if not entry:
            continue
        if "://" in entry or "/" in entry or entry == "*":
            raise SystemExit(
                f"invalid {option} entry {entry!r}: give a host name such as "
                "host.containers.internal, optionally with :port or :*"
            )
        _host_patterns(entry)
        entries.append(entry)
    return tuple(entries)


def parse_origin_list(raw: str | None, *, option: str) -> tuple[str, ...]:
    """Split a comma-separated --allowed-origins value and reject entries that are not origins."""
    entries = []
    for entry in (raw or "").split(","):
        entry = entry.strip()
        if not entry:
            continue
        if entry in ("*", "null"):
            raise SystemExit(f"invalid {option} entry {entry!r}: list each allowed origin")
        _origin_patterns(entry)
        entries.append(entry)
    return tuple(entries)


def default_audit_log_path() -> Path:
    """$XDG_STATE_HOME/bec-mcp-server/audit.jsonl, falling back to ~/.local/state."""
    state_home = os.environ.get("XDG_STATE_HOME") or str(Path.home() / ".local" / "state")
    return Path(state_home).expanduser() / "bec-mcp-server" / "audit.jsonl"


def resolve_audit_log(raw: str | None) -> Path | None:
    """``None`` selects the default location; off/none/false/0 disable the audit log."""
    if raw is None:
        return default_audit_log_path()
    if raw.strip().lower() in _AUDIT_OFF:
        return None
    return Path(raw).expanduser()


def resolve_token(token_file: str | None) -> str | None:
    """Token precedence: --token-file > BEC_MCP_TOKEN_FILE > BEC_MCP_TOKEN.

    Tokens are never accepted directly on the command line, because argv is visible to every
    local user in the process listing.
    """
    token_file = token_file or os.environ.get("BEC_MCP_TOKEN_FILE")
    if token_file:
        try:
            token = Path(token_file).expanduser().read_text(encoding="utf-8").strip()
        except OSError as exc:
            raise SystemExit(f"could not read token file {token_file!r}: {exc}") from None
        if not token:
            raise SystemExit(f"token file {token_file!r} is empty")
    else:
        token = os.environ.get("BEC_MCP_TOKEN") or None
    if token is not None and not token.isascii():
        # the bearer check compares raw header bytes, which HTTP carries as latin-1
        raise SystemExit("the bearer token must be ASCII")
    return token


def env_str(name: str, default: str) -> str:
    """Read a string variable; empty values count as unset for every BEC_MCP_* variable."""
    return os.environ.get(name) or default


def env_optional(name: str) -> str | None:
    """Read a variable that has no default."""
    return os.environ.get(name) or None


def env_int(name: str, default: int) -> int:
    """Read an integer variable, exiting with a readable message on a typo."""
    raw = os.environ.get(name)
    if not raw:
        return default
    try:
        return int(raw)
    except ValueError:
        # a deployment typo must produce a readable startup error, not a traceback
        raise SystemExit(f"environment variable {name} must be an integer, got {raw!r}") from None


def env_float(name: str, default: float) -> float:
    """Read a number variable, exiting with a readable message on a typo."""
    raw = os.environ.get(name)
    if not raw:
        return default
    try:
        return float(raw)
    except ValueError:
        raise SystemExit(f"environment variable {name} must be a number, got {raw!r}") from None


def env_bool(name: str) -> bool:
    """Read a flag variable: 1, true, yes and on switch it on."""
    return (os.environ.get(name) or "").strip().lower() in ("1", "true", "yes", "on")
