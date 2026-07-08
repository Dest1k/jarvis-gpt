param(
  [Parameter(Position = 0)]
  [ValidateSet("menu", "start", "stop", "restart", "status", "llm", "logs", "doctor", "open")]
  [string]$Action = "menu",

  [ValidateSet("gemma4-turbo", "gemma4-mono")]
  [string]$Profile = "gemma4-turbo",

  [string]$HomePath = "D:\jarvis",
  [string]$ModelRoot = "D:\jarvis\data\models",

  [switch]$NoDispatcher,
  [switch]$NoBackend,
  [switch]$NoFrontend,
  [switch]$NoBridge,
  [switch]$WatchLlm,
  [switch]$BuildFrontend,
  [switch]$DevFrontend
)

$ErrorActionPreference = "Stop"

$RepoRoot = (Resolve-Path (Join-Path $PSScriptRoot "..")).Path
$FrontendRoot = Join-Path $RepoRoot "frontend"
$LogDir = Join-Path $HomePath "logs\jarvis-gpt"
$StateDir = Join-Path $HomePath "data\jarvis-gpt\state"
$StateFile = Join-Path $StateDir "launcher-state.json"
$BridgeTokenFile = Join-Path $HomePath ".jarvis\bridge.token"

function Set-JarvisEnvironment {
  param([string]$SelectedProfile)

  $env:JARVIS_HOME = $HomePath
  $env:JARVIS_MODEL_ROOT = $ModelRoot
  $env:JARVIS_PROFILE = $SelectedProfile
  $env:JARVIS_LLM_BASE_URL = "http://localhost:8001/v1"
  $env:JARVIS_LLM_MODEL = "dispatcher"
  $env:NEXT_PUBLIC_JARVIS_API_URL = "http://localhost:8000"
}

function Ensure-LauncherFolders {
  New-Item -ItemType Directory -Force -Path $LogDir, $StateDir | Out-Null
}

