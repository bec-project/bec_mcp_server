#!/usr/bin/env bash
# Start Redis, BEC server, Ollama and the BEC MCP server (skips anything already running).
# Expects the layout created by setup_env.sh:  $BEC_ROOT/{bec,ophyd_devices,bec_venv,bec_mcp_server}
set -e
HERE="$(cd "$(dirname "$0")" && pwd)"
REPO="$(dirname "$HERE")"
BEC_ROOT="${BEC_ROOT:-$(dirname "$REPO")}"
source "${BEC_VENV:-$BEC_ROOT/bec_venv}/bin/activate"
mkdir -p "$HERE/logs"

redis-cli ping >/dev/null 2>&1 || { echo "starting redis"; redis-server --daemonize yes --save "" --appendonly no --logfile "$HERE/logs/redis.log"; sleep 1; }

tmux has-session -t bec 2>/dev/null || { echo "starting bec-server"; bec-server start; sleep 15; }

curl -s http://127.0.0.1:11434/api/version >/dev/null || {
  echo "starting ollama"
  OLLAMA_FLASH_ATTENTION=1 OLLAMA_KV_CACHE_TYPE=q8_0 nohup ollama serve >"$HERE/logs/ollama.log" 2>&1 &
  sleep 3
}

curl -s -o /dev/null http://127.0.0.1:8000/mcp || {
  echo "starting bec mcp server"
  (cd "$REPO" && PYTHONPATH=. nohup python -m bec_mcp_server --port 8000 >"$HERE/logs/mcp.log" 2>&1 &)
  sleep 5
}

echo "stack up — run: python $HERE/chat_local.py"
