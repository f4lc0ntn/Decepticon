#!/usr/bin/env python3
"""
Decepticon Comprehensive Event Logger
======================================
Captures every observable event from a running Decepticon engagement:

  Layer 1 — LangGraph SSE stream
    • on_chat_model_start/end  → full LLM input (messages) + output (text, tool calls)
    • on_tool_start/end        → every tool call with input args + raw output
    • on_chain_start/end       → agent node enter/exit with state
    → logs/events.ndjson   (all raw events)
    → logs/llm-calls.ndjson (deduplicated LLM request+response pairs)
    → logs/commands.ndjson  (execute/execute_tmux calls with full output)
    → logs/agents.ndjson    (per-agent decisions and reasoning)

  Layer 2 — message-stream fallback (LangGraph messages/partial SSE)
    • Parses AI message chunks: text + tool_use blocks
    • Parses tool result messages
    → same files as above via unified process_message()

  Layer 3 — polling fallback
    • Polls /threads/{tid}/history every 10 s if SSE fails
    → same files

  Layer 4 — Docker sandbox log capture (optional, via subprocess)
    • Tails `docker logs -f decepticon-sandbox`
    → logs/sandbox-raw.log

Usage:
  python decepticon-logger.py --tid <thread_id> --rid <run_id> --logdir ./logs/run1

  # With custom LangGraph URL:
  python decepticon-logger.py --tid ... --rid ... --logdir ... --url http://localhost:2024

  # Also capture sandbox Docker logs:
  python decepticon-logger.py --tid ... --rid ... --logdir ... --sandbox

Output files (all NDJSON = one JSON object per line):
  events.ndjson   — every raw SSE event (comprehensive, large)
  llm-calls.ndjson — deduplicated LLM request+response pairs
  commands.ndjson  — shell command executions (input + stdout/stderr)
  agents.ndjson    — agent reasoning, decisions, tool dispatch
  agent-log.txt    — human-readable chronological log (also printed to stdout)
  sandbox-raw.log  — Docker logs of the sandbox container (--sandbox only)
"""

import argparse
import json
import subprocess
import sys
import threading
import time
import urllib.request
import urllib.error
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path

# ── Config ────────────────────────────────────────────────────────────────────
LGURL = "http://localhost:2024"
POLL_INTERVAL = 10   # seconds between history polls (fallback mode)
SSE_TIMEOUT   = 900  # seconds for SSE connection timeout

# Tool names that represent sandbox command execution
EXEC_TOOLS = {"execute", "execute_tmux", "bash", "run_command", "shell", "exec"}

# ── Timestamp ─────────────────────────────────────────────────────────────────
def ts() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z"

# ── Log file manager ──────────────────────────────────────────────────────────
class LogFiles:
    def __init__(self, logdir: Path):
        logdir.mkdir(parents=True, exist_ok=True)
        self.events   = open(logdir / "events.ndjson",    "w", encoding="utf-8")
        self.llm      = open(logdir / "llm-calls.ndjson", "w", encoding="utf-8")
        self.commands = open(logdir / "commands.ndjson",  "w", encoding="utf-8")
        self.agents   = open(logdir / "agents.ndjson",    "w", encoding="utf-8")
        self.human    = open(logdir / "agent-log.txt",    "w", encoding="utf-8")
        self._lock    = threading.Lock()

    def write(self, f, obj: dict):
        line = json.dumps(obj, ensure_ascii=False, default=str)
        with self._lock:
            f.write(line + "\n")
            f.flush()

    def log(self, text: str):
        print(text, flush=True)
        with self._lock:
            self.human.write(text + "\n")
            self.human.flush()

    def close(self):
        for f in (self.events, self.llm, self.commands, self.agents, self.human):
            try:
                f.close()
            except Exception:
                pass


# ── HTTP helpers ──────────────────────────────────────────────────────────────
def http_get(path: str, timeout: int = 30) -> dict:
    url = LGURL + path
    with urllib.request.urlopen(url, timeout=timeout) as r:
        return json.loads(r.read().decode())


