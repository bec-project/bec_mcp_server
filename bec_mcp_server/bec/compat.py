"""The one place that reaches into private bec_lib attributes.

Tools use the public bec_lib API wherever one exists. What has no public accessor yet goes
through a function here, so a bec_lib release that renames an attribute breaks this module and
its BEC 4 tests, not every tool. Each function is a candidate for a public accessor upstream.

Rules for additions: one small function per private access, a docstring naming the attribute
and the bec_lib version it was checked against, a defensive fallback where the attribute may be
missing, and a test in tests/test_compat.py.
"""

# pylint: disable=protected-access

from __future__ import annotations

import contextlib
from collections.abc import Iterator
from typing import Any

try:
    from bec_lib.device import rpc_method_context
except ImportError:  # pragma: no cover - bec_lib without the RPC context hook
    rpc_method_context = None


def own_service_name(client: Any) -> str | None:
    """Name this client publishes its service status under (``BECService._service_name``).

    For a non-unique service such as BECMCPClient this is ``<name>/<uuid>``, which tells this
    process's own entry apart from other MCP servers on the same beamline. Checked on 4.1.6.
    """
    try:
        return str(client._service_name)
    except AttributeError:
        return None


def service_metrics(client: Any) -> dict[str, Any]:
    """Latest ServiceMetricMessage per service name (``BECService._services_metric``).

    BECService refreshes this cache together with the public ``client.service_status``, so read
    that property first. The metrics key expires 30 s after a service stops publishing; the status
    key expires after 6 s. Checked on 4.1.6.
    """
    metrics = getattr(client, "_services_metric", None)
    return dict(metrics) if isinstance(metrics, dict) else {}


def device_config(device: Any) -> Any:
    """Full device config dict as loaded from the session (``device.root._config``).

    Prefer public device properties (enabled, readout_priority, ...) where they exist; this is for
    the remaining fields. Checked on 4.1.6.
    """
    return getattr(device.root, "_config", None)


def device_info(device: Any) -> dict[str, Any]:
    """Device-info payload the device server published for a device (``DeviceBase._info``).

    It holds the signal tree with kinds and metadata, hints, the describe output, sub-devices and
    the custom user-access methods. Empty when the device server sent none. Checked on 4.1.6.
    """
    info = getattr(device, "_info", None)
    return dict(info) if isinstance(info, dict) else {}


class DeviceRPCRefused(RuntimeError):
    """Raised when code inside forbid_device_rpc() tries to call a device over RPC."""


def _refuse_rpc(device: str, func_call: str, *_args: Any, **_kwargs: Any) -> Any:
    raise DeviceRPCRefused(
        f"refused device RPC {device}.{func_call}: this server reads cached values only"
    )


@contextlib.contextmanager
def forbid_device_rpc() -> Iterator[None]:
    """Make every device RPC in this context raise instead of reaching the scan queue.

    A non-cached read, a get() on a signal, a cached read of an ``omitted`` signal, and touching
    a device attribute that bec_lib serves over RPC all dispatch through
    ``bec_lib.device.rpc_method_context``; setting it here refuses them in the current thread.
    Checked on 4.1.6.
    """
    if rpc_method_context is None:  # pragma: no cover - older bec_lib
        yield
        return
    token = rpc_method_context.set(_refuse_rpc)
    try:
        yield
    finally:
        rpc_method_context.reset(token)


def refresh_queue(queue_storage: Any) -> None:
    """Refresh the cached scan queue from Redis (``QueueStorage._update_queue``).

    Checked on 4.1.6.
    """
    queue_storage._update_queue()
