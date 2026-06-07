# How to Build, Configure & Run Decepticon

## Architecture overview

```
.env                        ← your credentials + model choice
config/litellm.yaml         ← LLM gateway routing (GLM, OpenAI, Gemini…)
docker-compose.yml          ← canonical service definitions (don't edit)
docker-compose.override.yml ← our fixes (sandbox transport, logging)

Scripts (scripts/):
  decepticon-run.ps1       ← one-shot launcher (prereqs→config→build→engage→log)
  decepticon-logger.py     ← SSE-stream logger (agents, LLM calls, commands)
  decepticon-llm-report.py ← postgres query for LiteLLM call history
```

---

## 1. One-time setup

### 1.1 Prerequisites

- **Docker Desktop** (WSL2 backend) — running
- **Python 3.8+** — for the logger
- **Git** — already done if you have this repo

### 1.2 Configure .env

The only file you need to edit. Minimum for GLM:

```ini
CUSTOM_OPENAI_API_KEY=<your-glm-key>
CUSTOM_OPENAI_API_BASE=https://api.z.ai/api/coding/paas/v4
CUSTOM_OPENAI_MODEL=glm-5.1
DECEPTICON_MODEL_PROFILE=eco
DECEPTICON_MODEL=custom/glm-5.1
DECEPTICON_AUTH_PRIORITY=custom_openai_api
DECEPTICON_AUTH_CLAUDE_CODE=false
LITELLM_MASTER_KEY=sk-decepticon-master
LITELLM_SALT_KEY=sk-decepticon-salt-change-me
POSTGRES_PASSWORD=decepticon
POSTGRES_PORT=5435
NEO4J_PASSWORD=decepticon-graph
COMPOSE_PROFILES=c2-sliver
DECEPTICON_HOME=C:/decepticon
```

**GLM model options:**
- `glm-4.6`  — faster, lower cost, good tool-calling
- `glm-5.1`  — stronger reasoning, better exploitation chains

### 1.3 Workspace

Docker bind-mounts `C:\decepticon` (not OneDrive) as `/workspace` inside containers.
The run script creates all subdirectories automatically.

---

## 2. Run an engagement (one command)

```powershell
cd C:\Users\alaed\OneDrive\Decepticon
.\scripts\decepticon-run.ps1 -Target 10.55.0.10
```

**Options:**

| Flag | Default | Meaning |
|------|---------|---------|
| `-Target` | *required* | Target IP address |
| `-Model` | `glm-5.1` | `glm-4.6` or `glm-5.1` |
| `-WorkspaceDir` | `C:\decepticon\workspace` | Host path for artefacts |
| `-NoPull` | off | Skip `docker compose pull` (use cached images) |
| `-NoLog` | off | Skip starting the background logger |

The script:
1. Checks Docker, Python, .env
2. Updates `.env` with chosen model
3. Creates workspace dirs
4. `docker compose pull` (unless `-NoPull`)
5. `docker compose up -d`, waits for healthy
6. Verifies sandbox → target reachability
7. Creates LangGraph thread + launches run
8. Starts `decepticon-logger.py` in background
9. Prints thread ID / run ID / log paths

---

## 3. Manual step-by-step (equivalent to the script)

```powershell
cd C:\Users\alaed\OneDrive\Decepticon

# 3a. Pull images (once or when updating)
docker compose pull

# 3b. Start stack
docker compose up -d

# 3c. Check health
docker compose ps

# 3d. Create thread
$tid = (Invoke-WebRequest http://localhost:2024/threads -Method POST `
    -ContentType application/json -Body '{}' -UseBasicParsing |
    ConvertFrom-Json).thread_id

# 3e. Launch engagement
$body = @{
    assistant_id = "decepticon"
    input = @{
        messages = @(@{ role="human"; content="AUTHORIZED. Target: 10.55.0.10. Execute full kill chain." })
        workspace_path = "/workspace"
        target_url = "10.55.0.10"
    }
    config = @{ configurable = @{ workspace_path = "/workspace" } }
} | ConvertTo-Json -Depth 10