def http_stream(path: str, timeout: int = SSE_TIMEOUT):
    """Open SSE stream, yield raw lines."""
    url = LGURL + path
    req = urllib.request.Request(url, headers={"Accept": "text/event-stream"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        for raw in r:
            yield raw.decode("utf-8", errors="replace").rstrip("\n\r")


# ── Content extraction helpers ─────────────────────────────────────────────────
def extract_text(content) -> str:
    """Flatten message content to a plain string."""
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts = []
        for block in content:
            if isinstance(block, str):
                parts.append(block)
            elif isinstance(block, dict):
                parts.append(block.get("text", block.get("content", "")))
        return " ".join(p for p in parts if p)
    return str(content) if content else ""


def extract_tool_calls(content) -> list:
    """Extract tool_use blocks from content list."""
    if not isinstance(content, list):
        return []
    return [b for b in content if isinstance(b, dict) and b.get("type") in ("tool_use", "function")]


# ── Core logger ───────────────────────────────────────────────────────────────
class DecepticonLogger:
    def __init__(self, tid: str, rid: str, logs: LogFiles):
        self.tid  = tid
        self.rid  = rid
        self.logs = logs

        self.seen_msg_ids: set = set()
        self.llm_pairs: dict   = {}   # run_id → {"request": ..., "response": ...}
        self.msg_seq: int      = 0

    # ── Message processor (messages/partial + polling) ────────────────────────
    def process_message(self, msg: dict):
        """Route a single LangGraph history message to the right log files."""
        msg_id = msg.get("id", "") or str(self.msg_seq)
        if msg_id in self.seen_msg_ids:
            return
        self.seen_msg_ids.add(msg_id)
        self.msg_seq += 1

        now    = ts()
        mtype  = msg.get("type", "")
        name   = msg.get("name", "")
        seq    = self.msg_seq

        # Always write the raw event
        self.logs.write(self.logs.events, {"ts": now, "seq": seq, "source": "message", **msg})

        if mtype in ("human", "HumanMessage"):
            text = extract_text(msg.get("content", ""))
            self.logs.log(f"[{now}] [HUMAN] {text[:200]}")

        elif mtype in ("ai", "AIMessage", "AIMessageChunk"):
            content = msg.get("content", "")
            text    = extract_text(content)
            tool_uses = extract_tool_calls(content)

            # Reasoning / narrative text
            if text:
                rec = {"ts": now, "seq": seq, "type": "reasoning", "agent": name, "text": text}
                self.logs.write(self.logs.agents, rec)
                self.logs.log(f"[{now}] [AGENT/{name or 'orchestrator'}] {text[:400]}")

            # Tool dispatch
            for tc in tool_uses:
                tool_name  = tc.get("name", tc.get("function", {}).get("name", ""))
                tool_input = tc.get("input", tc.get("function", {}).get("arguments", {}))
                if isinstance(tool_input, str):
                    try:
                        tool_input = json.loads(tool_input)
                    except Exception:
                        tool_input = {"raw": tool_input}

                rec = {"ts": now, "seq": seq, "type": "tool_call",
                       "agent": name, "tool": tool_name, "input": tool_input}
                self.logs.write(self.logs.agents, rec)
                self.logs.log(f"[{now}] [TOOL CALL] {tool_name}({json.dumps(tool_input, default=str)[:180]})")

                if tool_name.lower() in EXEC_TOOLS:
                    cmd = (tool_input.get("command") or
                           tool_input.get("cmd") or
                           tool_input.get("script") or
                           json.dumps(tool_input, default=str))
                    self.logs.write(self.logs.commands, {
                        "ts": now, "seq": seq, "phase": "input",
                        "tool": tool_name, "agent": name, "command": str(cmd)
                    })

            # LLM usage from response_metadata
            usage = (msg.get("usage_metadata") or
                     msg.get("response_metadata", {}).get("usage") or {})
            model = (msg.get("response_metadata", {}).get("model_name") or
                     msg.get("response_metadata", {}).get("model") or "unknown")
            if usage or model != "unknown":
                self.logs.write(self.logs.llm, {
                    "ts": now, "seq": seq, "phase": "response_meta",
                    "agent": name, "model": model,
                    "in_tokens":  usage.get("input_tokens",  usage.get("prompt_tokens", 0)),
                    "out_tokens": usage.get("output_tokens", usage.get("completion_tokens", 0)),
                    "text_preview": text[:300]
                })

        elif mtype in ("tool", "ToolMessage"):
            content    = msg.get("content", "")
            tool_name  = name or msg.get("name", "tool")
            text       = extract_text(content) if not isinstance(content, str) else content

            self.logs.log(f"[{now}] [TOOL RESULT/{tool_name}] {text[:400]}")

            if tool_name.lower() in EXEC_TOOLS:
                try:
                    result = json.loads(text) if text.startswith("{") else text
                except Exception:
                    result = text
                self.logs.write(self.logs.commands, {
                    "ts": now, "seq": seq, "phase": "output",
                    "tool": tool_name, "output": result
                })

    # ── LangGraph 'events' stream_mode processor ──────────────────────────────
    def process_event(self, event: dict):
        """Handle a LangGraph stream_mode=events event."""
        ev_type = event.get("event", "")
        name    = event.get("name", "")
        now     = ts()
        run_id  = event.get("run_id", "")

        self.logs.write(self.logs.events, {"ts": now, "source": "event", **event})

        if ev_type == "on_chat_model_start":
            data     = event.get("data", {})
            inputs   = data.get("input", {})
            messages = inputs.get("messages", [])
            node     = event.get("metadata", {}).get("langgraph_node", "?")

            flat_msgs = []
            for turn in messages:
                items = turn if isinstance(turn, list) else [turn]
                for m in items:
                    role    = m.get("type", m.get("role", "?"))
                    content = extract_text(m.get("content", ""))
                    flat_msgs.append({"role": role, "content": content[:500]})

            rec = {
                "ts": now, "phase": "request", "model": name, "node": node,
                "run_id": run_id, "messages": flat_msgs, "msg_count": len(flat_msgs)
            }
            self.logs.write(self.logs.llm, rec)
            self.llm_pairs[run_id] = {"request": rec}

            preview = flat_msgs[-1]["content"][:150] if flat_msgs else ""
            self.logs.log(f"[{now}] [LLM→{node}] model={name} msgs={len(flat_msgs)} last_msg={preview}")

        elif ev_type == "on_chat_model_end":
            data        = event.get("data", {})
            output      = data.get("output", {})
            gens        = output.get("generations", [[]])
            text        = ""
            tool_calls  = []
            if gens and gens[0]:
                g    = gens[0][0] if isinstance(gens[0], list) else gens[0]
                text = g.get("text", "") or extract_text(
                    g.get("message", {}).get("content", ""))
                tool_calls = g.get("message", {}).get("tool_calls", [])
            usage = output.get("llm_output", {}).get("token_usage", {})

            rec = {
                "ts": now, "phase": "response", "model": name,
                "run_id": run_id,
                "text":        text[:2000],
                "tool_calls":  tool_calls,
                "in_tokens":   usage.get("prompt_tokens", 0),
                "out_tokens":  usage.get("completion_tokens", 0),
                "finish_reason": (gens[0][0].get("generation_info", {}).get("finish_reason")
                                  if gens and gens[0] else None)
            }
            self.logs.write(self.logs.llm, rec)
            if run_id in self.llm_pairs:
                self.llm_pairs[run_id]["response"] = rec

            self.logs.log(
                f"[{now}] [LLM RESP] model={name} "
                f"in={usage.get('prompt_tokens','?')} out={usage.get('completion_tokens','?')} "
                f"tools={len(tool_calls)} text={text[:120]}"
            )

        elif ev_type == "on_tool_start":
            data       = event.get("data", {})
            tool_input = data.get("input", {})
            node       = event.get("metadata", {}).get("langgraph_node", "?")

            rec = {"ts": now, "type": "tool_call", "node": node, "tool": name,
                   "run_id": run_id, "input": tool_input}
            self.logs.write(self.logs.agents, rec)
            self.logs.log(f"[{now}] [TOOL START/{name}] {json.dumps(tool_input, default=str)[:250]}")

            if name.lower() in EXEC_TOOLS:
                cmd = (tool_input.get("command") or tool_input.get("cmd") or
                       tool_input.get("script") or json.dumps(tool_input, default=str))
                self.logs.write(self.logs.commands, {
                    "ts": now, "phase": "input", "tool": name, "node": node,
                    "run_id": run_id, "command": str(cmd)
                })

        elif ev_type == "on_tool_end":
            data   = event.get("data", {})
            output = data.get("output", "")
            node   = event.get("metadata", {}).get("langgraph_node", "?")

            rec = {"ts": now, "type": "tool_result", "node": node, "tool": name,
                   "run_id": run_id, "output_preview": str(output)[:500]}
            self.logs.write(self.logs.agents, rec)
            self.logs.log(f"[{now}] [TOOL END/{name}] {str(output)[:300]}")

            if name.lower() in EXEC_TOOLS:
                self.logs.write(self.logs.commands, {
                    "ts": now, "phase": "output", "tool": name, "node": node,
                    "run_id": run_id, "output": str(output)[:4000]
                })

        elif ev_type in ("on_chain_start", "on_chain_end"):
            node = event.get("metadata", {}).get("langgraph_node", name)
            self.logs.write(self.logs.agents, {
                "ts": now, "type": ev_type, "node": node, "run_id": run_id
            })
            self.logs.log(f"[{now}] [NODE] {ev_type} → {node}")

    # ── SSE stream ────────────────────────────────────────────────────────────
    def run_sse(self) -> bool:
        """
        Try streaming with stream_mode=events first (richest data).
        Falls back to messages stream.
        Returns True if stream completed normally (terminal status reached).
        """
        for mode in ("events", "messages"):
            try:
                self.logs.log(f"[{ts()}] Connecting SSE stream (mode={mode})...")
                ok = self._consume_sse(mode)
                if ok:
                    return True
            except urllib.error.HTTPError as e:
                self.logs.log(f"[{ts()}] SSE mode={mode} failed: HTTP {e.code}")
            except Exception as e:
                self.logs.log(f"[{ts()}] SSE mode={mode} error: {e}")
        return False

    def _consume_sse(self, mode: str) -> bool:
        path      = f"/threads/{self.tid}/runs/{self.rid}/stream?stream_mode={mode}"
        event_tag = None
        terminal  = False

        for line in http_stream(path):
            if line.startswith("event: "):
                event_tag = line[7:].strip()

            elif line.startswith("data: "):
                raw = line[6:].strip()
                if raw in ("{}", ""):
                    if event_tag == "end":
                        self.logs.log(f"[{ts()}] Stream ended (end event)")
                        terminal = True
                        break
                    continue

                try:
                    data = json.loads(raw)
                except json.JSONDecodeError:
                    continue

                if mode == "events":
                    if isinstance(data, dict):
                        self.process_event(data)
                elif mode == "messages":
                    msgs = data if isinstance(data, list) else [data]
                    for m in msgs:
                        self.process_message(m)

            elif not line:
                event_tag = None

        return terminal

    # ── Polling fallback ──────────────────────────────────────────────────────
    def run_polling(self):
        """Poll /threads/{tid}/history + /runs/{rid} until terminal status."""
        self.logs.log(f"[{ts()}] Polling mode (interval={POLL_INTERVAL}s)")
        last_status = None

        while True:
            try:
                run    = http_get(f"/threads/{self.tid}/runs/{self.rid}")
                status = run.get("status", "?")

                if status != last_status:
                    self.logs.log(f"[{ts()}] STATUS: {status}")
                    last_status = status

                history = http_get(f"/threads/{self.tid}/history")
                if isinstance(history, list):
                    for checkpoint in history:
                        values   = checkpoint.get("values", {}) if isinstance(checkpoint, dict) else {}
                        messages = values.get("messages", [])
                        for msg in messages:
                            self.process_message(msg)

                if status in ("success", "error", "interrupted",
                              "timeout", "cancelled", "failed"):
                    self.logs.log(f"[{ts()}] TERMINAL: {status}")
                    break

            except urllib.error.HTTPError as e:
                self.logs.log(f"[{ts()}] HTTP {e.code}")
            except Exception as e:
                self.logs.log(f"[{ts()}] Polling error: {e}")

            time.sleep(POLL_INTERVAL)

    # ── Entry point ───────────────────────────────────────────────────────────
    def start(self):
        self.logs.log("=" * 70)
        self.logs.log(" DECEPTICON LOGGER")
        self.logs.log(f"  Thread:  {self.tid}")
        self.logs.log(f"  Run:     {self.rid}")
        self.logs.log(f"  API:     {LGURL}")
        self.logs.log("=" * 70)

        try:
            completed = self.run_sse()
        except Exception as e:
            self.logs.log(f"[{ts()}] SSE failed entirely ({e}), switching to polling")
            completed = False

        if not completed:
            self.run_polling()

        self.logs.log("=" * 70)
        self.logs.log(f" Done. Events: {self.msg_seq}  LLM pairs: {len(self.llm_pairs)}")
        self.logs.log("=" * 70)


# ── Docker sandbox log capture ────────────────────────────────────────────────
def capture_sandbox_logs(logdir: Path, stop_event: threading.Event):
    """Tail docker logs for the sandbox container and write to sandbox-raw.log."""
    out = open(logdir / "sandbox-raw.log", "w", encoding="utf-8")
    try:
        proc = subprocess.Popen(
            ["docker", "logs", "-f", "--timestamps", "decepticon-sandbox"],
            stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
            text=True, encoding="utf-8", errors="replace"
        )
        while not stop_event.is_set():
            line = proc.stdout.readline()
            if not line:
                if proc.poll() is not None:
                    break
                time.sleep(0.1)
                continue
            out.write(line)
            out.flush()
    except Exception as e:
        out.write(f"[sandbox-logger error] {e}\n")
    finally:
        out.close()
        try:
            proc.terminate()
        except Exception:
            pass


# ── Main ─────────────────────────────────────────────────────────────────────
def main():
    global LGURL

    p = argparse.ArgumentParser(
        description="Decepticon comprehensive event logger",
        formatter_class=argparse.RawDescriptionHelpFormatter
    )
    p.add_argument("--tid",     required=True, help="LangGraph thread ID")
    p.add_argument("--rid",     required=True, help="LangGraph run ID")
    p.add_argument("--logdir",  required=True, help="Output directory for log files")
    p.add_argument("--url",     default=LGURL,  help="LangGraph API base URL")
    p.add_argument("--sandbox", action="store_true",
                   help="Also capture Docker sandbox container logs")
    args = p.parse_args()

    LGURL = args.url.rstrip("/")
    logdir = Path(args.logdir)

    logs   = LogFiles(logdir)
    logger = DecepticonLogger(args.tid, args.rid, logs)

    # Optionally capture sandbox Docker logs in a background thread
    stop_evt = threading.Event()
    if args.sandbox:
        t = threading.Thread(target=capture_sandbox_logs,
                             args=(logdir, stop_evt), daemon=True)
        t.start()
        logs.log(f"[{ts()}] Sandbox log capture started → {logdir}/sandbox-raw.log")

    try:
        logger.start()
    except KeyboardInterrupt:
        logs.log(f"\n[{ts()}] Interrupted by user")
    finally:
        stop_evt.set()
        logs.close()
        print(f"\nLogs saved to: {logdir}", flush=True)
        print(f"  events.ndjson    — all raw events")
        print(f"  llm-calls.ndjson — LLM request/response pairs")
        print(f"  commands.ndjson  — shell command executions")
        print(f"  agents.ndjson    — agent decisions and reasoning")
        print(f"  agent-log.txt    — human-readable timeline")


if __name__ == "__main__":
    main()
