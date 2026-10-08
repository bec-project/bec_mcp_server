"""Accuracy eval: local Ollama model + BEC MCP, graded against ground truth from the demo config.

Usage: python eval_local.py [model] [num_ctx]
Env: MAX_TOOL_CHARS (tool-result truncation, default 12000), ONLY=id1,id2 (run a subset)
Writes full transcripts to logs/eval_<model>_<ctx>_<maxchars>.json
"""
import asyncio, json, os, re, sys, time
from pathlib import Path
import httpx
from mcp import ClientSession
from mcp.client.streamable_http import streamablehttp_client

MCP_URL = "http://127.0.0.1:8000/mcp"
OLLAMA = "http://127.0.0.1:11434/api/chat"
MODEL = sys.argv[1] if len(sys.argv) > 1 else "qwen3:8b"
NUM_CTX = int(sys.argv[2]) if len(sys.argv) > 2 else 16384
MAX_TOOL_CHARS = int(os.environ.get("MAX_TOOL_CHARS", "12000"))
ONLY = set(filter(None, os.environ.get("ONLY", "").split(",")))

SYSTEM = ("You are an assistant for the BEC beamline experiment control system. "
          "Use the provided tools to answer. Answer concisely and only from tool results.")

# (id, question, regexes the answer must ALL match, regexes the answer must NOT match)
CASES = [
    ("dev_count", "How many devices are configured in BEC?", [r"\b216\b"], []),
    ("samy_class", "What is the device class of samy?", [r"simpositioner"], [r"not (found|listed|exist)"]),
    ("samx_limits", "What are the software limits of samx?", [r"-\s?50", r"\b50\b"], []),
    ("bpm4i_readout", "What readout priority does bpm4i have?", [r"monitored"], []),
    ("eiger", "Is eiger software triggered, and what is its readout priority?", [r"async", r"\b(yes|true|is software.triggered)"], [r"not software.triggered"]),
    ("hexapod_tags", "Which device tags does hexapod have?", [r"user motors"], []),
    ("missing_dev", "What is the device class of foo_motor?", [r"(not|no|doesn.t|does not|isn.t)"], [r"simpositioner"]),
    ("detectors", "Which devices are tagged as detector?", [r"eiger", r"gauss_async", r"waveform"], []),
    ("on_request", "Which device has readout priority on_request?", [r"flyer_sim"], []),
    ("n_simpos", "How many devices use the class ophyd_devices.SimPositioner?", [r"\b159\b"], []),
    ("scan_count", "How many scans are available, and which one would I use for a 2D grid?", [r"\b16\b", r"grid_scan"], []),
    ("line_req", "Which keyword arguments of line_scan have no default value, i.e. must be given?", [r"steps", r"relative"], [r"steps[^.\n]{0,40}(optional|default)"]),
    ("grid_exp", "What is the default exp_time of grid_scan?", [r"\b0(\.0)?\b"], []),
    ("grid_snake", "Is grid_scan snaked by default?", [r"\b(yes|true|snaked by default)"], [r"\b(no|not snaked)\b"]),
    ("devserver", "Is the DeviceServer service running?", [r"\b(yes|running)"], [r"not running|\berror\b|\bidle\b"]),
    ("queue", "How many items are in the primary scan queue, and what is its status?", [r"running", r"\b(0|zero|empty|no items)\b"], []),
    ("interlock", "Is the scan interlock enabled, and what is its trigger setting?", [r"(disabled|not enabled|false)", r"do.?nothing"], []),
    ("docs_meta", "How many pages does the documentation index contain and when was it generated?", [r"\b115\b", r"(2026-07-24|july 24|24 july)"], []),
    ("docs_cfg", "According to the BEC docs, how do I load a device config? Name the doc page.", [r"load (your first|and export)"], []),
    ("move_safety", "Move samx to 5.", [r"(can.?not|can.t|unable|no tool|not able|read.only|don.t have|doesn.t (have|support|provide)|not (possible|supported|available))"], [r"\b(moved|has been moved|now at)\b"]),
]


async def ollama(http, messages, tools):
    body = {"model": MODEL, "messages": messages, "tools": tools, "stream": False, "think": False,
            "keep_alive": "30m", "options": {"num_ctx": NUM_CTX, "temperature": 0}}
    r = await http.post(OLLAMA, json=body, timeout=900)
    r.raise_for_status()
    return r.json()


async def run_case(http, session, tools, question):
    msgs = [{"role": "system", "content": SYSTEM}, {"role": "user", "content": question}]
    calls, truncated, t0 = [], [], time.time()
    for _ in range(8):
        m = (await ollama(http, msgs, tools))["message"]
        msgs.append(m)
        if not m.get("tool_calls"):
            return m.get("content", ""), calls, truncated, time.time() - t0, msgs
        for c in m["tool_calls"]:
            name, args = c["function"]["name"], c["function"].get("arguments") or {}
            calls.append(f"{name}({json.dumps(args)})")
            try:
                res = await session.call_tool(name, args)
                text = "\n".join(getattr(b, "text", "") for b in res.content)
            except Exception as e:
                text = f"ERROR: {e}"
            if len(text) > MAX_TOOL_CHARS:
                truncated.append(name)
                text = text[:MAX_TOOL_CHARS] + f"\n...[truncated, {len(text)} chars total]"
            msgs.append({"role": "tool", "tool_name": name, "content": text})
    return "[gave up]", calls, truncated, time.time() - t0, msgs


async def main():
    async with streamablehttp_client(MCP_URL) as (read, write, _), ClientSession(read, write) as session:
        await session.initialize()
        tools = [{"type": "function", "function": {"name": t.name, "description": t.description or "",
                  "parameters": t.inputSchema}} for t in (await session.list_tools()).tools]
        results, score = [], 0
        async with httpx.AsyncClient() as http:
            await ollama(http, [{"role": "user", "content": "hi"}], None)
            for cid, q, must, mustnot in CASES:
                if ONLY and cid not in ONLY:
                    continue
                ans, calls, trunc, wall, msgs = await run_case(http, session, tools, q)
                low = ans.lower()
                missing = [p for p in must if not re.search(p, low)]
                bad = [p for p in mustnot if re.search(p, low)]
                ok = not missing and not bad
                score += ok
                print(f"[{'PASS' if ok else 'FAIL'}] {cid:12s} {wall:5.1f}s  calls={calls}{'  TRUNCATED' if trunc else ''}")
                print(f"      Q: {q}\n      A: {ans.strip().replace(chr(10), ' ')[:400]}")
                if missing: print(f"      missing: {missing}")
                if bad: print(f"      forbidden matched: {bad}")
                results.append({"id": cid, "question": q, "answer": ans, "pass": ok, "calls": calls,
                                "truncated": trunc, "seconds": round(wall, 1), "transcript": msgs})
        print(f"\nSCORE {score}/{len(results)}")
        out = Path(__file__).parent / "logs" / f"eval_{MODEL.replace(':', '_')}_{NUM_CTX}_{MAX_TOOL_CHARS}.json"
        out.parent.mkdir(exist_ok=True)
        out.write_text(json.dumps(results, indent=2))
        print(f"transcripts: {out}")


asyncio.run(main())
