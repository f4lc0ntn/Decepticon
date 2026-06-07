#!/usr/bin/env python3
"""
Decepticon LLM Call Report
===========================
Queries the LiteLLM postgres DB for every LLM call recorded during an engagement.
Requires: pip install psycopg2-binary  (or psycopg2)

Usage:
    python decepticon-llm-report.py
    python decepticon-llm-report.py --hours 2
    python decepticon-llm-report.py --json > llm-calls.json
    python decepticon-llm-report.py --full   # include full request/response bodies

The LiteLLM proxy stores one row per API call in `litellm_spendlogs`.
With store_model_in_db=true in litellm.yaml, every GLM (or other) call is logged.
"""

import argparse
import json
import sys

try:
    import psycopg2
    import psycopg2.extras
except ImportError:
    print("ERROR: psycopg2 not installed. Run: pip install psycopg2-binary", file=sys.stderr)
    sys.exit(1)

# ── Connection defaults (match docker-compose.yml / .env) ─────────────────────
DB_HOST = "localhost"
DB_PORT = 5435          # host port from POSTGRES_PORT in .env
DB_NAME = "litellm"
DB_USER = "llmproxy"
DB_PASS = "decepticon"


def connect(host, port, name, user, password):
    return psycopg2.connect(
        host=host, port=port, dbname=name, user=user, password=password,
        connect_timeout=10
    )


def get_spend_logs(cur, hours: int = 24, full: bool = False) -> list:
    cols = """
        id, request_id, call_type, model, api_base,
        startTime, endTime,
        prompt_tokens, completion_tokens, total_tokens,
        response_cost, status,
        messages, response
    """ if full else """
        id, request_id, call_type, model, api_base,
        startTime, endTime,
        prompt_tokens, completion_tokens, total_tokens,
        response_cost, status
    """
    cur.execute(f"""
        SELECT {cols}
        FROM litellm_spendlogs
        WHERE startTime >= NOW() - INTERVAL '{hours} hours'
        ORDER BY startTime ASC
    """)
    return cur.fetchall()


def format_row(row, full: bool = False) -> dict:
    keys = [
        "id", "request_id", "call_type", "model", "api_base",
        "start_time", "end_time",
        "prompt_tokens", "completion_tokens", "total_tokens",
        "cost_usd", "status"
    ]
    if full:
        keys += ["messages", "response"]

    d = dict(zip(keys, row))

    # Compute duration
    if d.get("start_time") and d.get("end_time"):
        dur = (d["end_time"] - d["start_time"]).total_seconds()
        d["duration_s"] = round(dur, 3)

    # Format timestamps
    for k in ("start_time", "end_time"):
        if d.get(k):
            d[k] = d[k].isoformat()

    return d


def print_table(rows: list):
    if not rows:
        print("No LLM calls found in the given time window.")
        return

    total_in  = sum(r.get("prompt_tokens", 0) or 0 for r in rows)
    total_out = sum(r.get("completion_tokens", 0) or 0 for r in rows)
    total_cost = sum(r.get("cost_usd", 0.0) or 0.0 for r in rows)

    header = f"{'#':<4} {'Model':<30} {'Start':<24} {'Dur(s)':<8} {'In':<8} {'Out':<8} {'Status':<12}"
    print(header)
    print("-" * len(header))

    for i, r in enumerate(rows, 1):
        print(
            f"{i:<4} {str(r.get('model','?'))[:30]:<30} "
            f"{str(r.get('start_time','?'))[:23]:<24} "
            f"{str(r.get('duration_s','?')):<8} "
            f"{str(r.get('prompt_tokens','?')):<8} "
            f"{str(r.get('completion_tokens','?')):<8} "
            f"{str(r.get('status','?')):<12}"
        )

    print("-" * len(header))
    print(f"TOTAL: {len(rows)} calls | {total_in:,} in-tokens | {total_out:,} out-tokens | ${total_cost:.4f}")


def main():
    p = argparse.ArgumentParser(description="Query LiteLLM postgres for LLM call history")
    p.add_argument("--hours",    type=int, default=24, help="Look back N hours (default 24)")
    p.add_argument("--json",     action="store_true",  help="Output JSON instead of table")
    p.add_argument("--full",     action="store_true",  help="Include messages and response bodies")
    p.add_argument("--host",     default=DB_HOST)
    p.add_argument("--port",     type=int, default=DB_PORT)
    p.add_argument("--dbname",   default=DB_NAME)
    p.add_argument("--user",     default=DB_USER)
    p.add_argument("--password", default=DB_PASS)
    args = p.parse_args()

    try:
        conn = connect(args.host, args.port, args.dbname, args.user, args.password)
    except Exception as e:
        print(f"ERROR: Cannot connect to postgres at {args.host}:{args.port}/{args.dbname}: {e}", file=sys.stderr)
        print("  Is the Decepticon stack running? (`docker compose ps`)", file=sys.stderr)
        sys.exit(1)

    with conn:
        cur = conn.cursor()
        try:
            raw_rows = get_spend_logs(cur, args.hours, args.full)
        except psycopg2.errors.UndefinedTable:
            print("ERROR: litellm_spendlogs table not found.", file=sys.stderr)
            print("  Set store_model_in_db=true in config/litellm.yaml and restart litellm.", file=sys.stderr)
            sys.exit(1)

    rows = [format_row(r, args.full) for r in raw_rows]

    if args.json:
        json.dump(rows, sys.stdout, indent=2, default=str)
        print()
    else:
        print(f"\n=== LiteLLM Call Log (last {args.hours}h, {len(rows)} calls) ===\n")
        print_table(rows)
        if args.full and rows:
            print("\n=== Full Request/Response Bodies ===\n")
            for i, r in enumerate(rows, 1):
                print(f"── Call #{i}: {r.get('model')} @ {r.get('start_time')} ──")
                print("  MESSAGES:")
                msgs = r.get("messages")
                if msgs:
                    try:
                        msgs = json.loads(msgs) if isinstance(msgs, str) else msgs
                        for m in msgs[-3:]:  # last 3 messages
                            role = m.get("role", "?")
                            content = str(m.get("content", ""))[:400]
                            print(f"    [{role}] {content}")
                    except Exception:
                        print(f"    {str(msgs)[:400]}")
                print("  RESPONSE:")
                resp = r.get("response")
                if resp:
                    try:
                        resp = json.loads(resp) if isinstance(resp, str) else resp
                        choices = resp.get("choices", [])
                        if choices:
                            text = choices[0].get("message", {}).get("content", "")
                            print(f"    {str(text)[:400]}")
                        else:
                            print(f"    {str(resp)[:400]}")
                    except Exception:
                        print(f"    {str(resp)[:400]}")
                print()


if __name__ == "__main__":
    main()
