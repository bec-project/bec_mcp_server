"""Start ollmcp against the BEC MCP server, configured only from BEC_MCP_URL and BEC_MCP_TOKEN.

ollmcp does not expand environment variables in its servers file, so this writes one with the
token into the container's home directory (readable only by this user) and then runs ollmcp.
Extra arguments are passed through, e.g. ``--model qwen3:8b``.
"""

import json
import os
import sys
from pathlib import Path

url = os.environ.get("BEC_MCP_URL", "").strip()
token = os.environ.get("BEC_MCP_TOKEN", "").strip()
if not url or not token:
    sys.exit("set BEC_MCP_URL and BEC_MCP_TOKEN")

servers = {
    "mcpServers": {
        "bec": {
            "type": "streamable_http",
            "url": url,
            "headers": {"Authorization": f"Bearer {token}"},
        }
    }
}
path = Path.home() / ".config" / "ollmcp" / "servers.json"
path.parent.mkdir(parents=True, exist_ok=True)
path.touch(mode=0o600)
path.write_text(json.dumps(servers, indent=2), encoding="utf-8")

ollama = os.environ.get("OLLAMA_HOST", "http://host.containers.internal:11434")
os.execvp("ollmcp", ["ollmcp", "--servers-json", str(path), "--host", ollama, *sys.argv[1:]])
