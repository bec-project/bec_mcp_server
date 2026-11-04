"""One BECClient behind the MCP server.

The server never opens its own Redis connection: every read goes through a single BECClient that
``BecConnection`` starts lazily on the first tool call, verifies, restarts when it gets too old or
after a Redis failure, and shuts down when the server exits.

Tools borrow the client for the length of one call::

    with connection.use() as client:
        statuses = client.service_status

Inside ``use()`` the thread's stdout goes to stderr, and a Redis failure becomes a
``BecUnavailableError`` whose message says what to check. That client is then replaced on the
next call, because pub/sub updates sent while Redis was away are lost for good.
"""

from __future__ import annotations

import dataclasses
import logging
import os
import threading
import time
from collections.abc import Callable, Iterator, Mapping
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any

from bec_mcp_server.bec import stdout_guard

if TYPE_CHECKING:  # pragma: no cover
    from bec_lib.client import BECClient
    from bec_lib.service_config import ServiceConfig

logger = logging.getLogger(__name__)

#: name the client registers under in BEC's service status (``BECMCPClient/<uuid>``)
SERVICE_NAME = "BECMCPClient"

#: restart the client after this long (BMS-019): some of its internal structures only grow
DEFAULT_CLIENT_MAX_AGE_S = 6 * 3600.0

#: bound for every Redis connect and reply on the client's connections. bec_lib's own blocking
#: reads wait at most 200 ms, so this only fires on an unreachable or wedged Redis.
DEFAULT_REDIS_TIMEOUT_S = 5.0

#: default log directory when no site service config chooses one (BMS-015)
DEFAULT_LOG_DIR = Path.home() / ".cache" / "bec-mcp-server" / "logs"


class BecUnavailableError(ConnectionError):
    """BEC cannot be reached or refused this server. The message says what to check."""


@dataclass(frozen=True)
class BecConnectionConfig:
    """How to reach BEC. Every field is optional.

    When host and port are None, Redis is resolved like for any other BEC client: the service
    config file, then BEC_SERVICE_CONFIG, then the deployment config, then localhost:6379.
    """

    host: str | None = None
    port: int | None = None
    username: str | None = None
    password: str | None = None
    service_config_path: str | None = None
    log_dir: str | None = None
    client_max_age_s: float = DEFAULT_CLIENT_MAX_AGE_S
    redis_timeout_s: float = DEFAULT_REDIS_TIMEOUT_S

    @classmethod
    def from_env(cls, environ: Mapping[str, str] | None = None) -> BecConnectionConfig:
        """Read BEC_REDIS_*, BEC_MCP_ACL_FILE, BEC_MCP_SERVICE_CONFIG and BEC_MCP_LOG_DIR."""
        env = os.environ if environ is None else environ
        username = env.get("BEC_REDIS_USERNAME") or None
        password = env.get("BEC_REDIS_PASSWORD") or None
        acl_file = env.get("BEC_MCP_ACL_FILE") or None
        if acl_file:
            if username is None and password is None:
                # explicit env credentials win over the file; the file is the service path
                username, password = read_acl_env_file(acl_file)
            elif username is None or password is None:
                # half-set credentials next to an ACL file are ambiguous; authenticating with a
                # username and no password would fail with no hint that the file was ignored
                raise SystemExit(
                    "BEC_REDIS_USERNAME and BEC_REDIS_PASSWORD must be set together when "
                    "BEC_MCP_ACL_FILE is also configured: set both to override the file, "
                    "or neither to use it"
                )
        return cls(
            host=env.get("BEC_REDIS_HOST") or None,
            port=_env_number(env, "BEC_REDIS_PORT", int),
            username=username,
            password=password,
            service_config_path=env.get("BEC_MCP_SERVICE_CONFIG") or None,
            log_dir=env.get("BEC_MCP_LOG_DIR") or None,
            client_max_age_s=_env_number(
                env, "BEC_MCP_CLIENT_MAX_AGE", float, DEFAULT_CLIENT_MAX_AGE_S, minimum=0
            ),
            redis_timeout_s=_env_number(
                env, "BEC_MCP_REDIS_TIMEOUT", float, DEFAULT_REDIS_TIMEOUT_S, minimum=0.1
            ),
        )

    def with_overrides(
        self,
        *,
        redis: str | None = None,
        service_config_path: str | None = None,
        acl_file: str | None = None,
        client_max_age_s: float | None = None,
    ) -> BecConnectionConfig:
        """Apply command-line overrides (``--redis``, ``--config``, ``--acl-file``, ...).

        As with BEC_MCP_ACL_FILE, explicit BEC_REDIS_USERNAME/BEC_REDIS_PASSWORD win over
        ``acl_file``.
        """
        changes: dict[str, Any] = {}
        if redis:
            changes["host"], changes["port"] = parse_redis_address(redis)
        if service_config_path:
            changes["service_config_path"] = service_config_path
        if acl_file and self.username is None and self.password is None:
            changes["username"], changes["password"] = read_acl_env_file(acl_file)
        if client_max_age_s is not None:
            if client_max_age_s < 0:
                raise SystemExit("the client max age must be 0 (no restarts) or positive")
            changes["client_max_age_s"] = client_max_age_s
        return dataclasses.replace(self, **changes) if changes else self


