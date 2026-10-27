<#
.SYNOPSIS
    Start Boutiko locally and expose it to WhatsApp through a Cloudflare quick tunnel.

.DESCRIPTION
    1. docker compose up -d --build (Postgres + API)
    2. Wait for http://localhost:8000/health
    3. Run "cloudflared tunnel --url http://localhost:8000" and print the public webhook URL
    Press Ctrl+C to stop the tunnel. The containers keep running: "docker compose down" stops them.

.PARAMETER Port
    Host port for the API. Defaults to API_PORT from the environment, else 8000.

.EXAMPLE
    .\scripts\dev.ps1
    .\scripts\dev.ps1 -Port 8001
#>
param(
    [int]$Port = 0
)

$ErrorActionPreference = "Stop"
$root = Split-Path -Parent $PSScriptRoot
Set-Location $root

if ($Port -eq 0) {
    if ($env:API_PORT) { $Port = [int]$env:API_PORT } else { $Port = 8000 }
}
$env:API_PORT = "$Port"  # read by docker-compose.yml
$localUrl = "http://localhost:$Port"
$healthUrl = "$localUrl/health"
$timeoutSeconds = 90

function Fail($message) {
    Write-Host "ERROR: $message" -ForegroundColor Red
    exit 1
}

# --- Prerequisites ---
if (-not (Test-Path ".env")) {
    Fail ".env not found. Copy .env.example to .env and fill it in."
}
if (-not (Get-Command docker -ErrorAction SilentlyContinue)) {
    Fail "docker not found. Install Docker Desktop."
}
$cloudflared = Get-Command cloudflared -ErrorAction SilentlyContinue
if (-not $cloudflared) {
    Fail "cloudflared not found. Install it with:  winget install --id Cloudflare.cloudflared  (then open a new terminal)"
}

# Something else (an editor's port forwarding, another dev server...) may already hold the port
# on localhost and silently swallow our requests.
$dockerProcesses = "com.docker.backend", "com.docker.proxy", "wslrelay", "vpnkit"
$owners = Get-NetTCPConnection -LocalPort $Port -State Listen -ErrorAction SilentlyContinue |
    ForEach-Object { Get-Process -Id $_.OwningProcess -ErrorAction SilentlyContinue } |
    Where-Object { $dockerProcesses -notcontains $_.ProcessName } |
    Select-Object -ExpandProperty ProcessName -Unique
if ($owners) {
    Fail "Port $Port is already used by: $($owners -join ', '). Free it or run: .\scripts\dev.ps1 -Port 8001"
}

# --- Containers ---
Write-Host "==> Starting containers (docker compose up -d --build)..." -ForegroundColor Cyan
docker compose up -d --build
if ($LASTEXITCODE -ne 0) {
    Fail "docker compose failed. Is Docker Desktop running?"
}

# --- Health check ---
Write-Host "==> Waiting for $healthUrl ..." -ForegroundColor Cyan
$deadline = (Get-Date).AddSeconds($timeoutSeconds)
$healthy = $false
while ((Get-Date) -lt $deadline) {
    try {
        $response = Invoke-WebRequest -Uri $healthUrl -UseBasicParsing -TimeoutSec 3
        if ($response.StatusCode -eq 200) { $healthy = $true; break }
    } catch {
        Start-Sleep -Seconds 2
    }
}
if (-not $healthy) {
    Write-Host "Last API logs:" -ForegroundColor Yellow
    docker compose logs --tail 40 api
    Fail "API did not answer on $healthUrl within $timeoutSeconds s."
}
Write-Host "API is up." -ForegroundColor Green

# --- Tunnel ---
$logFile = Join-Path $env:TEMP "boutiko-cloudflared.log"
$outFile = Join-Path $env:TEMP "boutiko-cloudflared.out.log"
Remove-Item $logFile, $outFile -ErrorAction SilentlyContinue

Write-Host "==> Starting cloudflared tunnel..." -ForegroundColor Cyan
# cloudflared prints the public URL on stderr.
$tunnel = Start-Process -FilePath $cloudflared.Source `
    -ArgumentList "tunnel", "--url", $localUrl `
    -RedirectStandardError $logFile -RedirectStandardOutput $outFile `
    -NoNewWindow -PassThru

try {
    $publicUrl = $null
    $deadline = (Get-Date).AddSeconds(45)
    while (-not $publicUrl -and (Get-Date) -lt $deadline -and -not $tunnel.HasExited) {
        Start-Sleep -Seconds 1
        if (Test-Path $logFile) {
            $match = Select-String -Path $logFile -Pattern "https://[a-z0-9-]+\.trycloudflare\.com" | Select-Object -First 1
            if ($match) { $publicUrl = $match.Matches[0].Value }
        }
    }
    if (-not $publicUrl) {
        if (Test-Path $logFile) { Get-Content $logFile -Tail 20 }
        Fail "Could not get a public URL from cloudflared (log: $logFile)."
    }

    Write-Host ""
    Write-Host "============================================================" -ForegroundColor Green
    Write-Host " Public URL   : $publicUrl"
    Write-Host " Callback URL : $publicUrl/webhook" -ForegroundColor Yellow
    Write-Host " Verify token : the WHATSAPP_VERIFY_TOKEN value in your .env"
    Write-Host "============================================================" -ForegroundColor Green
    Write-Host ""
    Write-Host "Paste the callback URL in Meta: WhatsApp > Configuration > Webhook > Edit,"
    Write-Host "then subscribe to the 'messages' field and send a message to the test number."
    Write-Host ""
    Write-Host "API logs     : docker compose logs -f api"
    Write-Host "Tunnel log   : $logFile"
    Write-Host "Press Ctrl+C to stop the tunnel (the URL changes on every restart)."

    Wait-Process -Id $tunnel.Id
}
finally {
    if (-not $tunnel.HasExited) {
        Stop-Process -Id $tunnel.Id -Force -ErrorAction SilentlyContinue
        Write-Host "Tunnel stopped. Containers are still running (docker compose down to stop them)."
    }
}
