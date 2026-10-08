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

from typing import Any


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


def refresh_queue(queue_storage: Any) -> None:
    """Refresh the cached scan queue from Redis (``QueueStorage._update_queue``).

    Checked on 4.1.6.
    """
    queue_storage._update_queue()
