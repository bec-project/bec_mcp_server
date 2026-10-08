"""Load bec_lib's demo device config (216 simulated devices) into the running BEC session."""
import os

import bec_lib
from bec_lib.client import BECClient
from bec_lib.redis_connector import RedisConnector
from bec_lib.service_config import ServiceConfig

cfg = os.path.join(os.path.dirname(bec_lib.__file__), "configs", "demo_config.yaml")
client = BECClient(config=ServiceConfig(redis={"host": "localhost", "port": 6379}),
                   connector_cls=RedisConnector, forced=True)
client.start()
client.config.update_session_with_file(cfg, force=True)
print("devices:", len(client.device_manager.devices))
print("scans:", len(client.scans._available_scans))
client.shutdown()
