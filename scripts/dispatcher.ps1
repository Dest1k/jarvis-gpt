param(
  [ValidateSet("status", "env", "up", "down", "logs")]
  [string]$Action = "status",
  [string]$HomePath = "D:\jarvis",
  [string]$ModelRoot = "D:\jarvis\data\models",
  [string]$Profile = "gemma4-mono"
)

$ErrorActionPreference = "Stop"

$env:JARVIS_HOME = $HomePath
$env:JARVIS_MODEL_ROOT = $ModelRoot
$env:JARVIS_PROFILE = $Profile

switch ($Action) {
  "status" {
    py -3.11 .\jarvis.py --profile $Profile dispatcher-status
  }
  "env" {
    py -3.11 .\jarvis.py --profile $Profile dispatcher-compose --env
  }
  "up" {
    py -3.11 .\jarvis.py --profile $Profile dispatcher-up
  }
  "down" {
    py -3.11 .\jarvis.py --profile $Profile dispatcher-down
  }
  "logs" {
    docker compose --profile llm logs --tail 120 dispatcher
  }
}
