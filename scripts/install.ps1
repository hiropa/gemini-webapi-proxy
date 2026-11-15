# One-command install on Windows with Docker Desktop (PowerShell 5.1 or 7):
#
#   powershell -ExecutionPolicy Bypass -File .\scripts\install.ps1
#   powershell -ExecutionPolicy Bypass -File .\scripts\install.ps1 -Listen 0.0.0.0 -Port 4990
#
# Writes .env on the first run (API key and admin password), builds and starts the
# proxy and the sign-in browser in Docker Desktop mode (docker-compose.desktop.yml),
# waits for /health and prints where to go. Running it again keeps .env and the data.
param(
    [string]$Listen = "",
    [int]$Port = 0
)
$ErrorActionPreference = "Stop"
Set-Location (Split-Path -Parent $PSScriptRoot)

if (-not (Get-Command docker -ErrorAction SilentlyContinue)) {
    Write-Error "Docker Desktop is required: https://docs.docker.com/desktop/setup/install/windows-install/"
}
docker compose version *> $null
if ($LASTEXITCODE -ne 0) { Write-Error "Docker Compose v2 is required (start Docker Desktop first)" }

# No host networking on Docker Desktop: see docker-compose.desktop.yml
$env:COMPOSE_FILE = "docker-compose.yml;docker-compose.desktop.yml"
$env:COMPOSE_PATH_SEPARATOR = ";"

function New-Secret {
    $bytes = New-Object byte[] 32
    [System.Security.Cryptography.RandomNumberGenerator]::Create().GetBytes($bytes)
    return ([Convert]::ToBase64String($bytes) -replace '[+/=]', '')
}

function Write-EnvFile([string[]]$Lines) {
    # UTF-8 without BOM and LF line ends: docker compose and the proxy read it on Linux.
    $text = ($Lines -join "`n") + "`n"
    [System.IO.File]::WriteAllText((Join-Path (Get-Location) ".env"), $text, (New-Object System.Text.UTF8Encoding $false))
}

if (-not $Mode) {
    $answer = Read-Host "Install this machine as master or slave? [master]"
    $Mode = if ($answer -and $answer.Trim()) { $answer.Trim().ToLower() } else { "master" }
    if ($Mode -ne "master" -and $Mode -ne "slave") { Write-Error "Mode must be 'master' or 'slave'" }
}
Write-Host "== Mode: $Mode (GOP_MODE in .env; docs/nodes.md)"

$newPassword = ""
$newNodeToken = ""
if (-not (Test-Path .env)) {
    $newPassword = New-Secret
    if ($Mode -eq "slave") { $newNodeToken = "gop-node-$(New-Secret)" }
    $hostValue = if ($Listen) { $Listen } else { "127.0.0.1" }
    $portValue = if ($Port) { $Port } else { 4982 }
    $envLines = @(
        "# Written by scripts/install.ps1. All settings: docs/configuration.md",
        "GOP_HOST=$hostValue",
        "GOP_PORT=$portValue",
        "# master: serves clients and manages accounts; slave: its accounts serve a master",
        "GOP_MODE=$Mode",
        "# Bearer key for OpenAI clients (master key; named client keys: admin UI -> Keys)",
        "GOP_API_KEY=$(New-Secret)",
        "# Password of the admin UI at /admin/ui",
        "GOP_ADMIN_PASSWORD=$newPassword"
    )
    if ($newNodeToken) {
        $envLines += "# Token of this slave's /node API - give it to the master when linking (its Nodes tab)"
        $envLines += "GOP_NODE_TOKEN=$newNodeToken"
    }
    Write-EnvFile $envLines
    # Only the current user may read it: it holds the API key and the admin password.
    icacls .env /inheritance:r /grant:r "$($env:USERNAME):(R,W)" *> $null
    Write-Host "== Created .env (keep it private: it holds the API key and admin password)"
} else {
    $lines = Get-Content .env
    if ($Listen) { $lines = $lines -replace '^GOP_HOST=.*', "GOP_HOST=$Listen" }
    if ($Port) { $lines = $lines -replace '^GOP_PORT=.*', "GOP_PORT=$Port" }
    if ($lines -notmatch '^GOP_MODE=') { $lines += "GOP_MODE=$Mode" }
    elseif ($PSBoundParameters.ContainsKey('Mode')) { $lines = $lines -replace '^GOP_MODE=.*', "GOP_MODE=$Mode" }
    if ($Mode -eq "slave" -and $lines -notmatch '^GOP_NODE_TOKEN=') {
        $newNodeToken = "gop-node-$(New-Secret)"
        $lines += "# Token of this slave's /node API - give it to the master when linking (its Nodes tab)"
        $lines += "GOP_NODE_TOKEN=$newNodeToken"
    }
    Write-EnvFile $lines
    Write-Host "== Keeping the existing .env"
}

