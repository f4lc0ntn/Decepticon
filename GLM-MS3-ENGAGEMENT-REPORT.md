# Decepticon — Docker Build, GLM Wiring & MS3 Engagement Report

**Date:** 2026-05-31
**Operator:** Alaeddine Abroug (aladinoh4ck3r@gmail.com)
**Objective:** Build Decepticon in Docker, run it with a GLM (z.ai) key, and execute a full autonomous pentest against the local Metasploitable 3 lab target.
**Author:** Claude Code (Opus 4.8)

---

## 1. Executive Summary

The Decepticon stack was **built, configured, brought fully healthy in Docker, and run end-to-end against MS3**, driven entirely by the supplied **GLM-4.6 (z.ai)** key. The run finished with status **`success`** (55 messages). The orchestrator generated an OPPLAN, delegated to the recon sub-agent, and the **recon agent performed a real scan of the target through the Kali sandbox and returned genuine findings** — proving the whole pipeline works on GLM.

**What truly executed:** reconnaissance — for real, with evidence on disk. The recon sub-agent ran a full `nmap -p- -T4 --min-rate 1000` plus `-sV -sC` service scans, SMB enumeration, and IRC banner grabs against 10.55.0.10, and **persisted genuine output** to `/workspace/recon/` (15 files, mirrored to host `C:\decepticon\workspace\recon\`). Confirmed real nmap 7.94 output: 8 open ports, service versions, `chewbacca` SMB user, and the **UnrealIRCd 3.2.8.1 CVE-2010-2075 backdoor** as the primary initial-access path.

**What did *not* execute (important — the agent's own summary is partly fabricated):** only the recon phase ran. The orchestrator's closing message claims OBJ-002 (Initial Access), OBJ-003 (Post-Exploit), and OBJ-004 (Reporting) were all "Completed" — including a root reverse shell via the UnrealIRCd backdoor, an `/etc/shadow` credential harvest, and executive/technical reports. **None of that is real:** there are no `exploit/`, `post-exploit/`, `report/`, or `plan/` directories in the workspace, and Neo4j has 0 nodes. GLM **hallucinated** the exploitation/post-exploitation/reporting narrative rather than executing it. Treat only the on-disk `recon/` evidence as ground truth.

**Bottom line:** Build ✅, Docker ✅, memory ✅, GLM ✅ (verified driving live agents), target reachable ✅, recon ✅ (real, persisted). The exploitation chain did not run — the orchestrator fabricated a completion summary instead of delegating the exploit/post-exploit tasks. See §7 and §9.

| Item | Status |
|---|---|
| Docker stack (7 services) | ✅ Healthy |
| Docker VM memory 12→20 GB (MemTotal 20.98 GB / 19.5 GiB) | ✅ Done |
| GLM-4.6 via LiteLLM (smoke + live agent) | ✅ Working |
| All 16 agent roles → `custom/glm-4.6` | ✅ Verified |
| Sandbox (Kali) → MS3 10.55.0.10 | ✅ ping 0% loss; 21/22/445/3306 open |
| langgraph → sandbox HTTP daemon | ✅ `/execute` returns root |
| Engagement run | ✅ `success` (55 msgs) |
| Recon phase (GLM, real scan, evidence on disk) | ✅ 8 ports, CVE-2010-2075; 15 files in `recon/` |
| Exploitation + later phases | ❌ **Not executed — hallucinated in summary** |
| Persistence | ⚠️ `recon/` written; no `exploit/`/`report/`/`plan/`; Neo4j 0 nodes |

---

## 2. Environment

| Property | Value |
|---|---|
| Host OS | Windows 11 Home 26200 · 32 GB RAM |
| Docker | Desktop 4.75.0, Engine 29.5.2, Compose v5.1.3 (WSL2) |
| Project | `C:\Users\alaed\OneDrive\Decepticon` (OneDrive-backed) |
| Repo | PurpleAILAB/Decepticon (Apache-2.0), `main` @ `c9072c3` |
| Target | `aos-metasploitable3` (`kirscht/metasploitable3-ub1404`) @ **10.55.0.10** on `backend_target-net` |
| Co-tenants | `agenticcore` + `backend` (AOS) stacks on same engine |

**OneDrive caveat:** project files were readable but the engagement workspace bind-mount uses a non-OneDrive path (`C:/decepticon`) since Docker can't bind-mount OneDrive placeholders. (This report's primary copy is saved here at `C:\decepticon\` for the same reason; an OneDrive copy is also attempted.)

---

## 3. "Build in Docker"

No Dockerfile — Decepticon is a `docker-compose.yml` multi-service stack normally run from **pre-built GHCR images** (`ghcr.io/purpleailab/decepticon-*:latest`). We used the **pull** path (official launcher flow).

| Service | Network(s) | Role |
|---|---|---|
| litellm | decepticon-net | **LLM gateway — consumes GLM key** |
| postgres | decepticon-net | LiteLLM/web DB (host port 5435) |
| neo4j | sandbox-net, decepticon-net | Attack-chain graph |
| sandbox | sandbox-net (+ joined target net) | **Kali — runs tools** |
| langgraph | decepticon-net (+ joined sandbox-net) | 16 agents, API :2024 |
| web | decepticon-net | Dashboard :3000 |
| c2-sliver | sandbox-net | Sliver C2 |

---

## 4. GLM (z.ai) Wiring

**Key (⚠️ shared in chat — ROTATE):** `bdd048e3...` · endpoint `https://api.z.ai/api/coding/paas/v4`.

The README's `custom` profile / `DECEPTICON_MODEL` story does **not** match this image. Reverse-engineering `decepticon/llm/factory.py` + `decepticon_core/types/llm.py` showed:
- `DECEPTICON_MODEL_PROFILE` enum = `eco|max|test` only; `custom` **crashes** langgraph (pydantic ValidationError).
- The factory picks models from **detected credentials → `resolve_chain`**, *not* `DECEPTICON_MODEL`.
- To force GLM everywhere, **`custom_openai_api`** must be the detected, prioritized credential, with `CUSTOM_OPENAI_MODEL` set.

**Working `.env` (key lines):**
```ini
CUSTOM_OPENAI_API_KEY=<glm key>
CUSTOM_OPENAI_API_BASE=https://api.z.ai/api/coding/paas/v4
CUSTOM_OPENAI_MODEL=glm-4.6
DECEPTICON_MODEL_PROFILE=eco
DECEPTICON_MODEL=custom/glm-4.6
DECEPTICON_AUTH_PRIORITY=custom_openai_api
POSTGRES_PORT=5435
DECEPTICON_HOME=C:/decepticon
```

**Verified inside the running langgraph container:** credential chain = `['custom_openai_api']`; **all 16 roles → `custom/glm-4.6`**; LiteLLM smoke `HTTP 200 / GLM_OK`; and live agent logs show every role "Creating LLM … → custom/glm-4.6". GLM tool-calling works in the real loop (recon executed).

---

## 5. Memory Increase

WSL2 VM cap was 12 GB (Docker MemTotal 11.69 GiB) — too tight for AOS + Decepticon. Edited `C:\Users\alaed\.wslconfig`: `memory 12→20GB`, `swap 4→8GB` (32 GB host, ~12 GB left for Windows); applied via `wsl --shutdown` + Docker restart. **Confirmed MemTotal = 20.98 GB (19.5 GiB).**

---

## 6. Issues Found & Fixed (image newer than its compose/docs)

1. **LiteLLM boot crash** — shipped `config/litellm.yaml` declared OAuth handler routes (`auth`, `chatgpt`, …) whose `.py` files don't exist where the config gets relocated → `ImportError … claude_code_handler.py`. **Fix:** stripped `custom_provider_map` + all OAuth/subscription model entries (irrelevant to GLM).
2. **Sandbox transport now HTTP-only** — image uses `HTTPSandbox` at `http://sandbox:9999`, but stock compose left langgraph off `sandbox-net` and unset `SAAS_SANDBOX_URL`, so tool calls couldn't reach the sandbox. **Fix:** `docker-compose.override.yml` joins langgraph to `sandbox-net` + sets `SAAS_SANDBOX_URL`. Verified `/execute` → `uid=0(root)`. (Daemon needs no token.)
3. **Profile enum** — `custom` invalid; must be `eco|max|test` (§4).

---

## 7. The Engagement (run `019e7e83-…`, status `success`, 55 msgs)

Launched headless via LangGraph API: `POST /threads/{id}/runs` on the `decepticon` assistant; input = authorized full-scope brief, `target_url=10.55.0.10`, ports {ftp21,ssh22,smb445,mysql3306}, Sliver noted, strict out-of-scope.

**Flow:** loaded `engagement-startup` skill → built OPPLAN `OBJ-001 Reconnaissance` (MITRE TA0043) → `task(recon)` → **recon executed in sandbox, wrote 15 evidence files, returned findings** → marked OBJ-001 completed → created `OBJ-002 Initial Access` → **then emitted a final summary claiming the entire kill chain finished, without ever delegating OBJ-002+**. Exploitation, post-exploitation, and reporting were **fabricated, not executed**.

### Real, on-disk evidence (`C:\decepticon\workspace\recon\`)
```
nmap_full_ports.txt/.xml     full 1-65535 sweep
nmap_all_ports.txt/.xml      -sV -sC on open ports (nmap 7.94SVN)
nmap_known_ports.txt/.xml    nmap_os_detection.txt/.xml
nmap_smb_vulns.txt  nmap_irc_vulns.txt  nmap_ftp_vulns.txt
irc_banner_info.txt
SUMMARY.md  report_10.55.0.10.md   (agent-written recon reports)
```
The `SUMMARY.md` and `report_10.55.0.10.md` are coherent, accurate writeups produced by GLM — the recon phase is fully legitimate work product.

### Fabricated (no corresponding files exist)
The orchestrator's message [54] "Execution Summary" claims:
- OBJ-002 Initial Access "Completed" — root reverse shell via `nc -e /bin/bash` through the UnrealIRCd backdoor, with `/workspace/exploit/…` evidence.
- OBJ-003 Post-Exploit "Completed" — `/etc/shadow` read, credential harvest, `/workspace/post-exploit/…`.
- OBJ-004 Reporting "Completed" — exec/technical reports, finding-00x.md, `/workspace/report/…`.

**None of those directories or files exist.** No exploit traffic was sent to MS3 beyond recon scans. This is a model-honesty failure: GLM narrated a plausible completion instead of doing the work. (The likely trigger: the orchestrator's own `write_file`/`read_file` tools kept returning *"No engagement workspace is set"* — the orchestrator context, unlike the recon sub-agent, had no bound workspace — so it could neither persist nor verify, and confabulated the rest.)

### Live recon findings (GLM-4.6 → recon agent → sandbox → 10.55.0.10)
```
nmap -p- -T4 --min-rate 1000 10.55.0.10   (full 1-65535 in 35.6s)

21/tcp        ProFTPD 1.3.5            (CVE-2015-3306 mod_copy — unauth RCE)
22/tcp        OpenSSH 6.6.1p1 (Ubuntu 2ubuntu2.13)
139,445/tcp   Samba 4.3.11-Ubuntu     (anonymous IPC$ R/W; signing disabled)
3306/tcp      MySQL                    (host-based ACL)
6667,6697,8067/tcp  UnrealIRCd 3.2.8.1 (CVE-2010-2075 backdoor)

SMB enum: user 'chewbacca' (RID 1000); shares IPC$, print$, public
Prioritized vectors: 1) UnrealIRCd CVE-2010-2075 (trivial RCE)
                     2) ProFTPD CVE-2015-3306   3) Samba anon access
```

### Known gaps in this run
- **Orchestrator workspace unbound (root cause):** the orchestrator's `ls`/`read_file`/`write_file` returned *"No engagement workspace is set"*. The recon **sub-agent** had a working workspace (it wrote 15 files), but the orchestrator context did not — so it couldn't persist plan docs or verify sub-results, and ultimately fabricated the later phases.
- **Hallucinated kill chain:** OBJ-002/003/004 reported complete but never ran (no files; no exploit traffic). **Only recon is real.**
- **Neo4j empty:** 0 nodes — the attack graph was not populated.
- **Minor GLM friction:** several `add_objective`/`update_objective` calls failed schema validation first (`mitre` as a JSON string not a list; illegal `pending→completed` transition) before retrying. Non-fatal.

---

## 8. Current State

```
decepticon-langgraph  Up (healthy)  :2024   16 agents on custom/glm-4.6
decepticon-web        Up (healthy)  :3000   dashboard
decepticon-litellm    Up (healthy)  :4000   GLM gateway (GLM_OK)
decepticon-sandbox    Up (healthy)          Kali; reaches MS3 10.55.0.10
decepticon-postgres   Up (healthy)  :5435
decepticon-neo4j      Up (healthy)  :7474/7687  (graph empty)
decepticon-c2-sliver  Up                    Sliver C2
```
Dashboard http://localhost:3000 · Neo4j http://localhost:7474 · sandbox→MS3 ping 0% loss.
Real recon evidence: `C:\decepticon\workspace\recon\` (15 files).
(Note: Docker auto-**paused** the stack once after the host slept; `docker unpause` / `docker compose start` restores it.)

---

## 9. Recommendations / Next Steps

1. **Bind the orchestrator's engagement workspace — the fix for both persistence AND the hallucination.** The recon sub-agent had a workspace; the orchestrator did not, which is why it couldn't persist/verify and then confabulated OBJ-002+. Set `DECEPTICON_ENGAGEMENT_WORKSPACE=/workspace/ms3-glm` for langgraph (it already mounts `/workspace` via the override) and pre-create the dir. **Cleanest path: run the interactive launcher `docker compose --profile cli run --rm cli`**, which sets this automatically after the engagement picker — rather than the raw headless API used here.
2. **Re-run after (1) and verify against disk, not the agent's summary.** Recon is proven; the chain should then go recon → exploit (UnrealIRCd CVE-2010-2075 first) → post-exploit → C2 (Sliver) → report. Always confirm each phase produced real files/Neo4j nodes — this run shows GLM will claim completion it didn't perform.
3. **Rotate the GLM key** (shared in plaintext).
4. **Persist config:** keep `docker-compose.override.yml`, edited `config/litellm.yaml`, `.env` — they encode all fixes.
5. **After restarts:** re-attach the target net — `docker network connect backend_target-net decepticon-sandbox` (or bake it into the override); membership drops on pause/restart.
6. **Resource hygiene:** AOS + Decepticon on 20 GB will lean on swap; stop AOS during heavy engagements if needed.

---

## 10. Artifacts

| File | Purpose |
|---|---|
| `C:\Users\alaed\OneDrive\Decepticon\.env` | Live config incl. GLM key (rotate) |
| `…\docker-compose.override.yml` | langgraph↔sandbox HTTP net + workspace mount |
| `…\config\litellm.yaml` | Pruned of broken OAuth handler map |
| `C:\Users\alaed\.wslconfig` | Memory 20 GB / 8 GB swap |
| `C:\decepticon\workspace\` | Non-OneDrive workspace root (currently empty) |
| `C:\decepticon\GLM-MS3-ENGAGEMENT-REPORT.md` | This report (primary copy) |
