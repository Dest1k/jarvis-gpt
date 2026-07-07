param(
  [string]$HomePath = "D:\jarvis",
  [string]$Profile = "gemma4-mono"
)

$ErrorActionPreference = "Stop"

$env:JARVIS_HOME = $HomePath
$env:JARVIS_PROFILE = $Profile

Write-Host "Starting JARVIS GPT backend on http://localhost:8000"
Start-Process powershell -WindowStyle Hidden -ArgumentList @(
  "-NoExit",
  "-Command",
  "cd `"$PWD`"; `$env:JARVIS_HOME='$HomePath'; `$env:JARVIS_PROFILE='$Profile'; py -3.11 .\jarvis.py serve --reload"
)

Write-Host "Starting Command Center on http://localhost:3000"
Start-Process powershell -WindowStyle Hidden -ArgumentList @(
  "-NoExit",
  "-Command",
  "cd `"$PWD\frontend`"; `$env:NEXT_PUBLIC_JARVIS_API_URL='http://localhost:8000'; npm run dev"
)
