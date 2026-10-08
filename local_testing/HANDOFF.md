# Handoff: testing the BEC MCP server with local models (Ollama)

Context for a Claude Code session picking this up on a new machine. Goal: run the BEC MCP server
against a local BEC instance (demo config), connect a local LLM through Ollama, and measure whether
its answers are actually correct.

## Files in `local_testing/`

| File | Purpose |
|---|---|
| `setup_env.sh [model]` | One-time setup: `brew install redis tmux ollama uv`, clones `bec` + `ophyd_devices` next to this repo, creates `bec_venv` (Python 3.12), installs everything editable, pulls the Ollama model, runs the repo tests. Idempotent. |
| `start_stack.sh` | Starts Redis, `bec-server` (tmux session `bec`), `ollama serve` and the MCP server on `http://127.0.0.1:8000/mcp`. Skips what is already running. Logs go to `local_testing/logs/` (gitignored). |
| `load_demo_config.py` | Loads bec_lib's `demo_config.yaml` into the session (216 sim devices, 16 scans). Needed once after a fresh Redis. |
| `chat_local.py [model] [num_ctx]` | Interactive chat: Ollama model ↔ MCP tools, with spinner and streamed output. `/tools`, `/reset`, `/quit`. |
| `eval_local.py [model] [num_ctx]` | 20 questions graded against demo-config ground truth (regex must/must-not). Env: `MAX_TOOL_CHARS` (tool-result truncation, default 12000), `ONLY=id1,id2`. Writes transcripts to `logs/`. |

Expected directory layout (`BEC_ROOT` defaults to the parent of this repo; `BEC_VENV` can override the venv):

```
BEC_ROOT/
  bec/  ophyd_devices/  bec_venv/  bec_mcp_server/   (this repo)
```

## Steps on a fresh Mac

1. Check hardware first — it decides the model:
   `sysctl -n machdep.cpu.brand_string hw.memsize; df -h /`
2. `cd bec_mcp_server && git checkout feat/local-testing`
3. `local_testing/setup_env.sh <model>` (see model table below)
4. `local_testing/start_stack.sh`
5. `source ../bec_venv/bin/activate && python local_testing/load_demo_config.py`
6. `python local_testing/eval_local.py <model> <num_ctx>` → compare with the baseline below.
7. Interactive: `python local_testing/chat_local.py <model> <num_ctx>`
8. Teardown: `bec-server stop; pkill -f bec_mcp_server; redis-cli shutdown; pkill -f "ollama serve"`

Ollama notes:
- Server env used everywhere: `OLLAMA_FLASH_ATTENTION=1 OLLAMA_KV_CACHE_TYPE=q8_0` (halves KV-cache memory).
- Native API `http://127.0.0.1:11434/api/chat` (used by the scripts, supports `tools`, `think`, `options.num_ctx`).
  OpenAI-compatible endpoint: `http://127.0.0.1:11434/v1` for other MCP clients.
- `ollama ps` shows loaded model, memory and whether it is 100% GPU. `ollama list` / `ollama rm <model>`.
- Qwen3 has a thinking mode; the scripts send `think: false`. Thinking was ~5× slower with no accuracy gain on
  simple tool calls.
- Before pulling a model, verify the exact tag on https://ollama.com/library (tags change).

## Model choice by RAM (Apple Silicon, 4-bit)

macOS lets the GPU use ~2/3–3/4 of unified memory by default (`sudo sysctl iogpu.wired_limit_mb=<MB>` raises it until reboot).

| RAM | Candidates |
|---|---|
| 16 GB | `qwen3:8b` (tested, see below). 14B only with short context. MoE 26–30B does not fit. |
| 32 GB | `qwen3:14b`, Gemma 4 26B A4B (MoE, ~4B active → fast), `qwen3:30b-a3b` |
| 64 GB+ | 30B-class MoE with 32k+ context, or dense ~32B |

Worth trying on a bigger machine: does a 26–30B MoE fix the failure classes below (especially the tool-selection ones)?

## Baseline: qwen3:8b on Mac mini M4 / 16 GB (2026-10-09)

Ollama 0.40.1, Q4_K_M, `think: false`, temperature 0, BEC 4.1.6 demo config.

- Speed: ~19 tok/s generation, ~170–280 tok/s cold prompt processing. Small tool results answer in 2–6 s;
  anything with a ~4k-token tool result takes 15–25 s; the full `bec_devices` result (~13k tokens) takes ~80 s.
- Memory: 6.3 GB at 16k ctx, 8.0 GB at 32k ctx (100% GPU).

`eval_local.py qwen3:8b 16384` (12k-char truncation): **11/20 correct after manual review**
(auto-grader also said 11, but `line_req` was a false PASS and `move_safety` a false FAIL — re-check transcripts by hand).

Failure classes:

1. **Doesn't realise `bec_device_config` has the answer (4 cases).** For samx limits, bpm4i readout priority,
   eiger software trigger, which device is `on_request`, it called no tool and said "no tool can do this".
   Tool description "Return the full BEC device config payload for one device" doesn't say *what* is in it.
   → Fix: list the fields in the description (deviceClass, enabled, readoutPriority, deviceTags,
   softwareTrigger, deviceConfig incl. limits).
2. **`bec_devices` output is too large (43 kB, ~13k tokens).** Truncated at 12k chars, the model confidently
   invented answers: "hexapod has no tags", "no device is tagged detector", "184 SimPositioners" (truth 159).
   With no truncation + 32k ctx, 3/4 of those became correct, but at ~80 s each, and counting (159) still failed.
   → Fix: server-side filters (`device_class`, `readout_priority`, `tag`, `name` prefix) and/or pagination,
   plus server-side counts per class/tag/readout priority. Small models can't count over long lists.
3. **`bec_scan_details` misleads.** It returns `required_kwargs: []` for line_scan while `steps` and `relative`
   are required (only visible as `default: "_empty"` in `signature`). The model also answered from the docs
   instead and invented that `exp_time`/`burst_at_each_point` have no default.
   → Fix: compute `required` from the signature and return a clean `{name, type, default, required}` list.
4. **Filter misuse:** `bec_scans(prefix="2D_grid")` → empty → "no scans". → Clarify that `prefix` is a scan-name
   prefix, or drop it for small models.
5. Service status is returned as raw enum ints (`status: 2`); the model guessed "2 = running" correctly
   but it's a guess. → Return `"RUNNING"` (BECStatus name).

Also: `bec_docs_search` (qdrant BM25) ranks poorly for some queries, e.g. "how do I write a custom scan?" → top hits
are EPICS signal/motor pages.

## Known repo issues (not fixed on this branch)

- `pyproject.toml`: `mcp>=1.0` has no upper bound; mcp 2.x renamed `mcp.server.fastmcp.FastMCP` → `server.py`
  fails to import. Workaround: `mcp<2` (setup_env.sh pins it). Real fix: pin, or migrate to `MCPServer`.
- `pyproject.toml` declares `readme = "README.md"` but the file doesn't exist → `pip install -e .` fails.
  Workaround: run from source with `PYTHONPATH=.`.

## Suggested next steps

1. Run `eval_local.py` on the new machine with `qwen3:8b` (to compare hardware) and a larger model.
2. Fix tool descriptions / outputs for failure classes 1–5, rerun the eval, and check whether the
   8B model closes the gap. Expect output shape to matter more than model size.
3. Extend `eval_local.py` with multi-step questions and any write tools before they exist (safety: a test run
   earlier showed qwen3:8b mixing absolute/relative moves after reading a position).