$rid = (Invoke-WebRequest "http://localhost:2024/threads/$tid/runs" -Method POST `
    -ContentType application/json -Body $body -UseBasicParsing |
    ConvertFrom-Json).run_id

# 3f. Start logger
python scripts\decepticon-logger.py --tid $tid --rid $rid --logdir C:\decepticon\logs\run1
```

---

## 4. Monitoring during a run

```powershell
# Live human-readable log (same as stdout from logger)
Get-Content C:\decepticon\logs\<timestamp>\agent-log.txt -Wait

# All container output
docker logs -f decepticon-langgraph

# Sandbox commands (what Kali actually ran)
docker logs -f decepticon-sandbox

# Dashboard (web UI)
# Open http://localhost:3000 in browser

# Neo4j (attack graph)
# Open http://localhost:7474  user: neo4j  pass: decepticon-graph
```

---

## 5. Log files

After a run, `C:\decepticon\logs\<timestamp>\` contains:

| File | Content |
|------|---------|
| `events.ndjson` | Every raw LangGraph SSE event |
| `llm-calls.ndjson` | LLM request+response pairs (deduplicated) |
| `commands.ndjson` | Shell commands executed in sandbox (input + output) |
| `agents.ndjson` | Agent reasoning, decisions, tool dispatch |
| `agent-log.txt` | Human-readable chronological timeline |
| `sandbox-raw.log` | Docker container logs for sandbox (`--sandbox` flag) |
| `run-info.json` | Thread ID, run ID, target, model, timestamps |

### Query LiteLLM postgres for LLM calls

```powershell
# Requires: pip install psycopg2-binary
python scripts\decepticon-llm-report.py              # table for last 24h
python scripts\decepticon-llm-report.py --hours 2    # last 2h only
python scripts\decepticon-llm-report.py --json > out.json
python scripts\decepticon-llm-report.py --full       # include full prompt/response bodies
```

---

## 6. Workspace artefacts

All evidence lives in `C:\decepticon\workspace\` (= `/workspace` in containers):

```
recon/
  nmap_full_ports.txt        ← 1-65535 port scan
  nmap_services.txt          ← -sV -sC service enum
  SUMMARY.md                 ← recon findings summary
  web/                       ← web recon output
exploit/
  msf_exploit.log            ← Metasploit session transcript
  proof_of_compromise.txt    ← root whoami/id/ifconfig
  shells.json                ← session metadata
  creds/                     ← captured credentials
post-exploit/
  clean_enum.txt             ← full host enumeration
  creds/hashes.txt           ← /etc/shadow
  creds/mysql_creds.txt      ← database credentials
  c2/persist_key             ← SSH persistence key
findings/
  FIND-001.md … FIND-00N.md ← individual findings
  evidence/                  ← proof files
report/
  executive-summary.md
  technical-report.md
plan/
  opplan.json                ← engagement objectives
```

---

## 7. Stop / cleanup

```powershell
cd C:\Users\alaed\OneDrive\Decepticon

# Stop all Decepticon services
docker compose down

# Stop and remove volumes (wipes postgres, neo4j data)
docker compose down -v

# Remove workspace artefacts from a run
Remove-Item C:\decepticon\workspace -Recurse -Force
```

---

## 8. Known issues & workarounds

| Issue | Cause | Fix |
|-------|-------|-----|
| pydantic crash: `model_profile 'custom' invalid` | DECEPTICON_MODEL_PROFILE must be `eco\|max\|test` | Already set to `eco` in .env |
| litellm ImportError on boot | shipped yaml references missing handler .py files | Already stripped from config/litellm.yaml |
| langgraph can't reach sandbox:9999 | stock compose leaves langgraph off sandbox-net | Fixed in docker-compose.override.yml |
| "No engagement workspace is set" | headless API launch didn't pass workspace_path | run script passes it in both input and config |
| postexploit GraphRecursionError | agent spirals re-exploiting per command | Known upstream issue; work around with focused prompts |
| GLM finish_reason=length | max_tokens too small for reasoning model | Not an issue at normal agent loop budget |
| load_skill error for /skills/standard/exploit/web | orchestrator restricted to /skills/shared/ | Known; use built-in knowledge instead |
