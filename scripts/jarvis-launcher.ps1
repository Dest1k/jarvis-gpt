param(
  [Parameter(Position = 0)]
  [ValidateSet("menu", "start", "app", "stop", "restart", "status", "llm", "logs", "doctor", "open")]
  [string]$Action = "menu",

  [ValidateSet("gemma4-turbo", "gemma4-mono", "gemma4-mono-perf")]
  [string]$Profile = "gemma4-turbo",

  # Explicit advanced/CLI opt-in for experimental or unsupported research profiles.
  [switch]$AllowExperimentalProfiles,

  [switch]$IUnderstandExperimentalProfile,

  [string]$HomePath = "D:\jarvis",
  [string]$ModelRoot = "D:\jarvis\data\models",

  [switch]$NoDispatcher,
  [switch]$NoBackend,
  [switch]$NoFrontend,
  [switch]$NoBridge,
  [switch]$Lan,
  [switch]$LocalOnly,
  [switch]$WatchLlm,
  [switch]$BuildFrontend,
  [switch]$DevFrontend,
  [switch]$NoDockerStart,
  [int]$DockerWaitSec = 240
)

$ErrorActionPreference = "Stop"
$script:AllowExperimentalProfiles = [bool]$AllowExperimentalProfiles
$script:IUnderstandExperimentalProfile = [bool]$IUnderstandExperimentalProfile

$RepoRoot = (Resolve-Path (Join-Path $PSScriptRoot "..")).Path
$FrontendRoot = Join-Path $RepoRoot "frontend"
$LogDir = Join-Path $HomePath "logs\jarvis-gpt"
$StateDir = Join-Path $HomePath "data\jarvis-gpt\state"
$StateFile = Join-Path $StateDir "launcher-state.json"
$BridgeTokenFile = Join-Path $HomePath ".jarvis\bridge.token"
$BridgePolicyRevision = "native-app-v2"
$ApiTokenFile = Join-Path $HomePath ".jarvis\api.token"
$script:ConfiguredApiToken = [string]$env:JARVIS_API_TOKEN
$script:ConfiguredCorsOrigins = [string]$env:JARVIS_CORS_ORIGINS
if ($Lan) {
  throw "-Lan is temporarily disabled while Command Center browser authentication is removed."
}
$script:LanMode = $false

function Get-LanIPv4 {
  try {
    $candidate = Get-NetIPConfiguration |
      Where-Object { $_.IPv4DefaultGateway -and $_.NetAdapter.Status -eq "Up" } |
      ForEach-Object { $_.IPv4Address } |
      Where-Object { $_ -and $_.IPAddress -and $_.IPAddress -notmatch "^(127\.|169\.254\.)" } |
      Select-Object -First 1
    if ($candidate) {
      return [string]$candidate.IPAddress
    }
  } catch {
  }

  try {
    $candidate = Get-NetIPAddress -AddressFamily IPv4 |
      Where-Object {
        $_.IPAddress -and
        $_.IPAddress -notmatch "^(127\.|169\.254\.)" -and
        $_.PrefixOrigin -ne "WellKnown"
      } |
      Sort-Object InterfaceMetric |
      Select-Object -First 1
    if ($candidate) {
      return [string]$candidate.IPAddress
    }
  } catch {
  }

  return $null
}

function Get-PublicHost {
  if ($script:LanMode) {
    $lanIp = Get-LanIPv4
    if ($lanIp) {
      return $lanIp
    }
  }
  return "127.0.0.1"
}

function Get-FrontendBindHost {
  if ($script:LanMode) {
    return "0.0.0.0"
  }
  return "127.0.0.1"
}

function Get-BackendUrl {
  return "http://127.0.0.1:8000"
}

function Get-FrontendUrl {
  return "http://$(Get-PublicHost):3000"
}

function Get-OrCreateApiToken {
  if (-not [string]::IsNullOrWhiteSpace($script:ConfiguredApiToken)) {
    return $script:ConfiguredApiToken.Trim()
  }
  if (Test-Path -LiteralPath $ApiTokenFile) {
    $stored = (Get-Content -LiteralPath $ApiTokenFile -Raw).Trim()
    if ($stored) {
      Protect-ApiTokenFile
      return $stored
    }
  }

  $tokenDir = Split-Path -Parent $ApiTokenFile
  New-Item -ItemType Directory -Force -Path $tokenDir | Out-Null
  $bytes = New-Object byte[] 32
  $rng = [System.Security.Cryptography.RandomNumberGenerator]::Create()
  try {
    $rng.GetBytes($bytes)
  } finally {
    $rng.Dispose()
  }
  $token = [Convert]::ToBase64String($bytes).TrimEnd([char[]]"=").Replace("+", "-").Replace("/", "_")
  Set-Content -LiteralPath $ApiTokenFile -Value $token -Encoding Ascii -NoNewline
  Protect-ApiTokenFile
  return $token
}

function Protect-ApiTokenFile {
  try {
    $identity = [System.Security.Principal.WindowsIdentity]::GetCurrent().User.Value
    $icacls = Join-Path $env:SystemRoot "System32\icacls.exe"
    foreach ($arguments in @(
      @($ApiTokenFile, "/reset", "/Q"),
      @($ApiTokenFile, "/inheritance:r", "/Q"),
      @($ApiTokenFile, "/grant:r", "*$($identity):(F)", "/Q")
    )) {
      & $icacls @arguments | Out-Null
      if ($LASTEXITCODE -ne 0) {
        throw "icacls exited with code $LASTEXITCODE"
      }
    }
  } catch {
    throw "Could not restrict API token file permissions: $($_.Exception.Message)"
  }
}

function Get-StringSha256 {
  param([string]$Value)

  $sha = [System.Security.Cryptography.SHA256]::Create()
  try {
    $bytes = [System.Text.Encoding]::UTF8.GetBytes($Value)
    $hash = $sha.ComputeHash($bytes)
    return (($hash | ForEach-Object { $_.ToString("x2") }) -join "")
  } finally {
    $sha.Dispose()
  }
}

function Get-FrontendEnvironmentSha256 {
  $values = @(
    [string]$env:JARVIS_BACKEND_URL
    [string]$env:JARVIS_API_TOKEN
  )
  return Get-StringSha256 -Value ($values -join "`n")
}

function Get-BackendEnvironmentSha256 {
  $values = @(
    [string]$env:JARVIS_HOME
    [string]$env:JARVIS_MODEL_ROOT
    [string]$env:JARVIS_PROFILE
    [string]$env:JARVIS_LLM_BASE_URL
    [string]$env:JARVIS_LLM_MODEL
    [string]$env:JARVIS_API_HOST
    [string]$env:JARVIS_API_PORT
    [string]$env:JARVIS_API_TOKEN
    [string]$env:JARVIS_API_REQUIRE_TOKEN_ON_LOOPBACK
    [string]$env:JARVIS_CORS_ORIGINS
    [string]$env:JARVIS_EXECUTION_ROOTS
    [string]$env:JARVIS_EXECUTION_CAPABILITIES_FILE
    [string]$env:JARVIS_BRIDGE_APP_PATHS_JSON
  )
  return Get-StringSha256 -Value ($values -join "`n")
}

function Get-ProfileCertification {
  param([string]$SelectedProfile)

  switch ($SelectedProfile) {
    "gemma4-turbo" {
      return @{
        name = "gemma4-turbo"
        certification = "certified"
        interactive_certified = $true
        default_recommended = $true
        research_only = $false
        readiness_deadline_sec = 180
        menu_visible = $true
        requires_experimental_opt_in = $false
        certification_reason = "Certified interactive default on the current host."
      }
    }
    "gemma4-mono-perf" {
      return @{
        name = "gemma4-mono-perf"
        certification = "experimental"
        interactive_certified = $false
        default_recommended = $false
        research_only = $true
        readiness_deadline_sec = 900
        menu_visible = $false
        requires_experimental_opt_in = $true
        certification_reason = "Experimental/research-only on this host (RESOLVED_BY_PRODUCT_DECISION)."
      }
    }
    "gemma4-mono" {
      return @{
        name = "gemma4-mono"
        certification = "unsupported"
        interactive_certified = $false
        default_recommended = $false
        research_only = $true
        readiness_deadline_sec = 1200
        menu_visible = $false
        requires_experimental_opt_in = $true
        certification_reason = "Unsupported interactive/research-only on this host (RESOLVED_BY_PRODUCT_DECISION)."
      }
    }
    default {
      throw "Unknown profile: $SelectedProfile"
    }
  }
}

function Assert-ProfileAllowed {
  param([string]$SelectedProfile)

  $cert = Get-ProfileCertification -SelectedProfile $SelectedProfile
  if (-not [bool]$cert.requires_experimental_opt_in) {
    return $cert
  }
  if (
    -not [bool]$script:AllowExperimentalProfiles -or
    -not [bool]$script:IUnderstandExperimentalProfile
  ) {
    throw (
      "Profile '$SelectedProfile' is $($cert.certification) " +
      "($($cert.certification_reason)). " +
      "Normal interactive menu shows only certified gemma4-turbo. " +
      "To opt in for research, re-run with -AllowExperimentalProfiles " +
      "-IUnderstandExperimentalProfile -Profile $SelectedProfile."
    )
  }
  Write-Host (
    "EXPERIMENTAL OPT-IN: profile=$SelectedProfile certification=$($cert.certification) " +
    "deadline=$($cert.readiness_deadline_sec)s research_only=$($cert.research_only)"
  ) -ForegroundColor Yellow
  return $cert
}

function Set-JarvisEnvironment {
  param([string]$SelectedProfile)

  $cert = Assert-ProfileAllowed -SelectedProfile $SelectedProfile
  $apiToken = Get-OrCreateApiToken
  $corsOrigins = @($script:ConfiguredCorsOrigins -split ",") |
    ForEach-Object { $_.Trim().TrimEnd("/") } |
    Where-Object { $_ } |
    Select-Object -Unique

  $env:JARVIS_HOME = $HomePath
  $env:JARVIS_MODEL_ROOT = $ModelRoot
  $env:JARVIS_PROFILE = $SelectedProfile
  $env:JARVIS_PROFILE_CERTIFICATION = [string]$cert.certification
  $env:JARVIS_PROFILE_READINESS_DEADLINE_SEC = [string]$cert.readiness_deadline_sec
  $env:JARVIS_LLM_BASE_URL = "http://localhost:8001/v1"
  $env:JARVIS_LLM_MODEL = "dispatcher"
  $env:JARVIS_API_HOST = "127.0.0.1"
  $env:JARVIS_API_PORT = "8000"
  $env:JARVIS_API_TOKEN = $apiToken
  $env:JARVIS_CORS_ORIGINS = $corsOrigins -join ","
  $env:JARVIS_BACKEND_URL = "http://127.0.0.1:8000"
}

