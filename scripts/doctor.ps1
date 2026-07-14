param(
  [switch]$SkipFrontend,
  [switch]$SkipHttp
)

$ErrorActionPreference = "Stop"

$argsList = @()
if ($SkipFrontend) {
  $argsList += "--skip-frontend"
}
if ($SkipHttp) {
  $argsList += "--skip-http"
}

py -3.11 .\scripts\smoke.py @argsList
# Propagate smoke.py process exit code. Windows PowerShell -File otherwise
# reports 0 even when the last native command failed (FUNC-FIND-016).
$smokeExit = $LASTEXITCODE
if ($null -eq $smokeExit) {
  $smokeExit = 1
}
exit $smokeExit