def _env_number(
    env: Mapping[str, str],
    name: str,
    kind: type[int] | type[float],
    default: Any = None,
    minimum: float | None = None,
) -> Any:
    raw = env.get(name)
    if not raw:
        return default
    try:
        value = kind(raw)
    except ValueError:
        # a deployment typo must produce a readable startup error, not a traceback
        raise SystemExit(
            f"environment variable {name} must be a number ({kind.__name__}), got {raw!r}"
        ) from None
    if minimum is not None and value < minimum:
        raise SystemExit(f"environment variable {name} must be at least {minimum}, got {raw!r}")
    return value


def parse_redis_address(value: str) -> tuple[str | None, int | None]:
    """Split ``HOST:PORT``, ``HOST`` or ``[::1]:PORT`` into host and port."""
    # rpartition keeps IPv6 hosts intact, mirroring bec_lib's own parsing
    host, sep, port = value.rpartition(":")
    if not sep or "]" in port:
        host, port = value, ""
    try:
        parsed_port = int(port) if port else None
    except ValueError:
        raise SystemExit(f"invalid Redis address {value!r}: the port must be an integer") from None
    return host.strip("[]") or None, parsed_port


def read_acl_env_file(path: str) -> tuple[str, str]:
    """Read REDIS_USER and REDIS_PASSWORD from a ``.bec_acl.env``-style file.

    Same format the auto-deployed BEC services use. bec_lib parses those files with dotenv,
    which accepts ``export KEY=...`` and strips only matched quote pairs; this mirrors that
    without the dependency.
    """
    user: str | None = None
    password: str | None = None
    try:
        lines = Path(path).expanduser().read_text(encoding="utf-8").splitlines()
    except OSError as exc:
        raise SystemExit(f"could not read ACL env file {path!r}: {exc}") from None
    for line in lines:
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        line = line.removeprefix("export ").lstrip()
        key, _, value = line.partition("=")
        key, value = key.strip(), value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
            value = value[1:-1]
        if key == "REDIS_USER":
            user = value
        elif key == "REDIS_PASSWORD":
            password = value
    if not user or not password:
        raise SystemExit(f"ACL env file {path!r} must define REDIS_USER and REDIS_PASSWORD")
    return user, password


def _redis_failure_types() -> tuple[type[BaseException], ...]:
    import redis.exceptions  # pylint: disable=import-outside-toplevel

    return (
        redis.exceptions.ConnectionError,
        redis.exceptions.TimeoutError,
        redis.exceptions.BusyLoadingError,
    )


def _fail_fast_connector_cls(timeout_s: float) -> type:
    """RedisConnector that gives up on a lost Redis instead of hanging."""
    # pylint: disable=import-outside-toplevel
    from bec_lib.redis_connector import RedisConnector

    class FailFastRedisConnector(RedisConnector):
        """RedisConnector with socket timeouts and without bec_lib's connection retries."""

        def __init__(self, bootstrap: Any, *args: Any, **kwargs: Any) -> None:
            kwargs.setdefault("socket_connect_timeout", timeout_s)
            kwargs.setdefault("socket_timeout", timeout_s)
            super().__init__(bootstrap, *args, **kwargs)

        def set_retry_enabled(self, enabled: bool) -> None:
            # BECService enables 20 retries with backoff of up to 30 s each once it has started.
            # redis-py copies the policy into every connection it opens, so switching it off
            # later misses the connections the client's stream thread already holds: a lost
            # Redis then blocks that thread, and its shutdown, for minutes. A diagnostic call
            # fails fast instead, and the next call starts a fresh client.
            super().set_retry_enabled(False)

    return FailFastRedisConnector


