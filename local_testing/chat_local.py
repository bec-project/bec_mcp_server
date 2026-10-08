"""Interactive chat: local Ollama model <-> BEC MCP server.

Usage: python chat_local.py [model] [num_ctx]
Commands inside the chat: /reset (clear history), /tools (list tools), /quit
"""
import asyncio, json, sys, time
import httpx
from mcp import ClientSession
from mcp.client.streamable_http import streamablehttp_client

MCP_URL = "http://127.0.0.1:8000/mcp"
OLLAMA = "http://127.0.0.1:11434/api/chat"
MODEL = sys.argv[1] if len(sys.argv) > 1 else "qwen3:8b"
NUM_CTX = int(sys.argv[2]) if len(sys.argv) > 2 else 16384
MAX_TOOL_CHARS = 12000  # truncate huge tool results so they fit the context

SYSTEM = ("You are an assistant for the BEC beamline experiment control system. "
          "Use the provided tools to answer. Answer concisely and only from tool results.")

DIM, CYAN, RESET = "\033[2m", "\033[36m", "\033[0m"


class Spinner:
    """Animated status line with elapsed time; cleared when stopped."""

    FRAMES = "⠋⠙⠹⠸⠼⠴⠦⠧⠇⠏"

    def __init__(self, label):
        self.label, self.task = label, None

    async def _spin(self):
        start, i = time.monotonic(), 0
        while True:
            print(f"\r{DIM}{self.FRAMES[i % len(self.FRAMES)]} {self.label} {time.monotonic() - start:4.1f}s{RESET}",
                  end="", flush=True)
            i += 1
            await asyncio.sleep(0.1)

    def __enter__(self):
        self.task = asyncio.get_running_loop().create_task(self._spin())
        return self

    def stop(self):
        if self.task and not self.task.done():
            self.task.cancel()
            print("\r\033[K", end="", flush=True)

    def __exit__(self, *exc):
        self.stop()


async def ollama(http, messages, tools, label):
    """Stream one model turn: spinner until the first token, then print tokens live."""
    body = {"model": MODEL, "messages": messages, "tools": tools, "stream": True, "think": False,
            "keep_alive": "30m", "options": {"num_ctx": NUM_CTX, "temperature": 0}}
    content, calls, final = "", [], {}
    with Spinner(label) as spinner:
        async with http.stream("POST", OLLAMA, json=body, timeout=900) as r:
            r.raise_for_status()
            async for line in r.aiter_lines():
                if not line:
                    continue
                chunk = json.loads(line)
                m = chunk.get("message", {})
                if m.get("content"):
                    spinner.stop()
                    print(m["content"], end="", flush=True)
                    content += m["content"]
                calls += m.get("tool_calls") or []
                if chunk.get("done"):
                    final = chunk
    if content:
        print()
    return {"role": "assistant", "content": content, "tool_calls": calls}, final


async def answer(http, session, tools, msgs):
    label = "model working…"
    for _ in range(8):
        m, r = await ollama(http, msgs, tools, label)
        msgs.append(m)
        calls = m["tool_calls"]
        if not calls:
            secs = r.get("total_duration", 0) / 1e9
            print(f"{DIM}[{r.get('prompt_eval_count', 0)} prompt tok, {secs:.1f}s]{RESET}")
            return
        for c in calls:
            name, args = c["function"]["name"], c["function"].get("arguments") or {}
            print(f"{DIM}→ {name}({json.dumps(args)}){RESET}")
            try:
                with Spinner(f"running {name}…"):
                    res = await session.call_tool(name, args)
                text = "\n".join(getattr(b, "text", "") for b in res.content)
            except Exception as e:
                text = f"ERROR: {e}"
            if len(text) > MAX_TOOL_CHARS:
                print(f"{DIM}  (result {len(text)} chars, truncated to {MAX_TOOL_CHARS}){RESET}")
                text = text[:MAX_TOOL_CHARS] + f"\n...[truncated, {len(text)} chars total]"
            msgs.append({"role": "tool", "tool_name": name, "content": text})
        label = "model reading tool results…"
    print("[stopped after 8 tool rounds]")


async def main():
    async with streamablehttp_client(MCP_URL) as (read, write, _), ClientSession(read, write) as session:
        await session.initialize()
        tools = [{"type": "function", "function": {"name": t.name, "description": t.description or "",
                  "parameters": t.inputSchema}} for t in (await session.list_tools()).tools]
        print(f"Connected: {MODEL} (ctx {NUM_CTX}) + {len(tools)} BEC MCP tools. /tools /reset /quit")
        msgs = [{"role": "system", "content": SYSTEM}]
        async with httpx.AsyncClient() as http:
            while True:
                try:
                    q = (await asyncio.to_thread(input, f"\n{CYAN}you>{RESET} ")).strip()
                except (EOFError, KeyboardInterrupt):
                    break
                if not q:
                    continue
                if q == "/quit":
                    break
                if q == "/reset":
                    msgs = msgs[:1]
                    print("history cleared")
                    continue
                if q == "/tools":
                    for t in tools:
                        print(f"  {t['function']['name']}: {t['function']['description']}")
                    continue
                msgs.append({"role": "user", "content": q})
                await answer(http, session, tools, msgs)


asyncio.run(main())
