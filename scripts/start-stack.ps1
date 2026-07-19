# Start Jarvis backend + frontend as durable Windows scheduled tasks.
# Processes are NOT children of the calling shell/job, so agent tooling and
# terminal sessions can exit without killing the stack.
param(
  [string]$RepoRoot = (Resolve-Path (Join-Path $PSScriptRoot "..")).Path,
  [string]$HomePath = "D:\jarvis",
  [string]$ProfileName = "qwen36-vl",
  [string]$BackendHost = "127.0.0.1",
  [int]$BackendPort = 8000,
  [string]$FrontendHost = "0.0.0.0",
  [int]$FrontendPort = 3000
)

$ErrorActionPreference = "Stop"
$logDir = Join-Path $HomePath "logs\jarvis-gpt"
$apiTokenFile = Join-Path $HomePath ".jarvis\api.token"
$python = @(
  "$env:LOCALAPPDATA\Programs\Python\Python311\python.exe",
  "C:\Users\Admin\AppData\Local\Programs\Python\Python311\python.exe",
  "C:\Windows\py.exe"
) | Where-Object { Test-Path $_ } | Select-Object -First 1
$node = @(
  "C:\Program Files\nodejs\node.exe",
  "$env:ProgramFiles\nodejs\node.exe"
) | Where-Object { Test-Path $_ } | Select-Object -First 1

if (-not $python) { throw "Python 3.11 not found" }
if (-not $node) { throw "node.exe not found" }
if (-not (Test-Path $apiTokenFile)) { throw "Missing API token file: $apiTokenFile" }

New-Item -ItemType Directory -Force -Path $logDir | Out-Null
$token = (Get-Content -LiteralPath $apiTokenFile -Raw).Trim()
if (-not $token) { throw "API token file is empty: $apiTokenFile" }

$frontendRoot = Join-Path $RepoRoot "frontend"
$nextBin = Join-Path $frontendRoot "node_modules\next\dist\bin\next"
if (-not (Test-Path $nextBin)) {
  throw "Next.js binary missing. Run npm ci && npm run build in frontend/ first."
}
if (-not (Test-Path (Join-Path $frontendRoot ".next\BUILD_ID"))) {
  throw "Frontend build missing (.next). Run: npm run build in frontend/"
}

$feBat = Join-Path $logDir "run-fe.bat"
$beBat = Join-Path $logDir "run-be.bat"

@"
@echo off
set JARVIS_API_TOKEN=$token
set JARVIS_HOME=$HomePath
set JARVIS_BACKEND_URL=http://127.0.0.1:$BackendPort
set NODE_ENV=production
cd /d "$frontendRoot"
"$node" "$nextBin" start -H $FrontendHost -p $FrontendPort >> "$logDir\frontend.runtime.log" 2>&1
"@ | Set-Content -LiteralPath $feBat -Encoding ASCII

@"
@echo off
set JARVIS_API_TOKEN=$token
set JARVIS_HOME=$HomePath
set JARVIS_PROFILE=$ProfileName
cd /d "$RepoRoot"
"$python" "$RepoRoot\jarvis.py" --profile $ProfileName serve --host $BackendHost --port $BackendPort >> "$logDir\backend.runtime.log" 2>&1
"@ | Set-Content -LiteralPath $beBat -Encoding ASCII

function Stop-PortListeners([int]$Port) {
  $lines = netstat -ano | Select-String "LISTENING" | Select-String ":$Port\s"
  foreach ($line in $lines) {
    if ($line.Line -match "\s+(\d+)\s*$") {
      $procId = [int]$Matches[1]
      if ($procId -gt 0) {
        Stop-Process -Id $procId -Force -ErrorAction SilentlyContinue
      }
    }
  }
}

Stop-PortListeners -Port $FrontendPort
Stop-PortListeners -Port $BackendPort
Start-Sleep -Seconds 1

foreach ($name in @("JarvisFrontend", "JarvisBackend")) {
  schtasks /Delete /TN $name /F 2>$null | Out-Null
}

# /IT = interactive token of the logged-on user so localhost is the same session.
schtasks /Create /TN "JarvisFrontend" /TR "`"$feBat`"" /SC ONCE /ST 00:00 /RL LIMITED /F /IT | Out-Null
schtasks /Create /TN "JarvisBackend" /TR "`"$beBat`"" /SC ONCE /ST 00:00 /RL LIMITED /F /IT | Out-Null
schtasks /Run /TN "JarvisBackend" | Out-Null
schtasks /Run /TN "JarvisFrontend" | Out-Null

$deadline = (Get-Date).AddSeconds(45)
$feUp = $false
$beUp = $false
while ((Get-Date) -lt $deadline) {
  Start-Sleep -Seconds 1
  $listen = netstat -ano | Select-String "LISTENING"
  if (-not $feUp -and ($listen | Select-String ":$FrontendPort\s")) { $feUp = $true }
  if (-not $beUp -and ($listen | Select-String ":$BackendPort\s")) { $beUp = $true }
  if ($feUp -and $beUp) { break }
}

if (-not $feUp -or -not $beUp) {
  Write-Host "frontend_up=$feUp backend_up=$beUp" -ForegroundColor Yellow
  if (Test-Path "$logDir\frontend.runtime.log") {
    Write-Host "--- frontend log ---" -ForegroundColor DarkYellow
    Get-Content "$logDir\frontend.runtime.log" -Tail 20
  }
  if (Test-Path "$logDir\backend.runtime.log") {
    Write-Host "--- backend log ---" -ForegroundColor DarkYellow
    Get-Content "$logDir\backend.runtime.log" -Tail 20
  }
  throw "Stack failed to become ready (frontend=$feUp backend=$beUp)"
}

Write-Host "OK frontend http://localhost:$FrontendPort/  backend http://127.0.0.1:$BackendPort/" -ForegroundColor Green
