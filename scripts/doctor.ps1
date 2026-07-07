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
