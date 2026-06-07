#Requires -Version 5.1
<#
.SYNOPSIS
    Decepticon autonomous red-team launcher.

.DESCRIPTION
    Full lifecycle in one command:
      1. Prerequisites  — Docker, Python, .env validity
      2. Configure      — swap model in .env, prep workspace dirs
      3. Images         — optional pull of latest GHCR images
      4. Stack bring-up — `docker compose up -d`, wait for healthy
      5. Verify         — sandbox → target ping; LangGraph API up
      6. Engage         — create thread, POST run brief
      7. Log            — start decepticon-logger.py in background

    All artifacts land in -WorkspaceDir; logs in <WorkspaceDir>\..\logs\<timestamp>.

.PARAMETER Target
    IP of the target (e.g. 10.55.0.10).

.PARAMETER Model
    GLM model: glm-4.6 or glm-5.1 (default glm-5.1).

.PARAMETER WorkspaceDir
    Host path for engagement artefacts. Default: C:\decepticon\workspace

.PARAMETER Scope
    Free-text scope description added to the engagement brief.

.PARAMETER NoPull
    Skip `docker compose pull` (use cached images).

.PARAMETER NoLog
    Skip starting the background logger daemon.

.PARAMETER LangGraphUrl
    Override the LangGraph API base URL. Default: http://localhost:2024

.EXAMPLE
    .\scripts\decepticon-run.ps1 -Target 10.55.0.10
    .\scripts\decepticon-run.ps1 -Target 10.55.0.10 -Model glm-4.6 -NoPull
    .\scripts\decepticon-run.ps1 -Target 10.55.0.10 -NoLog
#>

param(
    [Parameter(Mandatory)]
    [string]$Target,

    [ValidateSet("glm-4.6","glm-5.1","glm-z-plus")]
    [string]$Model = "glm-5.1",

    [string]$WorkspaceDir = "C:\decepticon\workspace",

    [string]$Scope = "Full kill chain: recon -> exploit -> post-exploit -> report",

    [switch]$NoPull,
    [switch]$NoLog,

    [string]$LangGraphUrl = "http://localhost:2024"
)

Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"

$PROJECT   = Split-Path $PSScriptRoot -Parent
$TIMESTAMP = (Get-Date -Format "yyyyMMdd-HHmmss")
$LOGDIR    = (Split-Path $WorkspaceDir -Parent) + "\logs\$TIMESTAMP"

# ─── Helpers ──────────────────────────────────────────────────────────────────
function Step  { param($n,$msg) Write-Host "`n$(Get-Date -F 'HH:mm:ss') [$n/7] $msg" -ForegroundColor Cyan }
function OK    { param($msg)    Write-Host "$(Get-Date -F 'HH:mm:ss')   [OK] $msg" -ForegroundColor Green }
function WARN  { param($msg)    Write-Host "$(Get-Date -F 'HH:mm:ss') [WARN] $msg" -ForegroundColor Yellow }
function ERR   { param($msg)    Write-Host "$(Get-Date -F 'HH:mm:ss')  [ERR] $msg" -ForegroundColor Red; exit 1 }
function Banner { param($msg)   Write-Host $msg -ForegroundColor Magenta }

Banner @"

  ██████  ███████  ██████ ███████ ██████  ████████ ██  ██████  ███    ██
  ██   ██ ██      ██      ██      ██   ██    ██    ██ ██    ██ ████   ██
  ██   ██ █████   ██      █████   ██████     ██    ██ ██    ██ ██ ██  ██
  ██   ██ ██      ██      ██      ██         ██    ██ ██    ██ ██  ██ ██
  ██████  ███████  ██████ ███████ ██         ██    ██  ██████  ██   ████

  PurpleAILAB — Autonomous Red-Team Framework
  Model: $Model  |  Target: $Target
"@

# ─── STEP 1: Prerequisites ────────────────────────────────────────────────────
Step 1 "Prerequisites"

try { $null = docker info 2>&1 }
catch { ERR "Docker not running — start Docker Desktop first." }
OK "Docker running"

$envPath = "$PROJECT\.env"
if (-not (Test-Path $envPath)) { ERR ".env missing at $envPath — see README §4 for GLM wiring." }

