#!/usr/bin/env python3
"""
Decepticon Sandbox Logging Proxy
==================================
Sits between langgraph and the sandbox HTTP daemon (port 9999).
Logs every request body (command) and response body (stdout/stderr/exit_code)
to NDJSON so sub-agent tool calls are fully captured.

Deploy via docker-compose.override.yml:
  - Listens on port 9998 within sandbox-net
  - Forwards everything to sandbox:9999
  - Writes to /logs/sandbox-proxy.ndjson (bind-mounted to host)

Set SAAS_SANDBOX_URL=http://sandbox-proxy:9998 in langgraph environment.
"""

import json
import os
import sys
import threading
import time
import urllib.request
import urllib.error
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, HTTPServer

UPSTREAM = os.environ.get("UPSTREAM_URL", "http://sandbox:9999")
LOG_PATH = os.environ.get("LOG_PATH", "/logs/sandbox-proxy.ndjson")
LISTEN_PORT = int(os.environ.get("LISTEN_PORT", "9998"))

_log_lock = threading.Lock()
_log_file = open(LOG_PATH, "a", encoding="utf-8")


def ts():
    return datetime.now(timezone.utc).isoformat()


def write_log(record: dict):
    line = json.dumps(record, ensure_ascii=False, default=str)
    with _log_lock:
        _log_file.write(line + "\n")
        _log_file.flush()


class ProxyHandler(BaseHTTPRequestHandler):
    def log_message(self, fmt, *args):
        pass  # suppress default access log — we write our own

    def do_POST(self):
        # Read request body
        length = int(self.headers.get("Content-Length", 0))
        req_body_raw = self.rfile.read(length) if length else b""

        try:
            req_body = json.loads(req_body_raw)
        except Exception:
            req_body = req_body_raw.decode("utf-8", errors="replace")

        # Extract key fields for summary
        endpoint = self.path
        command = None
        if isinstance(req_body, dict):
            command = (req_body.get("command") or
                       req_body.get("cmd") or
                       req_body.get("script") or
                       req_body.get("session_name"))

        req_headers = dict(self.headers)
        start = time.monotonic()
        t_start = ts()

        # Forward to upstream
        try:
            upstream_url = UPSTREAM.rstrip("/") + self.path
            fwd_req = urllib.request.Request(
                upstream_url,
                data=req_body_raw,
                headers={
                    k: v for k, v in req_headers.items()
                    if k.lower() not in ("host", "content-length")
                },
                method="POST"
            )
            with urllib.request.urlopen(fwd_req, timeout=120) as upstream_resp:
                status = upstream_resp.status
                resp_body_raw = upstream_resp.read()
                resp_headers = dict(upstream_resp.headers)
        except urllib.error.HTTPError as e:
            status = e.code
            resp_body_raw = e.read()
            resp_headers = {}
        except Exception as e:
            status = 502
            resp_body_raw = json.dumps({"error": str(e)}).encode()
            resp_headers = {}

        duration_ms = round((time.monotonic() - start) * 1000, 1)

        try:
            resp_body = json.loads(resp_body_raw)
        except Exception:
            resp_body = resp_body_raw.decode("utf-8", errors="replace")

        # Extract output summary
        stdout = stderr = exit_code = None
        if isinstance(resp_body, dict):
            stdout    = resp_body.get("stdout", resp_body.get("output"))
            stderr    = resp_body.get("stderr")
            exit_code = resp_body.get("exit_code", resp_body.get("returncode"))

        # Write log record
        record = {
            "ts":          t_start,
            "duration_ms": duration_ms,
            "endpoint":    endpoint,
            "status":      status,
            "command":     command,
            "request":     req_body,
            "stdout":      stdout,
            "stderr":      stderr,
            "exit_code":   exit_code,
            "response":    resp_body if stdout is None else None,  # avoid duplication
        }
        # Strip None values to keep logs clean
        record = {k: v for k, v in record.items() if v is not None}
        write_log(record)

        # Print summary to stdout for docker logs
        cmd_preview = str(command or "")[:120] if command else endpoint
        print(f"[{t_start[11:19]}] {endpoint} {status} {duration_ms}ms | {cmd_preview}", flush=True)
        if exit_code is not None:
            print(f"  exit={exit_code} stdout={len(str(stdout or ''))}b stderr={len(str(stderr or ''))}b", flush=True)

        # Send response back to client
        self.send_response(status)
        for k, v in resp_headers.items():
            if k.lower() in ("content-type", "content-length"):
                self.send_header(k, v)
        self.end_headers()
        self.wfile.write(resp_body_raw)

    def do_GET(self):
        # Health check passthrough
        try:
            upstream_url = UPSTREAM.rstrip("/") + self.path
            with urllib.request.urlopen(upstream_url, timeout=10) as r:
                status = r.status
                body   = r.read()
        except Exception as e:
            status = 502
            body   = str(e).encode()
        self.send_response(status)
        self.end_headers()
        self.wfile.write(body)


def main():
    print(f"Sandbox proxy starting on :{LISTEN_PORT}", flush=True)
    print(f"Forwarding to: {UPSTREAM}", flush=True)
    print(f"Logging to:    {LOG_PATH}", flush=True)
    server = HTTPServer(("0.0.0.0", LISTEN_PORT), ProxyHandler)
    server.serve_forever()


if __name__ == "__main__":
    main()
