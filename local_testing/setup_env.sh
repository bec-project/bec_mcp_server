#!/usr/bin/env bash
# One-time setup on macOS: Homebrew deps, BEC + ophyd_devices checkouts, a Python venv, and an Ollama model.
# Layout:  $BEC_ROOT/{bec,ophyd_devices,bec_venv,bec_mcp_server}   (BEC_ROOT defaults to the repo's parent dir)
# Usage:   ./setup_env.sh [ollama-model]      e.g. ./setup_env.sh qwen3:8b
set -e
HERE="$(cd "$(dirname "$0")" && pwd)"
REPO="$(dirname "$HERE")"
BEC_ROOT="${BEC_ROOT:-$(dirname "$REPO")}"
MODEL="${1:-qwen3:8b}"
cd "$BEC_ROOT"

brew install redis tmux ollama uv

[ -d bec ] || git clone https://github.com/bec-project/bec.git
[ -d ophyd_devices ] || git clone https://github.com/bec-project/ophyd_devices.git
[ -d bec_venv ] || uv venv --python 3.12 bec_venv
source bec_venv/bin/activate

# bec_mcp_server itself is not pip-installed: its pyproject references a missing README.md,
# so it is run from source with PYTHONPATH (see start_stack.sh). mcp is pinned <2 because
# server.py imports mcp.server.fastmcp, which mcp 2.x renamed.
uv pip install \
  -e ./bec/bec_lib -e ./bec/bec_ipython_client -e ./bec/bec_server -e ./ophyd_devices \
  'mcp>=1.0,<2' 'qdrant-client>=1.10' 'uvicorn>=0.30' httpx pytest

# Ollama must be running to pull a model.
curl -s http://127.0.0.1:11434/api/version >/dev/null || {
  OLLAMA_FLASH_ATTENTION=1 OLLAMA_KV_CACHE_TYPE=q8_0 nohup ollama serve >/dev/null 2>&1 &
  sleep 3
}
ollama pull "$MODEL"

(cd "$REPO" && PYTHONPATH=. python -m pytest -q)
echo "setup done — next: $HERE/start_stack.sh  then  python $HERE/load_demo_config.py"
