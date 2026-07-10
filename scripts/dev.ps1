param(
  [string]$HomePath = "D:\jarvis",
  [string]$Profile = "gemma4-turbo"
)

$ErrorActionPreference = "Stop"

$env:JARVIS_HOME = $HomePath
$env:JARVIS_PROFILE = $Profile
if ([string]::IsNullOrWhiteSpace([string]$env:JARVIS_API_TOKEN)) {
  $bytes = New-Object byte[] 24
  $rng = [System.Security.Cryptography.RandomNumberGenerator]::Create()
  try {
    $rng.GetBytes($bytes)
  } finally {
    $rng.Dispose()
  }
  $env:JARVIS_API_TOKEN = [Convert]::ToBase64String($bytes).TrimEnd("=").Replace("+", "-").Replace("/", "_")
}
$apiToken = [string]$env:JARVIS_API_TOKEN

Write-Host "Starting Jarvis backend on http://localhost:8000"
Start-Process powershell -WindowStyle Hidden -ArgumentList @(
  "-NoExit",
  "-Command",
  "cd `"$PWD`"; `$env:JARVIS_HOME='$HomePath'; `$env:JARVIS_PROFILE='$Profile'; `$env:JARVIS_API_TOKEN='$apiToken'; py -3.11 .\jarvis.py serve --reload"
)

Write-Host "Starting Command Center on http://localhost:3000"
Start-Process powershell -WindowStyle Hidden -ArgumentList @(
  "-NoExit",
  "-Command",
  "cd `"$PWD\frontend`"; `$env:JARVIS_BACKEND_URL='http://127.0.0.1:8000'; `$env:JARVIS_API_TOKEN='$apiToken'; npm run dev"
)