function Ensure-LauncherFolders {
  New-Item -ItemType Directory -Force -Path $LogDir, $StateDir | Out-Null
}

function Write-Banner {
  Clear-Host
  Write-Host "+--------------------------------------------------------------+" -ForegroundColor DarkCyan
  Write-Host "|                            JARVIS                            |" -ForegroundColor Cyan
  Write-Host "+--------------------------------------------------------------+" -ForegroundColor DarkCyan
  Write-Host ("| Repo:    {0}" -f $RepoRoot)
  Write-Host ("| Home:    {0}" -f $HomePath)
  Write-Host ("| Profile: {0}" -f $Profile)
  Write-Host "+--------------------------------------------------------------+" -ForegroundColor DarkCyan
  Write-Host ""
}

function Select-Menu {
  param(
    [string]$Title,
    [array]$Items
  )

  $selected = 0
  while ($true) {
    Write-Banner
    Write-Host $Title -ForegroundColor White
    Write-Host "Use Up/Down, Enter to select, Esc to cancel." -ForegroundColor DarkGray
    Write-Host ""

    for ($index = 0; $index -lt $Items.Count; $index += 1) {
      $item = $Items[$index]
      if ($index -eq $selected) {
        Write-Host (" > {0,-24} {1}" -f $item.Label, $item.Hint) -ForegroundColor Black -BackgroundColor Cyan
      } else {
        Write-Host ("   {0,-24} {1}" -f $item.Label, $item.Hint) -ForegroundColor Gray
      }
    }

    $key = $Host.UI.RawUI.ReadKey("NoEcho,IncludeKeyDown")
    switch ($key.VirtualKeyCode) {
      38 { $selected = ($selected + $Items.Count - 1) % $Items.Count }
      40 { $selected = ($selected + 1) % $Items.Count }
      13 { return $Items[$selected] }
      27 { return $null }
    }
  }
}

function Get-PortOwner {
  param([int]$Port)

  try {
    $connection = Get-NetTCPConnection -LocalPort $Port -State Listen -ErrorAction Stop |
      Select-Object -First 1
    if ($connection) {
      return [int]$connection.OwningProcess
    }
  } catch {
    return $null
  }
  return $null
}

function Get-PortListenAddresses {
  param([int]$Port)

  try {
    return @(
      Get-NetTCPConnection -LocalPort $Port -State Listen -ErrorAction Stop |
        Select-Object -ExpandProperty LocalAddress -Unique
    )
  } catch {
    return @()
  }
}

function Test-PortExposedForLan {
  param([int]$Port)

  $addresses = @(Get-PortListenAddresses -Port $Port)
  $lanIp = Get-LanIPv4
  return [bool](
    $addresses -contains "0.0.0.0" -or
    $addresses -contains "::" -or
    $addresses -contains $lanIp
  )
}