def _default_client_factory(service_config: ServiceConfig, connector_cls: type) -> BECClient:
    from bec_lib.client import BECClient  # pylint: disable=import-outside-toplevel

    return BECClient(
        config=service_config,
        connector_cls=connector_cls,
        wait_for_server=False,
        forced=True,
        name=SERVICE_NAME,
        prompt_for_acl=False,
    )


ClientFactory = Callable[["ServiceConfig", type], Any]


class BecConnection:
    """Owns the server's single BECClient: lazy start, verification, lifetime, shutdown."""

    #: bounded wait for another thread's client start, which can take seconds and must not pin
    #: every worker thread while Redis is half dead
    START_LOCK_TIMEOUT_S = 15.0

    #: per-thread join timeout when shutting a client down; its threads are not daemons (BMS-008)
    SHUTDOWN_THREAD_TIMEOUT_S = 5.0

    def __init__(
        self,
        config: BecConnectionConfig | None = None,
        *,
        client_factory: ClientFactory | None = None,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self.config = config or BecConnectionConfig.from_env()
        self._client_factory = client_factory or _default_client_factory
        self._clock = clock
        # held across client start and shutdown (slow, Redis I/O)
        self._start_lock = threading.Lock()
        # guards the fields below; never held across I/O
        self._state_lock = threading.Lock()
        self._client: Any = None
        self._generation = 0
        self._started_at: float | None = None
        self._in_flight = 0
        self._restart_reason: str | None = None
        self._closed = False
        self._starts = 0
        self._last_restart_reason: str | None = None
        self._last_error: str | None = None
        self._last_error_at: float | None = None
        self._redis_target: str | None = None

    # ------------------------------------------------------------------ borrowing the client

    @contextmanager
    def use(self) -> Iterator[BECClient]:
        """Borrow the started client for one tool call.

        Starts or restarts the client when needed. Raises BecUnavailableError when BEC cannot
        be reached, and turns a Redis failure inside the block into one as well.
        """
        with stdout_guard.guarded():
            client, generation = self._acquire()
            try:
                yield client
            except _redis_failure_types() as exc:
                message = self._describe_redis_failure(exc)
                self._mark_broken(generation, message)
                raise BecUnavailableError(message) from exc
            finally:
                with self._state_lock:
                    self._in_flight -= 1

    def run(self, func: Callable[..., Any], /, *args: Any, **kwargs: Any) -> Any:
        """Call ``func(client, *args, **kwargs)`` inside ``use()`` (blocking; offload it)."""
        with self.use() as client:
            return func(client, *args, **kwargs)

    def connect(self) -> None:
        """Start the client now instead of on the first tool call (fail fast at startup)."""
        with self.use():
            pass

    def _acquire(self) -> tuple[Any, int]:
        # a bounded acquire, which `with` cannot express
        if not self._start_lock.acquire(  # pylint: disable=consider-using-with
            timeout=self.START_LOCK_TIMEOUT_S
        ):
            raise BecUnavailableError(
                "another call is still connecting to BEC; retry in a few seconds"
            )
        # The start lock is held from here on, so only shutdown() and finishing calls change
        # the state concurrently; neither can install or remove a client.
        try:
            self._restart_if_due()
            with self._state_lock:
                if self._closed:
                    raise BecUnavailableError("the BEC connection has been shut down")
                need_start = self._client is None
            if need_start:
                client = self._start_client()
                with self._state_lock:
                    closed = self._closed
                    if not closed:
                        self._client = client
                        self._generation += 1
                        self._started_at = self._clock()
                        self._restart_reason = None
                        self._starts += 1
                if closed:
                    # shutdown() gave up waiting for this start; do not keep a live client
                    self._safe_shutdown(client)
                    raise BecUnavailableError("the BEC connection has been shut down")
            with self._state_lock:
                self._in_flight += 1
                return self._client, self._generation
        finally:
            self._start_lock.release()

    def _restart_if_due(self) -> None:
        """Replace the client if it is due for a restart and no call is using it.

        Called with the start lock held, so no other call can install or remove a client.
        """
        with self._state_lock:
            client = self._client
            if client is None or self._in_flight:
                # a busy client restarts on the first call that finds it idle
                return
            reason = self._restart_due_locked()
            broken = self._restart_reason is not None
        if reason is None:
            return
        if broken and not self._connector_alive(client):
            # Redis is still gone: shutting the client down and starting a new one would each
            # wait on it again, so keep the broken client until Redis answers
            raise self._fail(
                f"BEC's Redis at {self.redis_target} still does not answer. Check that BEC and "
                "its Redis are running. The next call tries again."
            )
        with self._state_lock:
            self._client = None
            self._started_at = None
            self._last_restart_reason = reason
        logger.info("restarting the BEC client: %s", reason)
        self._safe_shutdown(client)

    def _restart_due_locked(self) -> str | None:
        if self._restart_reason is not None:
            return self._restart_reason
        max_age = self.config.client_max_age_s
        if max_age and self._started_at is not None:
            age = self._clock() - self._started_at
            if age >= max_age:
                return f"client reached its maximum age ({age:.0f} s >= {max_age:.0f} s)"
        return None

    def _mark_broken(self, generation: int, message: str) -> None:
        with self._state_lock:
            self._record_error_locked(message)
            if generation == self._generation and self._client is not None:
                self._restart_reason = f"Redis failure: {message}"

    def _record_error_locked(self, message: str) -> None:
        self._last_error = message
        self._last_error_at = time.time()

    # ------------------------------------------------------------------ starting a client

    def _start_client(self) -> Any:
        try:
            service_config = self._build_service_config()
        except FileNotFoundError as exc:
            raise self._fail(f"BEC service config not found: {exc}") from exc
        self._redis_target = str(service_config.redis)
        client = self._client_factory(
            service_config, _fail_fast_connector_cls(self.config.redis_timeout_s)
        )
        try:
            client.start()
        except Exception as exc:  # pylint: disable=broad-except
            self._safe_shutdown(client)
            raise self._fail(self._describe_start_failure(exc)) from exc
        problem = self._check_started(client)
        if problem is not None:
            self._safe_shutdown(client)
            raise self._fail(problem)
        logger.info("BEC client started against Redis %s", self._redis_target)
        return client

    def _fail(self, message: str) -> BecUnavailableError:
        with self._state_lock:
            self._record_error_locked(message)
        return BecUnavailableError(message)

    def _check_started(self, client: Any) -> str | None:
        # BECClient.start() swallows redis ConnectionError (logs it, shuts itself down and
        # returns), so a failed connect only shows here (BMS-003). Caching that client would
        # serve empty devices and queues as if the beamline were idle.
        if getattr(client, "device_manager", None) is None or not self._connector_alive(client):
            return (
                f"Could not connect to BEC's Redis at {self.redis_target}. Check that BEC is "
                "running and that --redis, BEC_REDIS_HOST/BEC_REDIS_PORT or the service config "
                "point at the right instance. The next call tries again."
            )
        expected = self.config.username
        if expected:
            try:
                actual = str(client.connector.username)
            except Exception:  # pylint: disable=broad-except
                actual = None
            if actual != expected:
                # bec_lib falls back to the default account when the configured one is refused;
                # on an open Redis that would silently give this server more rights than intended
                return (
                    f"Redis at {self.redis_target} did not accept the account {expected!r} "
                    f"(the client ended up as {actual!r}). Check the password in the ACL file "
                    "or BEC_REDIS_USERNAME/BEC_REDIS_PASSWORD, and that the account exists."
                )
        return None

    @staticmethod
    def _connector_alive(client: Any) -> bool:
        try:
            return bool(client.connector.ping())
        except Exception:  # pylint: disable=broad-except
            return False

    def _describe_start_failure(self, exc: BaseException) -> str:
        name = type(exc).__name__
        if name == "BECAuthenticationError":
            return (
                f"Redis at {self.redis_target} refused every account this server tried. "
                "Give the server its own account with --acl-file or BEC_MCP_ACL_FILE."
            )
        if isinstance(exc, _redis_failure_types()):
            return self._describe_redis_failure(exc)
        return f"Starting the BEC client failed ({name}: {exc})."

    def _describe_redis_failure(self, exc: BaseException) -> str:
        import redis.exceptions  # pylint: disable=import-outside-toplevel

        if isinstance(exc, redis.exceptions.TimeoutError):
            return (
                f"BEC's Redis at {self.redis_target} did not answer within "
                f"{self.config.redis_timeout_s:g} s. It may be overloaded or hung. "
                "The next call reconnects."
            )
        return (
            f"Lost the connection to BEC's Redis at {self.redis_target} ({exc}). Check that "
            "BEC is running. The next call reconnects."
        )

    def _build_service_config(self) -> ServiceConfig:
        from bec_lib.service_config import ServiceConfig  # pylint: disable=import-outside-toplevel

        return ServiceConfig(
            config_path=self.config.service_config_path,
            config_name="client",
            **self._service_config_overrides(),
        )

    def _service_config_overrides(self) -> dict[str, Any]:
        overrides: dict[str, Any] = {}
        if self.config.host is not None or self.config.port is not None:
            overrides["redis"] = {
                "host": self.config.host or "localhost",
                "port": self.config.port or 6379,
            }
        if self.config.username is not None or self.config.password is not None:
            # env_file "" stops bec_lib from trying the deployment's .bec_acl.env first, which
            # holds the services' own account, before the credentials given here
            overrides["acl"] = {
                "env_file": "",
                "user": self.config.username or "default",
                "password": self.config.password,
            }
        log_dir = self._resolve_log_dir()
        if log_dir is not None:
            overrides["log_writer"] = {"base_path": str(log_dir)}
        return overrides

    def _resolve_log_dir(self) -> Path | None:
        """Keep log files out of the working directory (BMS-015).

        A site service config (--config or BEC_SERVICE_CONFIG) keeps its own log_writer, and
        BEC_SERVICE_CONFIG itself is never modified (BMS-014).
        """
        if self.config.log_dir:
            log_dir = Path(self.config.log_dir).expanduser()
        elif self.config.service_config_path or os.environ.get("BEC_SERVICE_CONFIG"):
            return None
        else:
            log_dir = DEFAULT_LOG_DIR
        try:
            log_dir.mkdir(parents=True, exist_ok=True)
        except OSError:
            logger.warning("cannot create log directory %s", log_dir, exc_info=True)
        return log_dir

    # ------------------------------------------------------------------ shutdown and status

    def _safe_shutdown(self, client: Any) -> None:
        try:
            client.shutdown(per_thread_timeout_s=self.SHUTDOWN_THREAD_TIMEOUT_S)
        except Exception:  # pylint: disable=broad-except
            logger.warning("BEC client shutdown raised", exc_info=True)

    def shutdown(self) -> None:
        """Shut the client and its non-daemon threads down (BMS-008). Idempotent and bounded."""
        with self._state_lock:
            self._closed = True
        # if a wedged start never releases the lock, give up: the closed flag stops that start
        # from keeping its client, and leaking a half-started client beats hanging the exit
        if not self._start_lock.acquire(  # pylint: disable=consider-using-with
            timeout=self.START_LOCK_TIMEOUT_S
        ):
            logger.warning("BEC client start still running at shutdown; not waiting for it")
            return
        try:
            with self._state_lock:
                client, self._client = self._client, None
                self._started_at = None
            if client is not None:
                self._safe_shutdown(client)
        finally:
            self._start_lock.release()

    @property
    def redis_target(self) -> str:
        """The Redis address in use, or the one that will be used."""
        if self._redis_target:
            return self._redis_target
        if self.config.host is not None or self.config.port is not None:
            return f"{self.config.host or 'localhost'}:{self.config.port or 6379}"
        return "the Redis from the BEC service config (default localhost:6379)"

    def state(self) -> dict[str, Any]:
        """Connection state for mcp_status. Never starts the client or touches Redis."""
        with self._state_lock:
            if self._closed:
                state = "closed"
            elif self._client is not None:
                state = "restart_pending" if self._restart_reason else "connected"
            elif self._last_error is not None:
                state = "failed"
            else:
                state = "not_connected"
            age = (
                round(self._clock() - self._started_at, 1)
                if self._client is not None and self._started_at is not None
                else None
            )
            return {
                "state": state,
                "redis": self.redis_target,
                "account": self.config.username or "default (bec_lib resolution)",
                "client_age_s": age,
                "client_max_age_s": self.config.client_max_age_s or None,
                "client_starts": self._starts,
                "calls_in_flight": self._in_flight,
                "last_restart_reason": self._last_restart_reason,
                "last_error": self._last_error,
                "last_error_age_s": (
                    round(time.time() - self._last_error_at, 1)
                    if self._last_error_at is not None
                    else None
                ),
            }
