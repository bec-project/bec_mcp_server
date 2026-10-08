"""compat reaches into private bec_lib attributes; these tests fail first when bec_lib moves them."""

import inspect
from types import SimpleNamespace

from bec_lib.bec_service import BECService
from bec_lib.device import DeviceBase
from bec_lib.queue_items import QueueStorage

from bec_mcp_server.bec import compat


def test_bec_lib_still_has_the_private_attributes_compat_uses():
    assert isinstance(inspect.getattr_static(BECService, "_service_name"), property)
    assert "self._services_metric" in inspect.getsource(BECService._update_existing_services)
    assert callable(QueueStorage._update_queue)


def test_own_service_name_and_metrics():
    client = SimpleNamespace(_service_name="BECMCPClient/abc", _services_metric={"ScanServer": 1})
    assert compat.own_service_name(client) == "BECMCPClient/abc"
    metrics = compat.service_metrics(client)
    assert metrics == {"ScanServer": 1}
    assert metrics is not client._services_metric

    assert compat.own_service_name(SimpleNamespace()) is None
    assert compat.service_metrics(SimpleNamespace()) == {}


def test_device_config_reads_the_root_config():
    device = DeviceBase(
        name="samx",
        config={
            "name": "samx",
            "enabled": False,
            "deviceClass": "ophyd_devices.SimPositioner",
            "readoutPriority": "baseline",
        },
    )
    config = compat.device_config(device)
    assert config["name"] == "samx" and config["enabled"] is False


def test_refresh_queue_calls_update_queue():
    calls = []
    compat.refresh_queue(SimpleNamespace(_update_queue=lambda: calls.append(True)))
    assert calls == [True]
