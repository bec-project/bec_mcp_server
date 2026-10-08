# Client configuration

The agent images in [`../agent/`](../agent/Containerfile) are built from these files. They also
work for a client on the host itself, connecting to `http://127.0.0.1:8000/mcp`. Every file refers
to the token through `BEC_MCP_TOKEN` and to the server through `BEC_MCP_URL`; none contains a
token, and none of yours should either.

```bash
export BEC_MCP_URL=http://127.0.0.1:8000/mcp
export BEC_MCP_TOKEN="$(cat ~/.config/bec-mcp/token)"
```

| Client | File | Where it goes |
| --- | --- | --- |
| Claude Code | [`claude-mcp.json`](claude-mcp.json) | `.mcp.json` in the project directory |
| qwen-code | [`qwen-settings.json`](qwen-settings.json) | `~/.qwen/settings.json`, or merged into it |
| ollmcp | none | written at start by [`../agent/ollmcp_entrypoint.py`](../agent/ollmcp_entrypoint.py) |

## Claude Code

Claude Code expands `${BEC_MCP_URL}` and `${BEC_MCP_TOKEN}` in `.mcp.json` when it connects, and
asks once per project before it uses a server from that file. To use the server in every project
without a file, register it for your user instead; the shell then writes the token into
`~/.claude.json`:

```bash
claude mcp add --transport http --scope user bec "$BEC_MCP_URL" \
    --header "Authorization: Bearer $BEC_MCP_TOKEN"
```

For a server Claude Code starts itself over stdio, register the venv's binary with its absolute
path, not `conda run`, which buffers stdout and breaks the handshake:

```bash
claude mcp add bec -- /opt/bec-mcp/venv/bin/bec-mcp-server --transport stdio
```

## qwen-code

qwen-code expands `$BEC_MCP_URL` and `$BEC_MCP_TOKEN` in `settings.json` when it loads it.
`httpUrl` selects streamable HTTP (`url` would mean SSE), and `trust` skips the confirmation before
each call, which is safe because every tool is read-only. For a local model through Ollama:

```bash
OPENAI_BASE_URL=http://127.0.0.1:11434/v1 OPENAI_API_KEY=ollama OPENAI_MODEL=qwen3:8b qwen
```

## Ollama with ollmcp

[ollmcp](https://github.com/jonigl/mcp-client-for-ollama) does not expand environment variables in
its servers file, so write one with the token and keep it private:

```bash
( umask 077; cat > ~/.config/ollmcp-bec.json <<EOF
{"mcpServers": {"bec": {"type": "streamable_http", "url": "$BEC_MCP_URL",
  "headers": {"Authorization": "Bearer $BEC_MCP_TOKEN"}}}}
EOF
)
ollmcp --servers-json ~/.config/ollmcp-bec.json --model qwen3:8b
```

mcphost, which the prototype also supported, is archived upstream; use ollmcp instead.
