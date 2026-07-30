from __future__ import annotations

import os
import redis
import threading
from dataclasses import dataclass
from enum import Enum
from typing import Any

from bec_lib.client import BECClient
from bec_lib.endpoints import MessageEndpoints
from bec_lib.messages import StatusMessage
from bec_lib.redis_connector import RedisConnector
from bec_lib.service_config import ServiceConfig


def _jsonable(value: Any) -> Any:
    if hasattr(value, "model_dump"):
        return value.model_dump(mode="json")
    if isinstance(value, Enum):
        return value.value
    if isinstance(value, dict):
        return {str(key): _jsonable(val) for key, val in value.items()}
    if isinstance(value, (list, tuple)):
        return [_jsonable(item) for item in value]
    return value


def _first_line(text: str | None) -> str:
    if not text:
        return ""
    return text.strip().splitlines()[0]


def _summarize_device(name: str, device_config: Any) -> dict[str, Any]:
    if not isinstance(device_config, dict):
        return {"name": name, "config": _jsonable(device_config)}

    return {
        "name": name,
        "device_class": device_config.get("deviceClass"),
        "enabled": device_config.get("enabled"),
        "readout_priority": device_config.get("readoutPriority"),
        "device_tags": device_config.get("deviceTags"),
    }


def _summarize_scan(name: str, scan_info: Any) -> dict[str, Any]:
    if not isinstance(scan_info, dict):
        return {"name": name, "scan_info": _jsonable(scan_info)}

    return {
        "name": name,
        "description": _first_line(scan_info.get("doc")),
        "base_class": scan_info.get("base_class"),
    }


def _scan_details(name: str, scan_info: Any) -> dict[str, Any]:
    summary = _summarize_scan(name, scan_info)
    if "scan_info" in summary:
        return summary

    assert isinstance(scan_info, dict)
    return {
        **summary,
        "required_kwargs": scan_info.get("required_kwargs") or [],
        "arg_input": scan_info.get("arg_input") or {},
        "signature": scan_info.get("signature") or [],
    }


@dataclass(frozen=True)
class BecConnectionConfig:
    host: str = "localhost"
    port: int = 6379
    username: str | None = None
    password: str | None = None

    @classmethod
    def from_env(cls) -> "BecConnectionConfig":
        return cls(
            host=os.environ.get("BEC_REDIS_HOST", "localhost"),
            port=int(os.environ.get("BEC_REDIS_PORT", "6379")),
            username=os.environ.get("BEC_REDIS_USERNAME") or None,
            password=os.environ.get("BEC_REDIS_PASSWORD") or None,
        )