function Write-Banner {
  Clear-Host
  Write-Host "+--------------------------------------------------------------+" -ForegroundColor DarkCyan
  Write-Host "|                       JARVIS GPT LAUNCHER                    |" -ForegroundColor Cyan
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
    return @{
      docker_available = $true
      exists = $true
      running = $state -eq "running"
      state = $state
      status = [string]$container.Status
      image = [string]$container.Image
      id = [string]$container.ID
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
  if (-not $container.docker_available) {
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

  return [ordered]@{
    ready = $ready
    phase = $phase
    profile = $Profile
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

function Write-LlmReadinessBlock {
  param([hashtable]$Readiness)

  $phase = [string]$Readiness.phase
  $color = switch ($phase) {
    "ready" { "Green" }
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

function Stop-PortOwner {
  param([int]$Port)

  try {
    $connections = Get-NetTCPConnection -LocalPort $Port -State Listen -ErrorAction Stop
  } catch {
    return
  }
  $connections | Select-Object -ExpandProperty OwningProcess -Unique | ForEach-Object {
    Stop-ProcessId -ProcessId ([int]$_)
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

function Start-ManagedProcess {
  param(
    [string]$Name,
    [string]$FilePath,
    [string[]]$Arguments,
    [string]$WorkingDirectory,
    [string]$Stdout,
    [string]$Stderr
  )

  $process = Start-Process `
    -FilePath $FilePath `
    -ArgumentList $Arguments `
    -WorkingDirectory $WorkingDirectory `
    -RedirectStandardOutput $Stdout `
    -RedirectStandardError $Stderr `
    -WindowStyle Hidden `
    -PassThru

  Write-Host ("Started {0} pid={1}" -f $Name, $process.Id) -ForegroundColor Green
  return [int]$process.Id
}

function Save-LauncherState {
  param([hashtable]$Services)

  $state = [ordered]@{
    profile = $Profile
    home = $HomePath
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

function Ensure-FrontendReady {
  if (-not (Test-Path (Join-Path $FrontendRoot "node_modules"))) {
    Write-Host "Installing frontend dependencies..." -ForegroundColor Yellow
    Invoke-JarvisCommand -FilePath "npm.cmd" -Arguments @("ci") -WorkingDirectory $FrontendRoot
  }

  if ($BuildFrontend -or -not (Test-Path (Join-Path $FrontendRoot ".next"))) {
    Write-Host "Building frontend..." -ForegroundColor Yellow
    Invoke-JarvisCommand -FilePath "npm.cmd" -Arguments @("run", "build") -WorkingDirectory $FrontendRoot
  }
}

function Start-JarvisStack {
  Ensure-LauncherFolders
  Set-JarvisEnvironment -SelectedProfile $Profile

  Write-Banner
  Write-Host "Preparing runtime folders..." -ForegroundColor Yellow
  Invoke-JarvisCommand -FilePath "py.exe" -Arguments @("-3.11", ".\jarvis.py", "--profile", $Profile, "init")

  $services = @{}

  if (-not $NoDispatcher) {
    Write-Host "Starting dispatcher for $Profile..." -ForegroundColor Yellow
    Invoke-JarvisCommand -FilePath "py.exe" -Arguments @("-3.11", ".\jarvis.py", "--profile", $Profile, "dispatcher-up")
    $services.dispatcher = @{ profile = $Profile; docker = $true }
    Write-Host "LLM readiness monitor: .\jarvis.cmd llm -WatchLlm" -ForegroundColor Cyan
  }

  if (-not $NoBridge) {
    if (Test-PortOpen -Port 8765) {
      $services.bridge = @{ port = 8765; pid = Get-PortOwner -Port 8765; reused = $true }
      Write-Host "Host bridge already listening on 127.0.0.1:8765" -ForegroundColor DarkYellow
    } else {
      $services.bridge = @{
        port = 8765
        pid = Start-ManagedProcess `
          -Name "host bridge" `
          -FilePath "py.exe" `
          -Arguments @("-3.11", ".\scripts\windows_rpc_bridge.py", "--host", "127.0.0.1", "--port", "8765", "--token-file", $BridgeTokenFile) `
          -WorkingDirectory $RepoRoot `
          -Stdout (Join-Path $LogDir "host-bridge.out.log") `
          -Stderr (Join-Path $LogDir "host-bridge.err.log")
      }
    }
  }

  if (-not $NoBackend) {
    if (Test-PortOpen -Port 8000) {
      $services.backend = @{ port = 8000; pid = Get-PortOwner -Port 8000; reused = $true }
      Write-Host "Backend already listening on 127.0.0.1:8000" -ForegroundColor DarkYellow
    } else {
      $services.backend = @{
        port = 8000
        pid = Start-ManagedProcess `
          -Name "backend" `
          -FilePath "py.exe" `
          -Arguments @("-3.11", ".\jarvis.py", "--profile", $Profile, "serve", "--host", "127.0.0.1", "--port", "8000") `
          -WorkingDirectory $RepoRoot `
          -Stdout (Join-Path $LogDir "backend.out.log") `
          -Stderr (Join-Path $LogDir "backend.err.log")
      }
    }
  }

  if (-not $NoFrontend) {
    Ensure-FrontendReady
    if (Test-PortOpen -Port 3000) {
      $services.frontend = @{ port = 3000; pid = Get-PortOwner -Port 3000; reused = $true }
      Write-Host "Command Center already listening on 127.0.0.1:3000" -ForegroundColor DarkYellow
    } else {
      $frontendArgs = if ($DevFrontend) {
        @("run", "dev", "--", "--hostname", "127.0.0.1")
      } else {
        @("run", "start", "--", "--hostname", "127.0.0.1")
      }
      $services.frontend = @{
        port = 3000
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

  $state = Read-LauncherState
  if ($state -and $state.services) {
    foreach ($service in @("frontend", "backend", "bridge")) {
      $entry = $state.services.$service
      if ($entry -and $entry.pid) {
        Stop-ProcessId -ProcessId ([int]$entry.pid)
        Write-Host ("Stopped {0} pid={1}" -f $service, $entry.pid) -ForegroundColor Yellow
      }
    }
  }

  Stop-PortOwner -Port 3000
  Stop-PortOwner -Port 8000
  Stop-PortOwner -Port 8765

  if (-not $NoDispatcher) {
    Write-Host "Stopping dispatcher..." -ForegroundColor Yellow
    try {
      Invoke-JarvisCommand -FilePath "py.exe" -Arguments @("-3.11", ".\jarvis.py", "--profile", $Profile, "dispatcher-down")
    } catch {
      Write-Host $_ -ForegroundColor DarkYellow
    }
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

function Show-JarvisStatus {
  Set-JarvisEnvironment -SelectedProfile $Profile
  Write-Banner
  Write-Host "+----------------+----------+-------------+----------------------------+" -ForegroundColor DarkCyan
  Write-Host "| Service        | State    | Process     | URL                        |" -ForegroundColor Cyan
  Write-Host "+----------------+----------+-------------+----------------------------+" -ForegroundColor DarkCyan
  Show-ServiceRow -Name "Backend" -Port 8000 -Url "http://127.0.0.1:8000"
  Show-ServiceRow -Name "Frontend" -Port 3000 -Url "http://127.0.0.1:3000"
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
  & (Join-Path $RepoRoot "scripts\doctor.ps1")
}

function Open-CommandCenter {
  Start-Process "http://127.0.0.1:3000"
}

function Invoke-Menu {
  $main = @(
    @{ Label = "Start full stack"; Value = "start"; Hint = "dispatcher + bridge + backend + UI" },
    @{ Label = "Stop stack"; Value = "stop"; Hint = "stop UI/backend/bridge/dispatcher" },
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

  if ($choice.Value -in @("start", "restart")) {
    $profiles = @(
      @{ Label = "gemma4-turbo"; Value = "gemma4-turbo"; Hint = "26B A4B NVFP4, fast warmed runtime" },
      @{ Label = "gemma4-mono"; Value = "gemma4-mono"; Hint = "31B IT NVFP4, stable baseline" }
    )
    $profileChoice = Select-Menu -Title "Select LLM profile" -Items $profiles
    if (-not $profileChoice) {
      return
    }
    $script:Profile = $profileChoice.Value

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

  switch ($choice.Value) {
    "start" { Start-JarvisStack }
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
  "stop" { Stop-JarvisStack }
  "restart" { Stop-JarvisStack; Start-JarvisStack }
  "status" { Show-JarvisStatus }
  "llm" { Show-LlmReadiness -Watch:$WatchLlm }
  "logs" { Show-Logs }
  "doctor" { Invoke-Doctor }
  "open" { Open-CommandCenter }
}