$settings = @{}
foreach ($line in Get-Content .env) {
    if ($line -match '^\s*([A-Z_]+)=(.*)$') { $settings[$Matches[1]] = $Matches[2] }
}
$hostSetting = if ($settings["GOP_HOST"]) { $settings["GOP_HOST"] } else { "127.0.0.1" }
$portSetting = if ($settings["GOP_PORT"]) { $settings["GOP_PORT"] } else { "4982" }

New-Item -ItemType Directory -Force -Path data | Out-Null

Write-Host "== Building images (first run takes a few minutes)"
docker compose build
if ($LASTEXITCODE -ne 0) { Write-Error "docker compose build failed" }

Write-Host "== Starting"
docker compose up -d
if ($LASTEXITCODE -ne 0) { Write-Error "docker compose up failed" }

Write-Host "== Waiting for /health"
for ($i = 0; $i -lt 60; $i++) {
    try {
        $r = Invoke-WebRequest -UseBasicParsing -TimeoutSec 5 "http://127.0.0.1:$portSetting/health"
        if ($r.Content -match '"ok"') {
            $shown = if ($hostSetting -eq "0.0.0.0") { $env:COMPUTERNAME } else { $hostSetting }
            Write-Host ""
            Write-Host "Ready."
            Write-Host "  Admin UI:   http://${shown}:$portSetting/admin/ui"
            if ($newPassword) { Write-Host "  Password:   $newPassword" }
            else { Write-Host "  Password:   GOP_ADMIN_PASSWORD in .env" }
            Write-Host "  OpenAI API: http://${shown}:$portSetting/openai/v1   (Bearer: GOP_API_KEY from .env)"
            Write-Host ""
            if ($Mode -eq "slave") {
                $token = if ($newNodeToken) { $newNodeToken } else { (Get-Content .env | Where-Object { $_ -match '^GOP_NODE_TOKEN=' }) -replace '^GOP_NODE_TOKEN=', '' }
                Write-Host ""
                Write-Host "This machine is a SLAVE: it holds accounts for a master and cannot add or"
                Write-Host "change them itself. From the MASTER's admin UI (Nodes tab) link it with:"
                Write-Host "  URL:   http://${shown}:$portSetting  (make sure the master can reach it)"
                Write-Host "  Token: $token"
                Write-Host "Accounts are then created and managed on the master; their traffic to Google"
                Write-Host "leaves via this machine's IP only."
            } else {
                Write-Host ""
                Write-Host "Next: open the admin UI, press the sign-in button on the default account and sign"
                Write-Host "in to Google in the browser shown on the page. Add more accounts with '+'."
                Write-Host "To use another machine's accounts from here, install a slave there and link it"
                Write-Host "in the Nodes tab (docs/nodes.md)."
            }
            exit 0
        }
    } catch { }
    Start-Sleep -Seconds 3
}

Write-Host "Health check timed out - recent logs:"
docker compose logs --tail=80
exit 1