function Ensure-LanFirewallRules {
  if (-not $script:LanMode) {
    return
  }

  $isAdmin = ([Security.Principal.WindowsPrincipal][Security.Principal.WindowsIdentity]::GetCurrent()).IsInRole(
    [Security.Principal.WindowsBuiltinRole]::Administrator
  )
  if (-not $isAdmin) {
    Write-Host "Firewall rules were not changed because the launcher is not elevated." -ForegroundColor DarkYellow
    Write-Host "If another device cannot connect, run this command as Administrator once." -ForegroundColor DarkGray
    return
  }

  foreach ($port in @(3000)) {
    $name = "Jarvis LAN $port"
    try {
      $existing = Get-NetFirewallRule -DisplayName $name -ErrorAction SilentlyContinue
      if (-not $existing) {
        New-NetFirewallRule `
          -DisplayName $name `
          -Direction Inbound `
          -Action Allow `
          -Protocol TCP `
          -LocalPort $port `
          -Profile Private `
          -Description "Allow Jarvis LAN access on TCP $port." | Out-Null
      }
    } catch {
      Write-Host ("Could not create firewall rule for TCP {0}: {1}" -f $port, $_.Exception.Message) -ForegroundColor DarkYellow
    }
  }
}

function Test-PortOpen {
  param([int]$Port)
  $client = [System.Net.Sockets.TcpClient]::new()
  try {
    $async = $client.BeginConnect("127.0.0.1", $Port, $null, $null)
    if (-not $async.AsyncWaitHandle.WaitOne(500, $false)) {
      return $false
    }
    $client.EndConnect($async)
    return $true
  } catch {
    return $false
  } finally {
    $client.Close()
  }
}

function Wait-PortClosed {
  param(
    [int]$Port,
    [int]$TimeoutMs = 5000
  )

  $deadline = (Get-Date).AddMilliseconds($TimeoutMs)
  while ((Get-Date) -lt $deadline) {
    if (-not (Test-PortOpen -Port $Port)) {
      return $true
    }
    Start-Sleep -Milliseconds 250
  }
  return -not (Test-PortOpen -Port $Port)
}

function Invoke-HttpProbe {
  param(
    [string]$Uri,
    [int]$TimeoutSec = 3,
    [switch]$Json
  )

  try {
    if ($Json) {
      $data = Invoke-RestMethod -Uri $Uri -Method Get -TimeoutSec $TimeoutSec
      return @{ ok = $true; status = 200; data = $data; error = "" }
    }
    $response = Invoke-WebRequest -UseBasicParsing -Uri $Uri -Method Get -TimeoutSec $TimeoutSec
    return @{ ok = $true; status = [int]$response.StatusCode; data = $response.Content; error = "" }
  } catch {
    $statusCode = $null
    if ($_.Exception.Response) {
      try {
        $statusCode = [int]$_.Exception.Response.StatusCode
      } catch {
        $statusCode = $null
      }
    }
    return @{ ok = $false; status = $statusCode; data = $null; error = $_.Exception.Message }
  }
}

function Test-DockerReady {
  $docker = Get-Command docker -ErrorAction SilentlyContinue
  if (-not $docker) {
    return @{ ready = $false; available = $false; error = "Docker CLI is not available in PATH" }
  }

  try {
    $output = @(& $docker.Source info --format "{{.ServerVersion}}" 2>&1)
    if ($LASTEXITCODE -eq 0) {
      return @{ ready = $true; available = $true; error = ""; version = ($output -join "`n").Trim() }
    }
    return @{ ready = $false; available = $true; error = (($output -join "`n").Trim()) }
  } catch {
    return @{ ready = $false; available = $true; error = $_.Exception.Message }
  }
}

function Resolve-DockerDesktopPath {
  $candidates = @()
  if ($env:ProgramFiles) {
    $candidates += Join-Path $env:ProgramFiles "Docker\Docker\Docker Desktop.exe"
  }
  if (${env:ProgramFiles(x86)}) {
    $candidates += Join-Path ${env:ProgramFiles(x86)} "Docker\Docker\Docker Desktop.exe"
  }
  if ($env:LOCALAPPDATA) {
    $candidates += Join-Path $env:LOCALAPPDATA "Docker\Docker Desktop.exe"
  }

  foreach ($candidate in $candidates) {
    if ($candidate -and (Test-Path -LiteralPath $candidate)) {
      return $candidate
    }
  }

  $command = Get-Command "Docker Desktop.exe" -ErrorAction SilentlyContinue
  if ($command) {
    return $command.Source
  }
  return $null
}

function Start-DockerDesktop {
  $desktopPath = Resolve-DockerDesktopPath
  if (-not $desktopPath) {
    Write-Host "Docker Desktop executable was not found." -ForegroundColor DarkYellow
    return $false
  }

  $alreadyLaunching = Get-Process -ErrorAction SilentlyContinue |
    Where-Object { $_.ProcessName -in @("Docker Desktop", "com.docker.backend", "Docker Desktop Installer") } |
    Select-Object -First 1

  if ($alreadyLaunching) {
    Write-Host "Docker Desktop is already starting; waiting for the Docker API..." -ForegroundColor DarkYellow
    return $true
  }

  Write-Host ("Starting Docker Desktop: {0}" -f $desktopPath) -ForegroundColor Yellow
  try {
    Start-Process -FilePath $desktopPath -WindowStyle Hidden | Out-Null
    return $true
  } catch {
    Write-Host ("Could not start Docker Desktop: {0}" -f $_.Exception.Message) -ForegroundColor DarkYellow
    return $false
  }
}

function Ensure-DockerReady {
  param([int]$TimeoutSec = 240)

  $probe = Test-DockerReady
  if ($probe.ready) {
    Write-Host ("Docker API is ready ({0})." -f $probe.version) -ForegroundColor Green
    return $true
  }

  if (-not $probe.available) {
    Write-Host $probe.error -ForegroundColor Red
    return $false
  }

  if ($NoDockerStart) {
    Write-Host ("Docker API is not ready: {0}" -f $probe.error) -ForegroundColor DarkYellow
    return $false
  }

  if (-not (Start-DockerDesktop)) {
    return $false
  }
  $deadline = (Get-Date).AddSeconds([Math]::Max(1, $TimeoutSec))
  $lastError = $probe.error
  $lastNoticeAt = (Get-Date).AddSeconds(-30)

  while ((Get-Date) -lt $deadline) {
    Start-Sleep -Seconds 3
    $probe = Test-DockerReady
    if ($probe.ready) {
      Write-Host ("Docker API is ready ({0})." -f $probe.version) -ForegroundColor Green
      return $true
    }
    $lastError = $probe.error
    if (((Get-Date) - $lastNoticeAt).TotalSeconds -ge 15) {
      Write-Host "Waiting for Docker API..." -ForegroundColor DarkGray
      $lastNoticeAt = Get-Date
    }
  }

  Write-Host ("Docker API did not become ready within {0}s: {1}" -f $TimeoutSec, $lastError) -ForegroundColor Red
  return $false
}

function Get-DispatcherContainerSnapshot {
  $docker = Get-Command docker -ErrorAction SilentlyContinue
  if (-not $docker) {
    return @{
      docker_available = $false
      exists = $false
      running = $false
      state = "missing"
      status = "Docker is not available in PATH"
      image = ""
    }
  }

  try {
    $line = & $docker.Source ps -a --filter "name=jarvis-gpt-dispatcher" --format "{{json .}}" 2>$null |
      Select-Object -First 1
    if (-not $line) {
      return @{
        docker_available = $true
        exists = $false
        running = $false
        state = "missing"
        status = "container not found"
        image = ""
      }
    }
    $container = $line | ConvertFrom-Json
    $state = [string]$container.State
    $command = @()
    try {
      $commandJson = & $docker.Source inspect jarvis-gpt-dispatcher --format "{{json .Config.Cmd}}" 2>$null
      if ($commandJson) {
        $parsedCommand = $commandJson | ConvertFrom-Json
        $command = @($parsedCommand | ForEach-Object { [string]$_ })
      }
    } catch {
      $command = @()
    }
    return @{
      docker_available = $true
      exists = $true
      running = $state -eq "running"
      state = $state
      status = [string]$container.Status
      image = [string]$container.Image
      id = [string]$container.ID
      command = $command
      runtime = Get-DispatcherContainerRuntime -Command $command
    }
  } catch {
    return @{
      docker_available = $true
      exists = $false
      running = $false
      state = "error"
      status = $_.Exception.Message
      image = ""
    }
  }
}

function Get-DispatcherLogSignals {
  $docker = Get-Command docker -ErrorAction SilentlyContinue
  if (-not $docker) {
    return @()
  }
  try {
    $lines = @(& $docker.Source logs --tail 120 jarvis-gpt-dispatcher 2>&1)
    $signals = $lines | Where-Object {
      $_ -match "(?i)(error|traceback|out of memory|ready|running|startup|loading|loaded|engine|model|cuda|graph|kv cache|served|uvicorn|api server)"
    } | Select-Object -Last 8
    if ($signals.Count -gt 0) {
      return @($signals)
    }
    return @($lines | Select-Object -Last 6)
  } catch {
    return @($_.Exception.Message)
  }
}

function Get-DispatcherFlagValue {
  param(
    [string[]]$Command,
    [string]$Name
  )

  for ($i = 0; $i -lt $Command.Count; $i++) {
    $token = [string]$Command[$i]
    if ($token -eq "--$Name" -and ($i + 1) -lt $Command.Count) {
      return [string]$Command[$i + 1]
    }
    if ($token.StartsWith("--$Name=")) {
      return $token.Substring($Name.Length + 3)
    }
  }
  return ""
}

function Get-ModelIdFromPath {
  param([string]$Path)
  if (-not $Path) {
    return ""
  }
  $normalized = $Path.Replace("\", "/").TrimEnd("/")
  $parts = $normalized.Split("/")
  return [string]$parts[$parts.Count - 1]
}

function Get-DispatcherContainerRuntime {
  param([string[]]$Command)
  if (-not $Command -or $Command.Count -eq 0) {
    return @{}
  }
  if ($Command.Count -eq 1 -and $Command[0] -match "\s--") {
    $Command = @($Command[0] -split "\s+" | Where-Object { $_ })
  }
  $modelPath = Get-DispatcherFlagValue -Command $Command -Name "model"
  return @{
    model_path = $modelPath
    model_id = Get-ModelIdFromPath -Path $modelPath
    served_model_name = Get-DispatcherFlagValue -Command $Command -Name "served-model-name"
    enforce_eager = [bool]($Command -contains "--enforce-eager")
    max_model_len = Get-DispatcherFlagValue -Command $Command -Name "max-model-len"
    gpu_memory_utilization = Get-DispatcherFlagValue -Command $Command -Name "gpu-memory-utilization"
    kv_cache_dtype = Get-DispatcherFlagValue -Command $Command -Name "kv-cache-dtype"
    max_num_seqs = Get-DispatcherFlagValue -Command $Command -Name "max-num-seqs"
    cpu_offload_gb = Get-DispatcherFlagValue -Command $Command -Name "cpu-offload-gb"
    kv_offloading_gb = Get-DispatcherFlagValue -Command $Command -Name "kv-offloading-size"
    kv_offloading_backend = Get-DispatcherFlagValue -Command $Command -Name "kv-offloading-backend"
    language_model_only = [bool]($Command -contains "--language-model-only")
    skip_mm_profiling = [bool]($Command -contains "--skip-mm-profiling")
    mm_processor_cache_gb = Get-DispatcherFlagValue -Command $Command -Name "mm-processor-cache-gb"
    max_num_batched_tokens = Get-DispatcherFlagValue -Command $Command -Name "max-num-batched-tokens"
  }
}

function Get-LlmReadiness {
  Set-JarvisEnvironment -SelectedProfile $Profile

  $container = Get-DispatcherContainerSnapshot
  $portOpen = Test-PortOpen -Port 8001
  $health = Invoke-HttpProbe -Uri "http://127.0.0.1:8001/health" -TimeoutSec 2
  $models = Invoke-HttpProbe -Uri "http://127.0.0.1:8001/v1/models" -TimeoutSec 4 -Json
  $servedModels = @()
  if ($models.ok -and $models.data -and $models.data.data) {
    $servedModels = @($models.data.data | ForEach-Object { [string]$_.id })
  }

  $phase = "offline"
  $ready = $false
  if ($servedModels.Count -gt 0 -and -not $container.running) {
    $phase = "external-ready"
    $ready = $true
  } elseif (-not $container.docker_available) {
    $phase = "docker-missing"
  } elseif (-not $container.exists) {
    $phase = "container-missing"
  } elseif ($container.state -match "(?i)restarting") {
    $phase = "restarting"
  } elseif (-not $container.running) {
    $phase = "container-stopped"
  } elseif ($servedModels.Count -gt 0) {
    $phase = "ready"
    $ready = $true
  } elseif ($portOpen -and $health.ok) {
    $phase = "http-warming"
  } elseif ($portOpen) {
    $phase = "port-open-loading"
  } else {
    $phase = "loading"
  }

  $cert = Get-ProfileCertification -SelectedProfile $Profile
  $deadlineSec = [int]$cert.readiness_deadline_sec
  $unhealthy = $false
  $unhealthyReason = ""
  if ($ready) {
    # Cyclic/repeated-token probe: short deterministic completion.
    $probe = Invoke-ProfileHealthProbe
    if (-not $probe.ok) {
      $ready = $false
      $unhealthy = $true
      $unhealthyReason = [string]$probe.error
      $phase = "unhealthy"
    }
  }

  return [ordered]@{
    ready = $ready
    phase = $phase
    profile = $Profile
    certification = [string]$cert.certification
    certification_reason = [string]$cert.certification_reason
    readiness_deadline_sec = $deadlineSec
    interactive_certified = [bool]$cert.interactive_certified
    unhealthy = $unhealthy
    unhealthy_reason = $unhealthyReason
    endpoint = "http://127.0.0.1:8001/v1"
    container = $container
    port_open = $portOpen
    health_ok = [bool]$health.ok
    health_status = $health.status
    health_error = $health.error
    models_ok = [bool]$models.ok
    models_status = $models.status
    models_error = $models.error
    served_models = $servedModels
    log_signals = Get-DispatcherLogSignals
    checked_at = (Get-Date).ToString("HH:mm:ss")
  }
}

function Invoke-ProfileHealthProbe {
  # Bounded health probe: reject empty/cyclic repeated-token degeneration.
  try {
    $body = @{
      model = "dispatcher"
      temperature = 0
      max_tokens = 16
      messages = @(@{ role = "user"; content = "Reply with only the digit 4. What is 2+2?" })
    } | ConvertTo-Json -Depth 5
    $response = Invoke-RestMethod `
      -Method Post `
      -Uri "http://127.0.0.1:8001/v1/chat/completions" `
      -ContentType "application/json" `
      -Body $body `
      -TimeoutSec 20
    $content = [string]$response.choices[0].message.content
    if ([string]::IsNullOrWhiteSpace($content)) {
      return @{ ok = $false; error = "health probe returned empty content" }
    }
    if ($content -match '(.)\1{11,}' -or $content -match '(\b\w+\b)(?:\s+\1){8,}') {
      return @{ ok = $false; error = "health probe detected repeated-token degeneration" }
    }
    return @{ ok = $true; error = ""; content = $content }
  } catch {
    # Endpoint may still be warming; do not mark ready profiles unhealthy solely
    # because the probe timed out once - caller uses phase/deadline for that.
    return @{ ok = $true; error = ""; skipped = $true; detail = $_.Exception.Message }
  }
}

function Get-DispatcherDesiredStatus {
  Push-Location $RepoRoot
  try {
    $output = @(
      & py.exe -3.11 .\jarvis.py --profile $Profile dispatcher-status 2>$null
    )
    if ($LASTEXITCODE -ne 0) {
      throw "dispatcher-status exited with code $LASTEXITCODE"
    }
    return (($output -join "`n") | ConvertFrom-Json)
  } finally {
    Pop-Location
  }
}

function Set-DispatcherComposeModelPath {
  $status = Get-DispatcherDesiredStatus
  $modelPath = [string]$status.desired_runtime.model_path
  if ([string]::IsNullOrWhiteSpace($modelPath)) {
    throw "Dispatcher desired model path is unavailable."
  }
  $env:JARVIS_QWEN_MODEL_PATH = $modelPath
}

function Write-LlmReadinessBlock {
  param([hashtable]$Readiness)

  $phase = [string]$Readiness.phase
  $color = switch ($phase) {
    "ready" { "Green" }
    "external-ready" { "Green" }
    "http-warming" { "Yellow" }
    "port-open-loading" { "Yellow" }
    "loading" { "Yellow" }
    "restarting" { "Red" }
    "container-stopped" { "Red" }
    default { "DarkYellow" }
  }
  $readyText = if ($Readiness.ready) { "READY" } else { "NOT READY" }

  Write-Host "+--------------------------------------------------------------+" -ForegroundColor DarkCyan
  Write-Host "|                         LLM READINESS                        |" -ForegroundColor Cyan
  Write-Host "+--------------------------------------------------------------+" -ForegroundColor DarkCyan
  Write-Host ("| State:   {0,-10} Phase: {1,-28} At: {2}" -f $readyText, $phase, $Readiness.checked_at) -ForegroundColor $color
  Write-Host ("| Profile: {0,-18} Endpoint: {1}" -f $Readiness.profile, $Readiness.endpoint)
  $runtime = $Readiness.container["runtime"]
  if ($runtime -and $runtime["model_id"]) {
    $mode = if ($runtime["enforce_eager"]) { "eager" } else { "cuda-graph" }
    Write-Host ("| Runtime: {0,-28} Mode: {1}  Ctx: {2}" -f $runtime["model_id"], $mode, $runtime["max_model_len"])
    Write-Host ("| vLLM:    gpu-util {0,-6} kv {1,-5} seqs {2}" -f $runtime["gpu_memory_utilization"], $runtime["kv_cache_dtype"], $runtime["max_num_seqs"])
    if ($runtime["cpu_offload_gb"] -or $runtime["kv_offloading_gb"]) {
      Write-Host ("| Offload: cpu {0} GB  kv {1} GB ({2})" -f $runtime["cpu_offload_gb"], $runtime["kv_offloading_gb"], $(if ($runtime["kv_offloading_backend"]) { $runtime["kv_offloading_backend"] } else { "native" }))
    }
    if ($runtime["language_model_only"]) {
      Write-Host ("| Text:    LM-only  batch-tokens {0}  MM cache {1} GB" -f $runtime["max_num_batched_tokens"], $runtime["mm_processor_cache_gb"])
    }
  }
  Write-Host ("| Docker:  {0,-10} Container: {1}" -f $Readiness.container.state, $Readiness.container.status)
  Write-Host ("| Port:    {0,-10} /health: {1,-8} /v1/models: {2}" -f $Readiness.port_open, $Readiness.health_ok, $Readiness.models_ok)
  if ($Readiness.served_models.Count -gt 0) {
    Write-Host ("| Served:  {0}" -f ($Readiness.served_models -join ", ")) -ForegroundColor Green
  } elseif ($Readiness.models_error) {
    Write-Host ("| Models:  {0}" -f $Readiness.models_error) -ForegroundColor DarkYellow
  }
  Write-Host "+--------------------------------------------------------------+" -ForegroundColor DarkCyan
  if ($Readiness.log_signals.Count -gt 0) {
    Write-Host "Recent dispatcher load signals:" -ForegroundColor DarkGray
    foreach ($line in $Readiness.log_signals) {
      $text = ([string]$line).Trim()
      if ($text.Length -gt 150) {
        $text = $text.Substring(0, 150) + "..."
      }
      $lineColor = if ($text -match "(?i)(error|traceback|out of memory|failed)") { "Red" } else { "DarkGray" }
      Write-Host ("  {0}" -f $text) -ForegroundColor $lineColor
    }
  }
}

function Test-WatchExitKey {
  try {
    if (-not $Host.UI.RawUI.KeyAvailable) {
      return $false
    }
    $key = $Host.UI.RawUI.ReadKey("NoEcho,IncludeKeyDown")
    return $key.VirtualKeyCode -in @(27, 81)
  } catch {
    return $false
  }
}

function Show-LlmReadiness {
  param([switch]$Watch)

  if (-not $Watch) {
    Write-Banner
    Write-LlmReadinessBlock -Readiness (Get-LlmReadiness)
    return
  }

  while ($true) {
    Write-Banner
    Write-LlmReadinessBlock -Readiness (Get-LlmReadiness)
    Write-Host ""
    Write-Host "Refreshing every 5 seconds. Press Q or Esc to stop watching." -ForegroundColor DarkGray
    for ($index = 0; $index -lt 5; $index += 1) {
      Start-Sleep -Seconds 1
      if (Test-WatchExitKey) {
        return
      }
    }
  }
}

function Stop-ProcessId {
  param([int]$ProcessId)

  if ($ProcessId -le 0 -or $ProcessId -eq $PID) {
    return
  }
  $process = Get-Process -Id $ProcessId -ErrorAction SilentlyContinue
  if ($process) {
    Stop-Process -Id $ProcessId -Force
  }
}

function Get-ProcessSnapshot {
  try {
    return @(Get-CimInstance Win32_Process -ErrorAction Stop)
  } catch {
    return @()
  }
}

function Get-CurrentProcessFamilyIds {
  $snapshot = Get-ProcessSnapshot
  $ids = New-Object System.Collections.Generic.HashSet[int]
  $current = [int]$PID
  while ($current -gt 0 -and $ids.Add($current)) {
    $parent = $snapshot | Where-Object { [int]$_.ProcessId -eq $current } | Select-Object -First 1
    if (-not $parent -or -not $parent.ParentProcessId) {
      break
    }
    $current = [int]$parent.ParentProcessId
  }
  return @($ids | ForEach-Object { [int]$_ })
}

function Get-DescendantProcessIds {
  param(
    [int]$RootProcessId,
    [array]$Snapshot
  )

  $ids = @()
  $children = @($Snapshot | Where-Object { [int]$_.ParentProcessId -eq $RootProcessId })
  foreach ($child in $children) {
    $childId = [int]$child.ProcessId
    $ids += $childId
    $ids += Get-DescendantProcessIds -RootProcessId $childId -Snapshot $Snapshot
  }
  return $ids
}

function Stop-ProcessTree {
  param(
    [int]$ProcessId,
    [string]$Reason = "matched process",
    [array]$Snapshot = $null,
    [int[]]$ProtectedProcessIds = @()
  )

  if ($ProcessId -le 0 -or $ProtectedProcessIds -contains $ProcessId) {
    return $false
  }
  if (-not $Snapshot) {
    $Snapshot = Get-ProcessSnapshot
  }

  $ids = @($ProcessId)
  $ids += Get-DescendantProcessIds -RootProcessId $ProcessId -Snapshot $Snapshot
  $ids = @($ids | Sort-Object -Unique)
  [array]::Reverse($ids)

  $stopped = $false
  foreach ($id in $ids) {
    if ($ProtectedProcessIds -contains $id) {
      continue
    }
    $process = Get-Process -Id $id -ErrorAction SilentlyContinue
    if ($process) {
      try {
        Stop-Process -Id $id -Force -ErrorAction Stop
        Write-Host ("Stopped pid={0} ({1})" -f $id, $Reason) -ForegroundColor Yellow
        $stopped = $true
      } catch {
        if ($_.Exception.Message -notmatch "Cannot find a process") {
          Write-Host ("Could not stop pid={0}: {1}" -f $id, $_.Exception.Message) -ForegroundColor DarkYellow
        }
      }
    }
  }
  return $stopped
}

function Test-JarvisProcess {
  param([object]$ProcessInfo)

  $text = ("{0} {1}" -f $ProcessInfo.CommandLine, $ProcessInfo.ExecutablePath).ToLowerInvariant()
  if ([string]::IsNullOrWhiteSpace($text)) {
    return $false
  }

  $repoNeedle = $RepoRoot.ToLowerInvariant()
  $frontendNeedle = $FrontendRoot.ToLowerInvariant()

  if ($text.Contains($repoNeedle) -or $text.Contains($frontendNeedle)) {
    return $true
  }

  return $false
}

function Test-ManagedServiceProcess {
  param(
    [object]$ProcessInfo,
    [string]$Service
  )

  if (-not $ProcessInfo) {
    return $false
  }

  $text = ("{0} {1}" -f $ProcessInfo.CommandLine, $ProcessInfo.ExecutablePath).ToLowerInvariant()
  if ([string]::IsNullOrWhiteSpace($text)) {
    return $false
  }

  $repoNeedle = $RepoRoot.ToLowerInvariant()
  $frontendNeedle = $FrontendRoot.ToLowerInvariant()

  switch ($Service) {
    "backend" {
      return (
        $text.Contains($repoNeedle) -and
        $text.Contains("jarvis.py") -and
        $text.Contains("serve")
      )
    }
    "frontend" {
      return $text.Contains($frontendNeedle) -and (
        $text.Contains("next") -or $text.Contains("npm")
      )
    }
    "bridge" {
      return $text.Contains($repoNeedle) -and $text.Contains("windows_rpc_bridge.py")
    }
    default {
      return Test-JarvisProcess -ProcessInfo $ProcessInfo
    }
  }
}

function Invoke-BridgeCapabilitiesProbe {
  param([int]$TimeoutSec = 3)

  if (-not (Test-Path -LiteralPath $BridgeTokenFile)) {
    return @{ ok = $false; status = $null; data = $null; error = "bridge token is missing" }
  }
  try {
    $token = (Get-Content -LiteralPath $BridgeTokenFile -Raw).Trim()
    if ([string]::IsNullOrWhiteSpace($token)) {
      return @{ ok = $false; status = $null; data = $null; error = "bridge token is empty" }
    }
    $body = @{
      action = "capabilities"
      payload = @{}
      timeout_sec = [Math]::Max(1, [Math]::Min(120, $TimeoutSec))
    } | ConvertTo-Json -Depth 4 -Compress
    $data = Invoke-RestMethod `
      -Uri "http://127.0.0.1:8765/action" `
      -Method Post `
      -Headers @{ Authorization = "Bearer $token" } `
      -ContentType "application/json" `
      -Body $body `
      -TimeoutSec $TimeoutSec
    $hasRawExecutionFlag = $null -ne $data.PSObject.Properties["raw_command_execution"]
    $expectedAppPathsSha256 = Get-StringSha256 -Value ([string]$env:JARVIS_BRIDGE_APP_PATHS_JSON)
    $ready = [bool](
      $data.ok -and
      [string]$data.contract -eq "action.v1" -and
      $hasRawExecutionFlag -and
      -not [bool]$data.raw_command_execution -and
      [string]$data.policy_revision -eq $BridgePolicyRevision -and
      [string]$data.app_paths_sha256 -eq $expectedAppPathsSha256
    )
    return @{
      ok = $ready
      status = 200
      data = $data
      error = if ($ready) { "" } else { "capabilities response failed action.v1 validation" }
    }
  } catch {
    $statusCode = $null
    if ($_.Exception.Response) {
      try { $statusCode = [int]$_.Exception.Response.StatusCode } catch { $statusCode = $null }
    }
    return @{ ok = $false; status = $statusCode; data = $null; error = $_.Exception.Message }
  }
}

function Wait-BridgeReady {
  param([int]$TimeoutSec = 15)

  $deadline = (Get-Date).AddSeconds([Math]::Max(1, $TimeoutSec))
  $lastError = "bridge did not become ready"
  while ((Get-Date) -lt $deadline) {
    $health = Invoke-HttpProbe -Uri "http://127.0.0.1:8765/health" -TimeoutSec 2 -Json
    if ($health.ok -and [string]$health.data.contract -eq "action.v1") {
      $capabilities = Invoke-BridgeCapabilitiesProbe -TimeoutSec 3
      if ($capabilities.ok) {
        return @{ ok = $true; health = $health; capabilities = $capabilities; error = "" }
      }
      $lastError = [string]$capabilities.error
    } elseif ($health.error) {
      $lastError = [string]$health.error
    } else {
      $lastError = "health endpoint did not advertise action.v1"
    }
    Start-Sleep -Milliseconds 250
  }
  return @{ ok = $false; health = $null; capabilities = $null; error = $lastError }
}

function Get-LlmStartDecision {
  param(
    [System.Collections.IDictionary]$Readiness,
    $DispatcherStatus
  )

  if (-not $Readiness) {
    return "start"
  }
  $container = $Readiness["container"]
  $containerRunning = [bool]($container -and $container["running"])
  $containerState = if ($container) { [string]$container["state"] } else { "" }
  if ($containerRunning -or $containerState -match "(?i)^(running|restarting)$") {
    if ($DispatcherStatus -and [bool]$DispatcherStatus.runtime_matches_desired) {
      return "reuse"
    }
    return "replace"
  }
  if ([bool]$Readiness["models_ok"]) {
    return "conflict"
  }
  if ([bool]$Readiness["port_open"]) {
    return "conflict"
  }
  return "start"
}

function Test-ManagedPortOwner {
  param(
    [int]$Port,
    [string]$Service
  )

  $ownerPid = Get-PortOwner -Port $Port
  if (-not $ownerPid) {
    return $false
  }
  $processInfo = Get-ProcessSnapshot |
    Where-Object { [int]$_.ProcessId -eq $ownerPid } |
    Select-Object -First 1
  return [bool](Test-ManagedServiceProcess -ProcessInfo $processInfo -Service $Service)
}

function Stop-JarvisProcessesBySignature {
  $snapshot = Get-ProcessSnapshot
  $protected = Get-CurrentProcessFamilyIds
  $matches = @(
    $snapshot |
      Where-Object { [int]$_.ProcessId -gt 0 } |
      Where-Object { $protected -notcontains [int]$_.ProcessId } |
      Where-Object { Test-JarvisProcess -ProcessInfo $_ }
  )

  foreach ($match in $matches) {
    Stop-ProcessTree `
      -ProcessId ([int]$match.ProcessId) `
      -Reason "Jarvis command line" `
      -Snapshot $snapshot `
      -ProtectedProcessIds $protected | Out-Null
  }
}

function Stop-PortOwner {
  param(
    [int]$Port,
    [switch]$SkipDockerEngineOwner,
    [switch]$ManagedOnly,
    [string]$Service = ""
  )

  try {
    $connections = Get-NetTCPConnection -LocalPort $Port -State Listen -ErrorAction Stop
  } catch {
    return
  }
  $snapshot = Get-ProcessSnapshot
  $protected = Get-CurrentProcessFamilyIds
  $connections | Select-Object -ExpandProperty OwningProcess -Unique | ForEach-Object {
    $ownerPid = [int]$_
    $owner = $snapshot | Where-Object { [int]$_.ProcessId -eq $ownerPid } | Select-Object -First 1
    $ownerText = ("{0} {1} {2}" -f $owner.Name, $owner.CommandLine, $owner.ExecutablePath).ToLowerInvariant()
    if ($SkipDockerEngineOwner -and $ownerText -match "(docker desktop|com\.docker\.backend|dockerd|wslhost)") {
      Write-Host ("Skipped Docker engine port owner pid={0} on {1}; dispatcher container is stopped separately." -f $ownerPid, $Port) -ForegroundColor DarkYellow
      return
    }
    if (
      $ManagedOnly -and
      -not (Test-ManagedServiceProcess -ProcessInfo $owner -Service $Service)
    ) {
      Write-Host (
        "Skipped foreign listener pid={0} on port {1}; it is not managed by Jarvis." -f
        $ownerPid,
        $Port
      ) -ForegroundColor DarkYellow
      return
    }
    Stop-ProcessTree `
      -ProcessId $ownerPid `
      -Reason ("port {0}" -f $Port) `
      -Snapshot $snapshot `
      -ProtectedProcessIds $protected | Out-Null
  }
}

function Stop-DispatcherRuntime {
  $docker = Get-Command docker -ErrorAction SilentlyContinue
  if (-not $docker) {
    Write-Host "Docker CLI is not available; dispatcher container control skipped." -ForegroundColor DarkYellow
    return
  }

  $probe = Test-DockerReady
  if (-not $probe.ready) {
    Write-Host ("Docker API is not ready; dispatcher container control skipped: {0}" -f $probe.error) -ForegroundColor DarkYellow
    return
  }

  Write-Host "Stopping dispatcher..." -ForegroundColor Yellow
  try {
    Invoke-JarvisCommand -FilePath "py.exe" -Arguments @("-3.11", ".\jarvis.py", "--profile", $Profile, "dispatcher-down")
  } catch {
    Write-Host $_ -ForegroundColor DarkYellow
  }

  try {
    & $docker.Source rm -f jarvis-gpt-dispatcher 2>$null | Out-Null
  } catch {
    Write-Host $_ -ForegroundColor DarkYellow
  }
  try {
    Push-Location $RepoRoot
    Set-DispatcherComposeModelPath
    & $docker.Source compose --profile llm down --remove-orphans 2>$null | Out-Null
  } catch {
    Write-Host $_ -ForegroundColor DarkYellow
  } finally {
    Pop-Location
  }
}

function Invoke-JarvisCommand {
  param(
    [string]$FilePath,
    [string[]]$Arguments,
    [string]$WorkingDirectory = $RepoRoot
  )

  Push-Location $WorkingDirectory
  try {
    & $FilePath @Arguments
    if ($LASTEXITCODE -ne 0) {
      throw "$FilePath exited with code $LASTEXITCODE"
    }
  } finally {
    Pop-Location
  }
}

function ConvertTo-WindowsCommandLineArgument {
  param([AllowEmptyString()][string]$Argument)

  if ($Argument.Length -gt 0 -and $Argument -notmatch '[\s"&|<>^()%!]') {
    return $Argument
  }
  $builder = [System.Text.StringBuilder]::new()
  [void]$builder.Append('"')
  $backslashes = 0
  foreach ($character in $Argument.ToCharArray()) {
    if ($character -eq '\') {
      $backslashes += 1
      continue
    }
    if ($character -eq '"') {
      if ($backslashes -gt 0) {
        [void]$builder.Append((('\' * ($backslashes * 2)) -join ''))
      }
      [void]$builder.Append('\"')
      $backslashes = 0
      continue
    }
    if ($backslashes -gt 0) {
      [void]$builder.Append((('\' * $backslashes) -join ''))
      $backslashes = 0
    }
    [void]$builder.Append($character)
  }
  if ($backslashes -gt 0) {
    [void]$builder.Append((('\' * ($backslashes * 2)) -join ''))
  }
  [void]$builder.Append('"')
  return $builder.ToString()
}

function Start-ManagedProcess {
  param(
    [string]$Name,
    [string]$FilePath,
    [string[]]$Arguments,
    [string]$WorkingDirectory,
    [string]$Stdout,
    [string]$Stderr
  )

  $startParameters = @{
    FilePath = $FilePath
    WorkingDirectory = $WorkingDirectory
    RedirectStandardOutput = $Stdout
    RedirectStandardError = $Stderr
    WindowStyle = "Hidden"
    PassThru = $true
  }
  if ($Arguments.Count -gt 0) {
    $startParameters.ArgumentList = (
      @($Arguments | ForEach-Object { ConvertTo-WindowsCommandLineArgument -Argument ([string]$_) }) -join " "
    )
  }
  $process = Start-Process @startParameters

  Write-Host ("Started {0} pid={1}" -f $Name, $process.Id) -ForegroundColor Green
  return [int]$process.Id
}

function Save-LauncherState {
  param([hashtable]$Services)

  $state = [ordered]@{
    profile = $Profile
    home = $HomePath
    lan = [bool]$script:LanMode
    lan_ip = if ($script:LanMode) { Get-LanIPv4 } else { $null }
    frontend_url = Get-FrontendUrl
    backend_url = Get-BackendUrl
    backend_environment_sha256 = (Get-BackendEnvironmentSha256)
    frontend_environment_sha256 = (Get-FrontendEnvironmentSha256)
    started_at = (Get-Date).ToString("o")
    services = $Services
  }
  $state | ConvertTo-Json -Depth 8 | Set-Content -Path $StateFile -Encoding UTF8
}

function Read-LauncherState {
  if (-not (Test-Path $StateFile)) {
    return $null
  }
  try {
    return Get-Content -Path $StateFile -Raw | ConvertFrom-Json
  } catch {
    return $null
  }
}

function Test-LauncherOwnsDispatcher {
  param($State)

  if (-not $State -or -not $State.services) {
    return $true
  }
  $entry = $State.services.dispatcher
  if (-not $entry) {
    return $false
  }
  $ownership = $entry.PSObject.Properties["started_by_launcher"]
  if ($ownership) {
    return [bool]$ownership.Value
  }
  return -not ([bool]$entry.reused -or [bool]$entry.skipped)
}

function Test-ReusedDispatcherOwnership {
  param(
    $State,
    [System.Collections.IDictionary]$Readiness
  )

  if (
    -not $State -or
    -not $State.services -or
    -not $State.services.dispatcher -or
    -not $Readiness -or
    -not $Readiness["container"] -or
    -not [bool]$Readiness["container"]["running"] -or
    -not (Test-LauncherOwnsDispatcher -State $State)
  ) {
    return $false
  }
  $previousId = [string]$State.services.dispatcher.container_id
  $currentId = [string]$Readiness["container"]["id"]
  return -not ($previousId -and $currentId -and $previousId -ne $currentId)
}

function Get-LatestWriteTimeUtc {
  param([string[]]$Paths)

  $latest = [DateTime]::MinValue
  foreach ($path in $Paths) {
    if (-not (Test-Path $path)) {
      continue
    }
    $item = Get-Item -LiteralPath $path -Force
    if ($item.PSIsContainer) {
      Get-ChildItem -LiteralPath $path -Recurse -File -Force -ErrorAction SilentlyContinue | ForEach-Object {
        if ($_.LastWriteTimeUtc -gt $latest) {
          $latest = $_.LastWriteTimeUtc
        }
      }
    } elseif ($item.LastWriteTimeUtc -gt $latest) {
      $latest = $item.LastWriteTimeUtc
    }
  }
  return $latest
}

function Get-FrontendBuildState {
  $buildId = Join-Path $FrontendRoot ".next\BUILD_ID"
  if (-not (Test-Path $buildId)) {
    return @{ stale = $true; reason = "missing .next build"; source_at = $null; build_at = $null }
  }

  $sourcePaths = @(
    (Join-Path $FrontendRoot "app"),
    (Join-Path $FrontendRoot "public"),
    (Join-Path $FrontendRoot "package.json"),
    (Join-Path $FrontendRoot "package-lock.json"),
    (Join-Path $FrontendRoot "next.config.mjs"),
    (Join-Path $FrontendRoot "tsconfig.json"),
    (Join-Path $FrontendRoot ".eslintrc.json")
  )
  $envFiles = @(Get-ChildItem -LiteralPath $FrontendRoot -Filter ".env*" -File -Force -ErrorAction SilentlyContinue | ForEach-Object { $_.FullName })
  $sourceStamp = Get-LatestWriteTimeUtc -Paths @($sourcePaths + $envFiles)
  $buildStamp = (Get-Item -LiteralPath $buildId -Force).LastWriteTimeUtc

  if ($sourceStamp -gt $buildStamp.AddSeconds(1)) {
    return @{
      stale = $true
      reason = "frontend sources are newer than .next"
      source_at = $sourceStamp
      build_at = $buildStamp
    }
  }

  return @{
    stale = $false
    reason = "frontend build is current"
    source_at = $sourceStamp
    build_at = $buildStamp
  }
}

function Ensure-FrontendReady {
  $rebuilt = $false

  if (-not (Test-Path (Join-Path $FrontendRoot "node_modules"))) {
    Write-Host "Installing frontend dependencies..." -ForegroundColor Yellow
    Invoke-JarvisCommand -FilePath "npm.cmd" -Arguments @("ci") -WorkingDirectory $FrontendRoot | Out-Host
  }

  if ($DevFrontend) {
    return $false
  }

  $buildState = Get-FrontendBuildState
  if ($BuildFrontend -or $buildState.stale) {
    $reason = if ($BuildFrontend) { "requested by -BuildFrontend" } else { $buildState.reason }
    Write-Host ("Building frontend ({0})..." -f $reason) -ForegroundColor Yellow
    Invoke-JarvisCommand -FilePath "npm.cmd" -Arguments @("run", "build") -WorkingDirectory $FrontendRoot | Out-Host
    $rebuilt = $true
  } else {
    Write-Host "Frontend build is current." -ForegroundColor DarkGray
  }

  return $rebuilt
}

function Test-ManagedJarvisPort {
  param(
    [int]$Port,
    [string]$Service
  )

  return (Test-PortOpen -Port $Port) -and (Test-ManagedPortOwner -Port $Port -Service $Service)
}

function Test-BridgeActionReady {
  if (-not (Test-ManagedJarvisPort -Port 8765 -Service "bridge")) {
    return $false
  }
  $bridgeHealth = Invoke-HttpProbe -Uri "http://127.0.0.1:8765/health" -TimeoutSec 2 -Json
  if (-not ($bridgeHealth.ok -and [string]$bridgeHealth.data.contract -eq "action.v1")) {
    return $false
  }
  $bridgeCapabilities = Invoke-BridgeCapabilitiesProbe -TimeoutSec 3
  return [bool]$bridgeCapabilities.ok
}

function Get-AlreadyRunningStackServices {
  param(
    $PreviousState,
    [bool]$BackendEnvironmentChanged,
    [bool]$FrontendEnvironmentChanged,
    [string]$BackendBindHost,
    [string]$FrontendBindHost
  )

  # Idempotent start: when every required managed service is already healthy and
  # launch environments are unchanged, skip all mutating CLI (init/dispatcher-up)
  # so the live API lease is never contested.
  if ($BackendEnvironmentChanged -or $FrontendEnvironmentChanged) {
    return $null
  }
  if ($script:LanMode -and -not (Test-PortExposedForLan -Port 3000)) {
    return $null
  }

  $services = @{}

  if (-not $NoBackend) {
    if (-not (Test-ManagedJarvisPort -Port 8000 -Service "backend")) {
      return $null
    }
    $services.backend = @{
      port = 8000
      pid = Get-PortOwner -Port 8000
      reused = $true
      host = $BackendBindHost
    }
  }

  if (-not $NoFrontend) {
    if (-not (Test-ManagedJarvisPort -Port 3000 -Service "frontend")) {
      return $null
    }
    $services.frontend = @{
      port = 3000
      pid = Get-PortOwner -Port 3000
      reused = $true
      host = $FrontendBindHost
    }
  }

  if (-not $NoBridge) {
    if (-not (Test-BridgeActionReady)) {
      return $null
    }
    $services.bridge = @{
      port = 8765
      pid = Get-PortOwner -Port 8765
      reused = $true
      contract = "action.v1"
      policy_revision = $BridgePolicyRevision
    }
  }

  if (-not $NoDispatcher) {
    $llmReadiness = Get-LlmReadiness
    $dispatcherStatus = Get-DispatcherDesiredStatus
    $llmStartDecision = Get-LlmStartDecision `
      -Readiness $llmReadiness `
      -DispatcherStatus $dispatcherStatus
    if ($llmStartDecision -ne "reuse") {
      return $null
    }
    $dispatcherOwnershipContinues = Test-ReusedDispatcherOwnership `
      -State $PreviousState `
      -Readiness $llmReadiness
    $services.dispatcher = @{
      profile = $Profile
      docker = [bool]$llmReadiness.container.running
      reused = $true
      started_by_launcher = $dispatcherOwnershipContinues
      container_id = [string]$llmReadiness.container.id
      phase = [string]$llmReadiness.phase
    }
  } else {
    $services.dispatcher = @{
      profile = $Profile
      skipped = "app-without-llm-launch"
      started_by_launcher = $false
    }
  }

  return $services
}

function Start-JarvisStack {
  Ensure-LauncherFolders
  Set-JarvisEnvironment -SelectedProfile $Profile
  $previousState = Read-LauncherState
  $backendEnvironmentSha256 = Get-BackendEnvironmentSha256
  $frontendEnvironmentSha256 = Get-FrontendEnvironmentSha256
  $backendEnvironmentChanged = [bool](
    -not $previousState -or
    [string]$previousState.backend_environment_sha256 -ne $backendEnvironmentSha256
  )
  $frontendEnvironmentChanged = [bool](
    -not $previousState -or
    [string]$previousState.frontend_environment_sha256 -ne $frontendEnvironmentSha256
  )
  Ensure-LanFirewallRules
  $backendBindHost = "127.0.0.1"
  $frontendBindHost = Get-FrontendBindHost

  Write-Banner

  $alreadyRunningServices = Get-AlreadyRunningStackServices `
    -PreviousState $previousState `
    -BackendEnvironmentChanged $backendEnvironmentChanged `
    -FrontendEnvironmentChanged $frontendEnvironmentChanged `
    -BackendBindHost $backendBindHost `
    -FrontendBindHost $frontendBindHost
  if ($null -ne $alreadyRunningServices) {
    Write-Host (
      "Jarvis stack is already running; reporting already-running status without " +
      "mutating CLI verification (avoids API executive-state lease failure)."
    ) -ForegroundColor Green
    Save-LauncherState -Services $alreadyRunningServices
    Show-JarvisStatus
    return
  }

  # API owns primary-runtime.lock while backend is live. Stop a managed backend
  # whose environment changed before any mutating CLI (init/dispatcher-up).
  if (
    -not $NoBackend -and
    $backendEnvironmentChanged -and
    (Test-ManagedJarvisPort -Port 8000 -Service "backend")
  ) {
    Write-Host "Stopping managed backend before mutating init (environment changed)..." -ForegroundColor Yellow
    Stop-PortOwner -Port 8000 -ManagedOnly -Service "backend"
    [void](Wait-PortClosed -Port 8000)
  }

  $backendHoldsLease = (-not $NoBackend) -and (Test-ManagedJarvisPort -Port 8000 -Service "backend")
  if ($backendHoldsLease) {
    Write-Host (
      "Live managed backend already owns executive state; skipping mutating init."
    ) -ForegroundColor DarkYellow
  } else {
    Write-Host "Preparing runtime folders..." -ForegroundColor Yellow
    Invoke-JarvisCommand -FilePath "py.exe" -Arguments @("-3.11", ".\jarvis.py", "--profile", $Profile, "init")
  }

  $services = @{}

  if (-not $NoDispatcher) {
    $llmReadiness = Get-LlmReadiness
    $dispatcherStatus = Get-DispatcherDesiredStatus
    $llmStartDecision = Get-LlmStartDecision `
      -Readiness $llmReadiness `
      -DispatcherStatus $dispatcherStatus
    if ($llmStartDecision -eq "reuse") {
      $runningModel = [string]$llmReadiness.container.runtime.model_id
      $modelText = if ($runningModel) { ", model=$runningModel" } else { "" }
      $dispatcherOwnershipContinues = Test-ReusedDispatcherOwnership `
        -State $previousState `
        -Readiness $llmReadiness
      Write-Host (
        "LLM is already started (phase={0}{1}); dispatcher launch skipped." -f
        $llmReadiness.phase,
        $modelText
      ) -ForegroundColor Green
      $services.dispatcher = @{
        profile = $Profile
        docker = [bool]$llmReadiness.container.running
        reused = $true
        started_by_launcher = $dispatcherOwnershipContinues
        container_id = [string]$llmReadiness.container.id
        phase = [string]$llmReadiness.phase
      }
    } elseif ($llmStartDecision -eq "conflict") {
      throw (
        "TCP 8001 is occupied, but no running Jarvis dispatcher or valid " +
        "OpenAI-compatible /v1/models endpoint was detected. The LLM may still be " +
        "warming; wait for it or free the port before full start."
      )
    } elseif (
      $llmStartDecision -in @("start", "replace") -and
      (Ensure-DockerReady -TimeoutSec $DockerWaitSec)
    ) {
      if ($backendHoldsLease) {
        throw (
          "Cannot start/replace dispatcher while the managed API owns executive " +
          "state. Stop the stack or use a cold start path before dispatcher-up."
        )
      }
      if ($llmStartDecision -eq "replace") {
        $actualModel = [string]$dispatcherStatus.runtime.model_id
        $desiredModel = [string]$dispatcherStatus.desired_runtime.model_id
        Write-Host (
          "Replacing mismatched dispatcher (actual={0}, desired={1})..." -f
          $actualModel,
          $desiredModel
        ) -ForegroundColor Yellow
      }
      Write-Host "Starting dispatcher for $Profile..." -ForegroundColor Yellow
      Invoke-JarvisCommand -FilePath "py.exe" -Arguments @("-3.11", ".\jarvis.py", "--profile", $Profile, "dispatcher-up")
      $startedDispatcher = Get-DispatcherContainerSnapshot
      $services.dispatcher = @{
        profile = $Profile
        docker = $true
        reused = $false
        started_by_launcher = $true
        container_id = [string]$startedDispatcher.id
      }
      Write-Host "LLM readiness monitor: .\jarvis.cmd llm -WatchLlm" -ForegroundColor Cyan
    } else {
      $services.dispatcher = @{
        profile = $Profile
        docker = $true
        skipped = "docker-not-ready"
        started_by_launcher = $false
      }
      Write-Host "Dispatcher was not started because Docker is not ready. Backend and UI will still start." -ForegroundColor Red
    }
  } else {
    $services.dispatcher = @{
      profile = $Profile
      skipped = "app-without-llm-launch"
      started_by_launcher = $false
    }
    Write-Host "App mode: dispatcher launch skipped; any existing LLM remains untouched." -ForegroundColor Cyan
  }

  if (-not $NoBridge) {
    $startBridge = $true
    if (Test-PortOpen -Port 8765) {
      if (-not (Test-ManagedPortOwner -Port 8765 -Service "bridge")) {
        throw "TCP 8765 is occupied by a process not managed by Jarvis. Stop it or choose another port."
      }
      $bridgeHealth = Invoke-HttpProbe -Uri "http://127.0.0.1:8765/health" -TimeoutSec 2 -Json
      $bridgeCapabilities = if (
        $bridgeHealth.ok -and [string]$bridgeHealth.data.contract -eq "action.v1"
      ) {
        Invoke-BridgeCapabilitiesProbe -TimeoutSec 3
      } else {
        @{ ok = $false; error = "health endpoint did not advertise action.v1" }
      }
      if ($bridgeCapabilities.ok) {
        $services.bridge = @{
          port = 8765
          pid = Get-PortOwner -Port 8765
          reused = $true
          contract = "action.v1"
          policy_revision = $BridgePolicyRevision
        }
        $startBridge = $false
        Write-Host "Host bridge action.v1/$BridgePolicyRevision already listening on 127.0.0.1:8765" -ForegroundColor DarkYellow
      } else {
        Write-Host "Restarting stale or unauthenticated host bridge..." -ForegroundColor Yellow
        Stop-PortOwner -Port 8765 -ManagedOnly -Service "bridge"
        if (-not (Wait-PortClosed -Port 8765)) {
          throw "Stale host bridge did not release TCP 8765."
        }
      }
    }
    if ($startBridge) {
      $bridgePid = Start-ManagedProcess `
        -Name "host bridge" `
        -FilePath "py.exe" `
        -Arguments @("-3.11", (Join-Path $RepoRoot "scripts\windows_rpc_bridge.py"), "--host", "127.0.0.1", "--port", "8765", "--token-file", $BridgeTokenFile) `
        -WorkingDirectory $RepoRoot `
        -Stdout (Join-Path $LogDir "host-bridge.out.log") `
        -Stderr (Join-Path $LogDir "host-bridge.err.log")
      $bridgeReady = Wait-BridgeReady -TimeoutSec 15
      if (-not $bridgeReady.ok) {
        $bridgeSnapshot = Get-ProcessSnapshot
        $bridgeProcess = $bridgeSnapshot |
          Where-Object { [int]$_.ProcessId -eq $bridgePid } |
          Select-Object -First 1
        if (Test-ManagedServiceProcess -ProcessInfo $bridgeProcess -Service "bridge") {
          Stop-ProcessTree `
            -ProcessId $bridgePid `
            -Reason "failed authenticated bridge readiness" `
            -Snapshot $bridgeSnapshot `
            -ProtectedProcessIds (Get-CurrentProcessFamilyIds) | Out-Null
        }
        Stop-PortOwner -Port 8765 -ManagedOnly -Service "bridge"
        throw ("Host bridge failed authenticated action.v1 readiness: {0}" -f $bridgeReady.error)
      }
      $services.bridge = @{
        port = 8765
        contract = "action.v1"
        policy_revision = $BridgePolicyRevision
        pid = $bridgePid
        reused = $false
      }
    }
  }

  if (-not $NoBackend) {
    if (Test-PortOpen -Port 8000) {
      if (-not (Test-ManagedPortOwner -Port 8000 -Service "backend")) {
        throw "TCP 8000 is occupied by a process not managed by Jarvis. Stop it or choose another port."
      }
      if ($backendEnvironmentChanged) {
        $reason = "its launch environment changed"
        Write-Host ("Restarting backend because {0}..." -f $reason) -ForegroundColor Yellow
        Stop-PortOwner -Port 8000 -ManagedOnly -Service "backend"
        [void](Wait-PortClosed -Port 8000)
      } else {
        $services.backend = @{ port = 8000; pid = Get-PortOwner -Port 8000; reused = $true; host = $backendBindHost }
        Write-Host ("Backend already listening on {0}:8000" -f $backendBindHost) -ForegroundColor DarkYellow
      }
    }
    if (-not (Test-PortOpen -Port 8000)) {
      $services.backend = @{
        port = 8000
        host = $backendBindHost
        pid = Start-ManagedProcess `
          -Name "backend" `
          -FilePath "py.exe" `
          -Arguments @("-3.11", (Join-Path $RepoRoot "jarvis.py"), "--profile", $Profile, "serve", "--host", $backendBindHost, "--port", "8000") `
          -WorkingDirectory $RepoRoot `
          -Stdout (Join-Path $LogDir "backend.out.log") `
          -Stderr (Join-Path $LogDir "backend.err.log")
      }
    }
  }

  if (-not $NoFrontend) {
    $frontendRebuilt = Ensure-FrontendReady
    if (Test-PortOpen -Port 3000) {
      if (-not (Test-ManagedPortOwner -Port 3000 -Service "frontend")) {
        throw "TCP 3000 is occupied by a process not managed by Jarvis. Stop it or choose another port."
      }
      if (
        $frontendRebuilt -or
        $frontendEnvironmentChanged -or
        ($script:LanMode -and -not (Test-PortExposedForLan -Port 3000))
      ) {
        $reason = if ($frontendRebuilt) {
          "the frontend build changed"
        } elseif ($frontendEnvironmentChanged) {
          "the frontend launch environment changed"
        } else {
          "LAN binding is required"
        }
        Write-Host ("Restarting Command Center because {0}..." -f $reason) -ForegroundColor Yellow
        Stop-PortOwner -Port 3000 -ManagedOnly -Service "frontend"
        [void](Wait-PortClosed -Port 3000)
      } else {
        $services.frontend = @{ port = 3000; pid = Get-PortOwner -Port 3000; reused = $true; host = $frontendBindHost }
        Write-Host ("Command Center already listening on {0}:3000" -f $frontendBindHost) -ForegroundColor DarkYellow
      }
    }

    if (-not (Test-PortOpen -Port 3000)) {
      $frontendArgs = if ($DevFrontend) {
        @("run", "dev", "--", "--hostname", $frontendBindHost)
      } else {
        @("run", "start", "--", "--hostname", $frontendBindHost)
      }
      $services.frontend = @{
        port = 3000
        host = $frontendBindHost
        pid = Start-ManagedProcess `
          -Name "frontend" `
          -FilePath "npm.cmd" `
          -Arguments $frontendArgs `
          -WorkingDirectory $FrontendRoot `
          -Stdout (Join-Path $LogDir "frontend.out.log") `
          -Stderr (Join-Path $LogDir "frontend.err.log")
      }
    }
  }

  Save-LauncherState -Services $services
  Start-Sleep -Seconds 3
  Show-JarvisStatus
}

function Stop-JarvisStack {
  Ensure-LauncherFolders
  Set-JarvisEnvironment -SelectedProfile $Profile
  Write-Banner

  $snapshot = Get-ProcessSnapshot
  $protected = Get-CurrentProcessFamilyIds
  $state = Read-LauncherState
  if ($state -and $state.services) {
    foreach ($service in @("frontend", "backend", "bridge")) {
      $entry = $state.services.$service
      if ($entry -and $entry.pid) {
        $statePid = [int]$entry.pid
        $processInfo = $snapshot |
          Where-Object { [int]$_.ProcessId -eq $statePid } |
          Select-Object -First 1
        if (Test-ManagedServiceProcess -ProcessInfo $processInfo -Service $service) {
          Stop-ProcessTree `
            -ProcessId $statePid `
            -Reason ("{0} state file" -f $service) `
            -Snapshot $snapshot `
            -ProtectedProcessIds $protected | Out-Null
        } else {
          Write-Host (
            "Skipped stale {0} state pid={1}; command line no longer matches Jarvis." -f
            $service,
            $statePid
          ) -ForegroundColor DarkYellow
        }
      }
    }
  }

  Stop-JarvisProcessesBySignature

  Stop-PortOwner -Port 3000 -ManagedOnly -Service "frontend"
  Stop-PortOwner -Port 8000 -ManagedOnly -Service "backend"
  Stop-PortOwner -Port 8765 -ManagedOnly -Service "bridge"

  if (Test-LauncherOwnsDispatcher -State $state) {
    Stop-DispatcherRuntime
    Stop-PortOwner -Port 8001 -SkipDockerEngineOwner
  } else {
    Write-Host "Preserving LLM runtime because this launcher state did not start it." -ForegroundColor Cyan
  }

  if (Test-Path $StateFile) {
    Remove-Item -LiteralPath $StateFile -Force
  }
  Show-JarvisStatus
}

function Show-ServiceRow {
  param(
    [string]$Name,
    [int]$Port,
    [string]$Url = ""
  )

  $owner = Get-PortOwner -Port $Port
  $open = Test-PortOpen -Port $Port
  if ($open) {
    $processText = if ($owner) { "pid $owner" } else { "docker/host" }
    Write-Host ("| {0,-14} | {1,-8} | {2,-11} | {3,-26} |" -f $Name, "online", $processText, $Url) -ForegroundColor Green
  } else {
    Write-Host ("| {0,-14} | {1,-8} | {2,-11} | {3,-26} |" -f $Name, "offline", "-", $Url) -ForegroundColor DarkGray
  }
}

function Get-EffectiveLanMode {
  return $false
}

function Get-EffectivePublicHost {
  return "127.0.0.1"
}

function Show-JarvisStatus {
  Set-JarvisEnvironment -SelectedProfile $Profile
  $statusHost = Get-EffectivePublicHost
  Write-Banner
  Write-Host "+----------------+----------+-------------+----------------------------+" -ForegroundColor DarkCyan
  Write-Host "| Service        | State    | Process     | URL                        |" -ForegroundColor Cyan
  Write-Host "+----------------+----------+-------------+----------------------------+" -ForegroundColor DarkCyan
  Show-ServiceRow -Name "Backend" -Port 8000 -Url "http://127.0.0.1:8000"
  Show-ServiceRow -Name "Frontend" -Port 3000 -Url ("http://{0}:3000" -f $statusHost)
  Show-ServiceRow -Name "Host bridge" -Port 8765 -Url "http://127.0.0.1:8765"
  Show-ServiceRow -Name "Dispatcher" -Port 8001 -Url "http://127.0.0.1:8001/v1"
  Write-Host "+----------------+----------+-------------+----------------------------+" -ForegroundColor DarkCyan
  Write-Host ""
  Write-LlmReadinessBlock -Readiness (Get-LlmReadiness)
  Write-Host ""
  Write-Host ("State file: {0}" -f $StateFile) -ForegroundColor DarkGray
  Write-Host ("Logs:       {0}" -f $LogDir) -ForegroundColor DarkGray
}

function Show-Logs {
  $items = @(
    @{ Label = "Backend log"; Value = Join-Path $LogDir "backend.err.log"; Hint = "backend stderr" },
    @{ Label = "Frontend log"; Value = Join-Path $LogDir "frontend.err.log"; Hint = "frontend stderr" },
    @{ Label = "Host bridge log"; Value = Join-Path $LogDir "host-bridge.err.log"; Hint = "bridge stderr" },
    @{ Label = "Dispatcher logs"; Value = "dispatcher"; Hint = "docker compose logs" }
  )
  $choice = Select-Menu -Title "Select log stream" -Items $items
  if (-not $choice) {
    return
  }
  Write-Banner
  if ($choice.Value -eq "dispatcher") {
    Set-DispatcherComposeModelPath
    Invoke-JarvisCommand -FilePath "docker" -Arguments @("compose", "--profile", "llm", "logs", "--tail", "160", "dispatcher")
    return
  }
  if (Test-Path $choice.Value) {
    Get-Content -Path $choice.Value -Tail 160
  } else {
    Write-Host "Log file does not exist yet: $($choice.Value)" -ForegroundColor DarkYellow
  }
}

function Invoke-Doctor {
  Set-JarvisEnvironment -SelectedProfile $Profile
  # Run doctor in a nested -File process so its explicit exit code is captured
  # without relying on in-process call-operator exit semantics.
  $doctorScript = Join-Path $RepoRoot "scripts\doctor.ps1"
  $doctorArgs = @(
    "-NoProfile",
    "-ExecutionPolicy", "Bypass",
    "-File", $doctorScript
  )
  $proc = Start-Process -FilePath "powershell.exe" `
    -ArgumentList $doctorArgs `
    -WorkingDirectory $RepoRoot `
    -Wait -PassThru -NoNewWindow
  $code = 1
  if ($null -ne $proc -and $null -ne $proc.ExitCode) {
    $code = [int]$proc.ExitCode
  }
  if ($Action -eq "doctor") {
    exit $code
  }
  if ($code -ne 0) {
    Write-Host "Doctor failed with exit code $code" -ForegroundColor Red
  }
  return $code
}

function Open-CommandCenter {
  Start-Process ("http://{0}:3000" -f (Get-EffectivePublicHost))
}

function Invoke-Menu {
  $main = @(
    @{ Label = "Start full stack"; Value = "start"; Hint = "loopback-only: dispatcher + bridge + backend + UI" },
    @{ Label = "Start app without LLM"; Value = "app"; Hint = "bridge + backend + UI; preserve existing LLM" },
    @{ Label = "Stop stack"; Value = "stop"; Hint = "stop services owned by current launcher state" },
    @{ Label = "Restart stack"; Value = "restart"; Hint = "stop then start" },
    @{ Label = "Status"; Value = "status"; Hint = "show service ports" },
    @{ Label = "LLM readiness"; Value = "llm"; Hint = "watch real model startup" },
    @{ Label = "Logs"; Value = "logs"; Hint = "tail local logs" },
    @{ Label = "Doctor"; Value = "doctor"; Hint = "run full smoke checks" },
    @{ Label = "Open UI"; Value = "open"; Hint = "open Command Center" },
    @{ Label = "Exit"; Value = "exit"; Hint = "" }
  )
  $choice = Select-Menu -Title "Main menu" -Items $main
  if (-not $choice -or $choice.Value -eq "exit") {
    return
  }

  if ($choice.Value -in @("start", "app", "restart")) {
    # Normal interactive menu shows only certified profile(s).
    $profiles = @(
      @{
        Label = "Turbo 26B - certified interactive (recommended)"
        Value = "gemma4-turbo"
        Hint  = "certified | gemma4-26b-a4b-nvfp4 | readiness deadline 180s"
      }
    )
    if ([bool]$script:AllowExperimentalProfiles) {
      $profiles += @(
        @{
          Label = "Mono 31B perf - EXPERIMENTAL research-only"
          Value = "gemma4-mono-perf"
          Hint  = "experimental | not interactive-certified | deadline 900s | requires confirmation"
        },
        @{
          Label = "Mono 31B offload - UNSUPPORTED interactive / research-only"
          Value = "gemma4-mono"
          Hint  = "unsupported interactive | research-only | deadline 1200s | requires confirmation"
        }
      )
    }
    $profileChoice = Select-Menu -Title "Select LLM profile (certified interactive only unless advanced opt-in)" -Items $profiles
    if (-not $profileChoice) {
      return
    }
    $script:Profile = $profileChoice.Value
    if ($script:Profile -ne "gemma4-turbo") {
      if (-not $IUnderstandExperimentalProfile) {
        $confirm = Select-Menu -Title "Confirm experimental/unsupported profile" -Items @(
          @{ Label = "Cancel - use certified turbo"; Value = "cancel"; Hint = "recommended" },
          @{ Label = "I understand this profile is experimental/research-only"; Value = "confirm"; Hint = "advanced opt-in" }
        )
        if (-not $confirm -or $confirm.Value -ne "confirm") {
          $script:Profile = "gemma4-turbo"
        } else {
          $script:IUnderstandExperimentalProfile = $true
          $script:AllowExperimentalProfiles = $true
        }
      }
    }

    if ($choice.Value -eq "app") {
      $script:NoDispatcher = $true
    } else {
      $presets = @(
        @{ Label = "Full stack"; Value = "full"; Hint = "recommended" },
        @{ Label = "App only"; Value = "app"; Hint = "bridge + backend + UI, no dispatcher" },
        @{ Label = "Backend only"; Value = "backend"; Hint = "API only" },
        @{ Label = "Dispatcher only"; Value = "dispatcher"; Hint = "LLM container only" }
      )
      $presetChoice = Select-Menu -Title "Select startup preset" -Items $presets
      if (-not $presetChoice) {
        return
      }

      $script:NoDispatcher = $presetChoice.Value -in @("app", "backend")
      $script:NoBridge = $presetChoice.Value -eq "dispatcher"
      $script:NoBackend = $presetChoice.Value -eq "dispatcher"
      $script:NoFrontend = $presetChoice.Value -in @("backend", "dispatcher")
    }
  }

  switch ($choice.Value) {
    "start" { Start-JarvisStack }
    "app" { $script:NoDispatcher = $true; Start-JarvisStack }
    "stop" { Stop-JarvisStack }
    "restart" { Stop-JarvisStack; Start-JarvisStack }
    "status" { Show-JarvisStatus }
    "llm" { Show-LlmReadiness -Watch }
    "logs" { Show-Logs }
    "doctor" { Invoke-Doctor }
    "open" { Open-CommandCenter }
  }

  Write-Host ""
  Write-Host "Press any key to return to menu..." -ForegroundColor DarkGray
  [void]$Host.UI.RawUI.ReadKey("NoEcho,IncludeKeyDown")
  Invoke-Menu
}

switch ($Action) {
  "menu" { Invoke-Menu }
  "start" { Start-JarvisStack }
  "app" { $script:NoDispatcher = $true; Start-JarvisStack }
  "stop" { Stop-JarvisStack }
  "restart" { Stop-JarvisStack; Start-JarvisStack }
  "status" { Show-JarvisStatus }
  "llm" { Show-LlmReadiness -Watch:$WatchLlm }
  "logs" { Show-Logs }
  "doctor" { Invoke-Doctor }
  "open" { Open-CommandCenter }
}