$envLines = Get-Content $envPath
$glmKey  = ($envLines | Where-Object { $_ -match "^CUSTOM_OPENAI_API_KEY=" }) -replace "^CUSTOM_OPENAI_API_KEY=",""
$glmBase = ($envLines | Where-Object { $_ -match "^CUSTOM_OPENAI_API_BASE=" }) -replace "^CUSTOM_OPENAI_API_BASE=",""
if (-not $glmKey -or $glmKey -match "your[- ]?key|placeholder") {
    ERR "CUSTOM_OPENAI_API_KEY not configured in .env. Add your GLM API key."
}
OK ".env: key=$($glmKey.Substring(0,[Math]::Min(8,$glmKey.Length)))*** base=$glmBase"

if (-not $NoLog) {
    try { $null = python --version 2>&1 }
    catch { WARN "Python not found — logger will be skipped. Install Python 3.8+."; $NoLog = $true }
}

$loggerScript = "$PROJECT\scripts\decepticon-logger.py"
if (-not $NoLog -and -not (Test-Path $loggerScript)) {
    WARN "Logger script missing at $loggerScript — logger disabled."
    $NoLog = $true
}

# ─── STEP 2: Configure model ──────────────────────────────────────────────────
Step 2 "Configure model ($Model)"

$newLines = $envLines | ForEach-Object {
    if     ($_ -match "^CUSTOM_OPENAI_MODEL=")  { "CUSTOM_OPENAI_MODEL=$Model" }
    elseif ($_ -match "^DECEPTICON_MODEL=")      { "DECEPTICON_MODEL=custom/$Model" }
    else   { $_ }
}
$newLines | Set-Content $envPath -Encoding UTF8
OK ".env: CUSTOM_OPENAI_MODEL=$Model  DECEPTICON_MODEL=custom/$Model"

# ─── STEP 3: Workspace & log dirs ─────────────────────────────────────────────
Step 3 "Workspace"

@($WorkspaceDir, "$WorkspaceDir\recon", "$WorkspaceDir\exploit",
  "$WorkspaceDir\post-exploit", "$WorkspaceDir\findings",
  "$WorkspaceDir\findings\evidence", "$WorkspaceDir\report",
  "$WorkspaceDir\plan", $LOGDIR) | ForEach-Object {
    New-Item -ItemType Directory -Force -Path $_ | Out-Null
}
OK "Workspace: $WorkspaceDir"
OK "Logs:      $LOGDIR"

# ─── STEP 4: Images ───────────────────────────────────────────────────────────
Step 4 "Images"

Set-Location $PROJECT

if ($NoPull) {
    WARN "Skipping image pull (-NoPull). Using local cache."
} else {
    Write-Host "  Pulling latest GHCR images..." -ForegroundColor Gray
    docker compose pull
    OK "Images refreshed"
}

# ─── STEP 5: Stack bring-up ───────────────────────────────────────────────────
Step 5 "Stack bring-up"

docker compose up -d

# Poll for healthy
Write-Host "  Waiting for services (up to 3 min)..." -ForegroundColor Gray
$deadline = (Get-Date).AddSeconds(180)
$lastMsg  = ""
while ((Get-Date) -lt $deadline) {
    $ps = docker compose ps --format json 2>&1 | Where-Object { $_.Trim().StartsWith("{") }
    if ($ps) {
        $svcs    = $ps | ForEach-Object { $_ | ConvertFrom-Json }
        $total   = ($svcs | Measure-Object).Count
        $up      = ($svcs | Where-Object { $_.Status -match "Up|running|healthy" } | Measure-Object).Count
        $msg     = "  $up/$total up"
        if ($msg -ne $lastMsg) { Write-Host $msg; $lastMsg = $msg }
        if ($up -ge $total -and $total -gt 0) { break }
    }
    Start-Sleep 5
}

# Verify LangGraph API
Write-Host "  Waiting for LangGraph API at $LangGraphUrl..." -ForegroundColor Gray
for ($i = 0; $i -lt 24; $i++) {
    try {
        $r = Invoke-WebRequest "$LangGraphUrl/ok" -UseBasicParsing -TimeoutSec 5 2>&1
        if ($r.StatusCode -eq 200) { break }
    } catch { }
    Start-Sleep 5
}
OK "LangGraph API up: $LangGraphUrl"

# ─── STEP 6: Verify sandbox → target ──────────────────────────────────────────
Step 6 "Connectivity check"

$ping = docker exec decepticon-sandbox bash -c "ping -c2 -W2 $Target 2>&1" 2>&1
if ($ping -match "2 received|bytes from") {
    OK "Sandbox → $Target (ping OK)"
} elseif ($ping -match "1 received") {
    WARN "Partial reachability to $Target — check network."
} else {
    WARN "Cannot ping $Target from sandbox. Check target is on sandbox-net."
}

