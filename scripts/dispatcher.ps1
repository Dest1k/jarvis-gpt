param(
  [ValidateSet("status", "env", "up", "down", "logs")]
  [string]$Action = "status",
  [string]$HomePath = "D:\jarvis",
  [string]$ModelRoot = "D:\jarvis\data\models",
  [string]$Profile = "gemma4-turbo"
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
    $dispatcherEnv = py -3.11 .\jarvis.py --profile $Profile dispatcher-compose --env |
      Out-String |
      ConvertFrom-Json
    if ($LASTEXITCODE -ne 0 -or -not $dispatcherEnv.JARVIS_QWEN_MODEL_PATH) {
      throw "Could not resolve the dispatcher model path."
    }
    $env:JARVIS_QWEN_MODEL_PATH = [string]$dispatcherEnv.JARVIS_QWEN_MODEL_PATH
    docker compose --profile llm logs --tail 120 dispatcher
  }
}
