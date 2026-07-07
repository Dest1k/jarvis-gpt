param(
  [string]$HomePath = "D:\jarvis"
)

$ErrorActionPreference = "Stop"

$dirs = @(
  $HomePath,
  (Join-Path $HomePath "models"),
  (Join-Path $HomePath "cache\jarvis-gpt"),
  (Join-Path $HomePath "data\jarvis-gpt\state"),
  (Join-Path $HomePath "logs\jarvis-gpt"),
  (Join-Path $HomePath "docker\jarvis-gpt")
)

foreach ($dir in $dirs) {
  New-Item -ItemType Directory -Force -Path $dir | Out-Null
}

$env:JARVIS_HOME = $HomePath
py -3.11 .\jarvis.py init