$portCheck = docker exec decepticon-sandbox bash -c "nc -z -w2 $Target 22 && echo SSH_OK || echo SSH_FAIL" 2>&1
OK "Port check: $portCheck"

# ─── STEP 7: Launch engagement ────────────────────────────────────────────────
Step 7 "Launch engagement"

# Create thread
$threadJson = Invoke-WebRequest "$LangGraphUrl/threads" -Method POST `
    -ContentType "application/json" -Body '{}' -UseBasicParsing
$TID = ($threadJson.Content | ConvertFrom-Json).thread_id
OK "Thread: $TID"

$brief = @"
AUTHORIZED PENETRATION TEST — ISOLATED LAB

Operator:   Alaeddine Abroug
Authority:  I own and operate this isolated lab environment.
Target:     $Target  (Metasploitable 3 — intentionally vulnerable training system)
Scope:      $Scope
Out-of-scope: Docker host, decepticon-* infrastructure (172.x), anything not 10.55.0.0/24.
Model:      $Model (GLM / z.ai)

OBJECTIVE: Full autonomous kill chain.

INSTRUCTION: EXECUTE immediately.
  Step 1 — call task(recon)      : full port scan, service enum, vuln ID
  Step 2 — call task(exploit)    : exploit highest-confidence finding, get shell
  Step 3 — call task(postexploit): enumerate host, dump creds, establish persistence
  Step 4 — call task(report)     : write executive + technical report to /workspace/report/

Do NOT build an OPPLAN and declare success. Delegate every step via task().
"@

$body = @{
    assistant_id = "decepticon"
    input = @{
        messages       = @(@{ role = "human"; content = $brief })
        workspace_path = "/workspace"
        target_url     = $Target
    }
    config = @{
        configurable = @{ workspace_path = "/workspace" }
    }
} | ConvertTo-Json -Depth 10

$runJson = Invoke-WebRequest "$LangGraphUrl/threads/$TID/runs" -Method POST `
    -ContentType "application/json" -Body $body -UseBasicParsing
$RID = ($runJson.Content | ConvertFrom-Json).run_id
OK "Run launched: $RID"

# Save run info
@{
    thread_id = $TID
    run_id    = $RID
    target    = $Target
    model     = $Model
    started   = (Get-Date -Format "o")
    logdir    = $LOGDIR
    workspace = $WorkspaceDir
} | ConvertTo-Json | Set-Content "$LOGDIR\run-info.json" -Encoding UTF8

# ─── Start logger ─────────────────────────────────────────────────────────────
if (-not $NoLog) {
    $logArgs = "--tid `"$TID`" --rid `"$RID`" --logdir `"$LOGDIR`" --url `"$LangGraphUrl`""
    Start-Process python -ArgumentList "$loggerScript $logArgs" `
        -RedirectStandardOutput "$LOGDIR\logger-stdout.txt" `
        -RedirectStandardError  "$LOGDIR\logger-stderr.txt" `
        -NoNewWindow
    OK "Logger started → $LOGDIR"
}

# ─── Summary ──────────────────────────────────────────────────────────────────
Write-Host "`n" + ("=" * 70) -ForegroundColor Cyan
Write-Host " DECEPTICON RUNNING" -ForegroundColor White
Write-Host ("=" * 70) -ForegroundColor Cyan
Write-Host "  Target:    $Target" -ForegroundColor White
Write-Host "  Model:     $Model" -ForegroundColor White
Write-Host "  Thread:    $TID" -ForegroundColor Yellow
Write-Host "  Run:       $RID" -ForegroundColor Yellow
Write-Host "  Workspace: $WorkspaceDir" -ForegroundColor White
Write-Host "  Logs:      $LOGDIR" -ForegroundColor White
Write-Host ""
Write-Host "  Monitor options:" -ForegroundColor Gray
Write-Host "    # Live agent log:" -ForegroundColor DarkGray
Write-Host "    python $loggerScript --tid $TID --rid $RID --logdir $LOGDIR\monitor" -ForegroundColor Yellow
Write-Host ""
Write-Host "    # Container stdout:" -ForegroundColor DarkGray
Write-Host "    docker logs -f decepticon-langgraph" -ForegroundColor Yellow
Write-Host ""
Write-Host "    # Structured logs (after logger runs):" -ForegroundColor DarkGray
Write-Host "    Get-Content $LOGDIR\agent-log.txt -Wait" -ForegroundColor Yellow
Write-Host ("=" * 70) -ForegroundColor Cyan