class BecConnection:
    def __init__(self, config: BecConnectionConfig | None = None) -> None:
        self.config = config or BecConnectionConfig.from_env()
        self._client: BECClient | None = None
        self._lock = threading.Lock()

    @property
    def client(self) -> BECClient:
        if self._client is not None:
            return self._client

        with self._lock:
            if self._client is not None:
                return self._client

            acl_config = None
            if self.config.username is not None or self.config.password is not None:
                acl_config = {
                    "user": self.config.username,
                    "password": self.config.password,
                }

            service_config = ServiceConfig(
                config_name="client",
                redis={"host": self.config.host, "port": self.config.port},
                acl=acl_config,
            )
            client = BECClient(
                config=service_config,
                connector_cls=RedisConnector,
                wait_for_server=False,
                forced=True,
                name="BECMCPClient",
                prompt_for_acl=False,
            )
            try:
                client.start()
            except redis.exceptions.ConnectionError:
                client.shutdown()
                raise
            self._client = client
            return self._client

    def connect(self) -> None:
        self.client

    def ping(self) -> dict[str, Any]:
        connector = self.client.connector
        return {
            "ok": bool(connector.ping()),
            "host": self.config.host,
            "port": self.config.port,
            "username": connector.username,
        }

    def _get_service_messages(self) -> list[StatusMessage]:
        connector = self.client.connector
        service_keys = connector.keys(MessageEndpoints.service_status("*"))
        if not service_keys:
            return []

        prefix = MessageEndpoints.service_status("").endpoint
        service_ids = [key.decode().removeprefix(prefix) for key in service_keys]
        return [
            message
            for service_id in service_ids
            if isinstance(
                (message := connector.get(MessageEndpoints.service_status(service_id))),
                StatusMessage,
            )
        ]

    def services(self) -> list[dict[str, Any]]:
        services = [
            {
                "name": message.name,
                "status": _jsonable(message.status),
                "info": _jsonable(message.info),
                "metadata": _jsonable(message.metadata),
            }
            for message in self._get_service_messages()
        ]
        return sorted(services, key=lambda item: item["name"] or "")

    def devices(self) -> dict[str, Any]:
        device_manager = self.client.device_manager
        items = sorted(device_manager.devices.items())

        return {
            "count": len(items),
            "devices": [
                _summarize_device(name, _jsonable(device.root._config)) for name, device in items
            ],
        }

    def device_config(self, device_name: str) -> dict[str, Any]:
        device_manager = self.client.device_manager
        device = device_manager.devices.get(device_name)

        if device is None:
            return {
                "found": False,
                "device_name": device_name,
                "available_devices": sorted(device_manager.devices.keys()),
            }

        return {
            "found": True,
            "device_name": device_name,
            "config": _jsonable(device.root._config),
        }

    def scans(
        self,
        *,
        prefix: str | None = None,
        base_class: str | None = None,
        include_internal: bool = False,
    ) -> dict[str, Any]:
        client = self.client
        scans = client.scans._available_scans if client.scans is not None else {}
        scan_items = sorted(scans.items())

        filtered_scans = []
        for name, scan in scan_items:
            scan_info = scan.scan_info
            scan_base_class = scan_info.get("base_class") if isinstance(scan_info, dict) else None
            if prefix is not None and not name.startswith(prefix):
                continue
            if base_class is not None and scan_base_class != base_class:
                continue
            if not include_internal and name.startswith("_"):
                continue
            filtered_scans.append((name, scan_info))

        return {
            "count": len(filtered_scans),
            "scans": [_summarize_scan(name, scan_info) for name, scan_info in filtered_scans],
        }

    def scan_details(self, scan_name: str) -> dict[str, Any]:
        client = self.client
        scans = client.scans._available_scans if client.scans is not None else {}
        scan = scans.get(scan_name)

        if scan is None:
            return {
                "found": False,
                "scan_name": scan_name,
                "available_scans": sorted(scans.keys()),
            }

        return {
            "found": True,
            "scan_name": scan_name,
            "scan": _scan_details(scan_name, scan.scan_info),
        }

    def scan_interlock(self) -> dict[str, Any]:
        client = self.client
        interlock = client.builtin_actors.scan_interlock if client.builtin_actors is not None else None
        if interlock is None:
            return {
                "enabled": None,
                "trigger_setting": None,
                "states_watched": {},
            }

        return {
            "enabled": interlock.enabled,
            "trigger_setting": _jsonable(interlock.trigger_setting),
            "states_watched": _jsonable(interlock.states_watched),
        }

    def queue(self) -> dict[str, Any]:
        client = self.client
        if client.queue is None:
            return {"queues": {}}

        client.queue.queue_storage._update_queue()
        queues = client.queue.queue_storage.current_scan_queue or {}

        summary: dict[str, Any] = {}
        for queue_name, queue_info in queues.items():
            info = queue_info.info or []
            summary[queue_name] = {
                "status": _jsonable(queue_info.status),
                "locks": _jsonable(queue_info.locks),
                "items": [
                    {
                        "queue_id": item.queue_id,
                        "status": item.status,
                        "scan_id": _jsonable(item.scan_id),
                        "scan_number": _jsonable(item.scan_number),
                        "is_scan": _jsonable(item.is_scan),
                        "request_count": len(item.request_blocks or []),
                        "scan_types": [
                            request_block.msg.scan_type for request_block in item.request_blocks or []
                        ],
                    }
                    for item in info
                ],
            }

        return {"queues": summary}

    def summary(self) -> dict[str, Any]:
        services = self.services()
        queue = self.queue()
        device_count = len(self.client.device_manager.devices)
        scan_count = (
            len(self.client.scans._available_scans)
            if self.client.scans is not None
            else 0
        )

        return {
            "connection": self.ping(),
            "service_count": len(services),
            "device_count": device_count,
            "scan_count": scan_count,
            "queues": {
                queue_name: {
                    "status": queue_info["status"],
                    "size": len(queue_info["items"]),
                }
                for queue_name, queue_info in queue["queues"].items()
            },
        }
