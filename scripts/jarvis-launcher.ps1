param(
  [Parameter(Position = 0)]
  [ValidateSet("menu", "start", "app", "stop", "restart", "status", "llm", "logs", "doctor", "open")]
  [string]$Action = "menu",

  [string]$Profile = "",

  # Explicit advanced/CLI opt-in for experimental or unsupported research profiles.
  [switch]$AllowExperimentalProfiles,

  [switch]$IUnderstandExperimentalProfile,

  [string]$HomePath = "D:\jarvis",
  [string]$ModelRoot = "D:\jarvis\data\models",

  [switch]$NoDispatcher,
  [switch]$NoBackend,
  [switch]$NoFrontend,
  [switch]$NoBridge,
  [switch]$NoTelegram,
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
$script:ProfileCatalog = $null

$RepoRoot = (Resolve-Path (Join-Path $PSScriptRoot "..")).Path
$FrontendRoot = Join-Path $RepoRoot "frontend"
$LogDir = Join-Path $HomePath "logs\jarvis-gpt"
$StateDir = Join-Path $HomePath "data\jarvis-gpt\state"
$StateFile = Join-Path $StateDir "launcher-state.json"
$DispatcherOwnershipJournal = Join-Path $StateDir "dispatcher-ownership-journal.json"
$BridgeTokenFile = Join-Path $HomePath ".jarvis\bridge.token"
$TelegramBridgeTokenFile = Join-Path $HomePath ".jarvis\telegram-bridge.token"
$TelegramLegacyStore = Join-Path $StateDir "telegram_bridge.sqlite3"
$BridgePolicyRevision = "native-app-v3"
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

function Protect-TelegramBridgeTokenFile {
  param([string]$Path = $TelegramBridgeTokenFile)

  try {
    $identity = [System.Security.Principal.WindowsIdentity]::GetCurrent().User.Value
    $icacls = Join-Path $env:SystemRoot "System32\icacls.exe"
    foreach ($arguments in @(
      @($Path, "/reset", "/Q"),
      @($Path, "/inheritance:r", "/Q"),
      @($Path, "/grant:r", "*$($identity):(F)", "/Q")
    )) {
      & $icacls @arguments | Out-Null
      if ($LASTEXITCODE -ne 0) {
        throw "icacls exited with code $LASTEXITCODE"
      }
    }
  } catch {
    throw "Could not restrict Telegram bridge token file permissions: $($_.Exception.Message)"
  }
}

function New-RandomBase64UrlToken {
  $bytes = New-Object byte[] 32
  $rng = [System.Security.Cryptography.RandomNumberGenerator]::Create()
  try {
    $rng.GetBytes($bytes)
  } finally {
    $rng.Dispose()
  }
  return [Convert]::ToBase64String($bytes).TrimEnd([char[]]"=").Replace("+", "-").Replace("/", "_")
}

function Get-OrCreateTelegramBridgeToken {
  if (Test-Path -LiteralPath $TelegramBridgeTokenFile) {
    $stored = (Get-Content -LiteralPath $TelegramBridgeTokenFile -Raw).Trim()
    if ($stored.Length -lt 32) {
      throw "Telegram bridge token file is empty or truncated: $TelegramBridgeTokenFile"
    }
    Protect-TelegramBridgeTokenFile
    return $stored
  }

  $tokenDir = Split-Path -Parent $TelegramBridgeTokenFile
  New-Item -ItemType Directory -Force -Path $tokenDir | Out-Null
  $candidate = New-RandomBase64UrlToken
  $temporary = Join-Path $tokenDir (".{0}.{1}.{2}.tmp" -f ([IO.Path]::GetFileName($TelegramBridgeTokenFile)), $PID, [guid]::NewGuid().ToString("N"))
  try {
    $encoding = [System.Text.Encoding]::ASCII
    $payload = $encoding.GetBytes($candidate)
    $stream = [System.IO.File]::Open(
      $temporary,
      [System.IO.FileMode]::CreateNew,
      [System.IO.FileAccess]::Write,
      [System.IO.FileShare]::None
    )
    try {
      $stream.Write($payload, 0, $payload.Length)
      $stream.Flush($true)
    } finally {
      $stream.Dispose()
    }
    Protect-TelegramBridgeTokenFile -Path $temporary
    try {
      # Same-directory move is atomic. If another launcher won the race, its protected
      # generate-once value remains authoritative and is read below.
      [System.IO.File]::Move($temporary, $TelegramBridgeTokenFile)
    } catch {
      if (-not (Test-Path -LiteralPath $TelegramBridgeTokenFile)) {
        throw
      }
    }
  } finally {
    if (Test-Path -LiteralPath $temporary) {
      Remove-Item -LiteralPath $temporary -Force
    }
  }

  Protect-TelegramBridgeTokenFile
  $stored = (Get-Content -LiteralPath $TelegramBridgeTokenFile -Raw).Trim()
  if ($stored.Length -lt 32) {
    throw "Telegram bridge token file is empty or truncated: $TelegramBridgeTokenFile"
  }
  return $stored
}

function Get-ConfiguredEnvironmentValue {
  param([string]$Name)

  $processValue = [Environment]::GetEnvironmentVariable($Name, "Process")
  if ($null -ne $processValue) {
    return [string]$processValue
  }
  $envFile = if ($env:JARVIS_ENV_FILE) {
    [string]$env:JARVIS_ENV_FILE
  } else {
    Join-Path $RepoRoot "backend\.env.local"
  }
  if (-not (Test-Path -LiteralPath $envFile)) {
    return ""
  }
  foreach ($rawLine in Get-Content -LiteralPath $envFile -Encoding UTF8) {
    $line = $rawLine.Trim()
    if (-not $line -or $line.StartsWith("#")) {
      continue
    }
    if ($line.StartsWith("export ")) {
      $line = $line.Substring(7).Trim()
    }
    $separator = $line.IndexOf("=")
    if ($separator -lt 1 -or $line.Substring(0, $separator).Trim() -ne $Name) {
      continue
    }
    $value = $line.Substring($separator + 1).Trim()
    if ($value.Length -ge 2 -and (
      ($value.StartsWith('"') -and $value.EndsWith('"')) -or
      ($value.StartsWith("'") -and $value.EndsWith("'"))
    )) {
      $value = $value.Substring(1, $value.Length - 2)
    }
    return $value
  }
  return ""
}

function Initialize-TelegramBridgeEnvironment {
  param([string]$ApiToken)

  $bridgeSecret = Get-OrCreateTelegramBridgeToken
  if ($bridgeSecret -eq $ApiToken) {
    throw "Telegram bridge token must be distinct from the API token."
  }
  $env:JARVIS_TELEGRAM_BRIDGE_SECRET = $bridgeSecret

  $botToken = (Get-ConfiguredEnvironmentValue -Name "TELEGRAM_BOT_TOKEN").Trim()
  $script:TelegramBridgeEnabled = -not [string]::IsNullOrWhiteSpace($botToken)
  $script:TelegramBotId = 0L
  $script:TelegramRealmId = ""
  if (-not $script:TelegramBridgeEnabled) {
    return
  }
  if ($bridgeSecret -eq $botToken) {
    throw "Telegram bridge token must be distinct from the Telegram bot token."
  }
  if ($botToken -notmatch '^([1-9][0-9]*):') {
    throw "TELEGRAM_BOT_TOKEN does not contain a valid immutable numeric bot id prefix."
  }
  try {
    $botId = [int64]$Matches[1]
  } catch {
    throw "TELEGRAM_BOT_TOKEN bot id is outside the supported integer range."
  }
  $canonicalRealm = "telegram:$botId"
  $configuredBotId = (Get-ConfiguredEnvironmentValue -Name "JARVIS_TELEGRAM_BOT_ID").Trim()
  if ($configuredBotId -and $configuredBotId -ne [string]$botId) {
    throw "JARVIS_TELEGRAM_BOT_ID does not match TELEGRAM_BOT_TOKEN."
  }
  $configuredRealm = (Get-ConfiguredEnvironmentValue -Name "JARVIS_TELEGRAM_REALM_ID").Trim()
  if ($configuredRealm -and $configuredRealm -ne $canonicalRealm) {
    throw "JARVIS_TELEGRAM_REALM_ID must use the canonical telegram:<bot_id> realm."
  }
  $legacyRealm = (Get-ConfiguredEnvironmentValue -Name "JARVIS_TELEGRAM_LEGACY_REALM_ID").Trim()
  $legacySourceRealm = (
    Get-ConfiguredEnvironmentValue -Name "JARVIS_TELEGRAM_LEGACY_SOURCE_REALM_ID"
  ).Trim()
  if ($legacySourceRealm -eq $canonicalRealm) {
    throw "JARVIS_TELEGRAM_LEGACY_SOURCE_REALM_ID must differ from the canonical destination realm."
  }
  if ($legacySourceRealm.Length -gt 120) {
    throw "JARVIS_TELEGRAM_LEGACY_SOURCE_REALM_ID must not exceed 120 characters."
  }
  $legacyStoreExists = Test-Path -LiteralPath $TelegramLegacyStore
  if ($legacyStoreExists -and -not $legacyRealm) {
    throw (
      "Realm-less Telegram history requires an explicit " +
      "JARVIS_TELEGRAM_LEGACY_REALM_ID=$canonicalRealm after verifying the bot with getMe."
    )
  }
  if ($legacyStoreExists -and $legacyRealm -ne $canonicalRealm) {
    throw "JARVIS_TELEGRAM_LEGACY_REALM_ID must map the legacy store into the canonical bot realm."
  }

  # These are assertions only: the bridge still calls getMe and fails closed unless the
  # actual immutable bot identity matches before opening/migrating the durable store.
  $env:TELEGRAM_BOT_TOKEN = $botToken
  $env:JARVIS_TELEGRAM_BOT_ID = [string]$botId
  $env:JARVIS_TELEGRAM_REALM_ID = $canonicalRealm
  if ($legacyRealm) {
    $env:JARVIS_TELEGRAM_LEGACY_REALM_ID = $legacyRealm
  }
  if ($legacySourceRealm) {
    $env:JARVIS_TELEGRAM_LEGACY_SOURCE_REALM_ID = $legacySourceRealm
  }
  $script:TelegramBotId = $botId
  $script:TelegramRealmId = $canonicalRealm
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

function Get-LocalEnvironmentFileSha256 {
  $envFile = if ($env:JARVIS_ENV_FILE) {
    [string]$env:JARVIS_ENV_FILE
  } else {
    Join-Path $RepoRoot "backend\.env.local"
  }
  if (-not (Test-Path -LiteralPath $envFile)) {
    return ""
  }
  return [string](Get-FileHash -LiteralPath $envFile -Algorithm SHA256).Hash.ToLowerInvariant()
}

function Get-FrontendEnvironmentSha256 {
  $values = @(
    [string]$env:JARVIS_BACKEND_URL
    [string]$env:JARVIS_API_TOKEN
    [string]$env:JARVIS_UI_SESSION_SECRET
    [string]$env:JARVIS_TRUST_PROXY_HEADERS
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
    [string]$env:TELEGRAM_BOT_TOKEN
    [string]$env:JARVIS_TELEGRAM_BRIDGE_SECRET
    [string]$env:JARVIS_TELEGRAM_REALM_ID
    [string]$env:JARVIS_TELEGRAM_BOT_ID
    [string]$env:JARVIS_TELEGRAM_SESSION_TTL_SECONDS
    [string]$env:JARVIS_TELEGRAM_USER_RATE_LIMIT_PER_MINUTE
    [string]$env:JARVIS_TELEGRAM_GLOBAL_RATE_LIMIT_PER_MINUTE
    [string]$env:JARVIS_API_USER_RATE_LIMIT_PER_MINUTE
    [string]$env:JARVIS_CORS_ORIGINS
    [string]$env:JARVIS_EXECUTION_ROOTS
    [string]$env:JARVIS_EXECUTION_CAPABILITIES_FILE
    [string]$env:JARVIS_BRIDGE_APP_PATHS_JSON
    (Get-LocalEnvironmentFileSha256)
  )
  return Get-StringSha256 -Value ($values -join "`n")
}

function Get-TelegramEnvironmentSha256 {
  $values = @(
    [string]$env:TELEGRAM_BOT_TOKEN
    [string]$env:JARVIS_TELEGRAM_BRIDGE_SECRET
    [string]$env:JARVIS_TELEGRAM_REALM_ID
    [string]$env:JARVIS_TELEGRAM_BOT_ID
    [string]$env:JARVIS_TELEGRAM_LEGACY_REALM_ID
    [string]$env:JARVIS_TELEGRAM_LEGACY_SOURCE_REALM_ID
    [string]$env:JARVIS_BACKEND_URL
    [string]$env:TELEGRAM_ALLOWED_CHAT_IDS
    [string]$env:JARVIS_TELEGRAM_MAX_CONCURRENT_UPDATES
    [string]$env:JARVIS_TELEGRAM_MAX_PENDING_UPDATES
    [string]$env:JARVIS_TELEGRAM_MAX_PENDING_PER_USER
    [string]$env:JARVIS_TELEGRAM_BRIDGE_RATE_LIMIT_PER_MINUTE
    (Get-LocalEnvironmentFileSha256)
  )
  return Get-StringSha256 -Value ($values -join "`n")
}

function Get-ProfileCatalog {
  if ($null -ne $script:ProfileCatalog) {
    return $script:ProfileCatalog
  }

  Push-Location $RepoRoot
  try {
    $output = @(& py.exe -3.11 .\jarvis.py profiles)
    if ($LASTEXITCODE -ne 0) {
      throw "jarvis.py profiles exited with code $LASTEXITCODE`: $($output -join ' ')"
    }
    $parsed = (($output -join "`n") | ConvertFrom-Json)
  } finally {
    Pop-Location
  }

  $catalog = @{}
  foreach ($property in $parsed.PSObject.Properties) {
    $catalog[[string]$property.Name] = $property.Value
  }
  if ($catalog.Count -eq 0) {
    throw "jarvis.py profiles returned an empty profile catalog"
  }
  $script:ProfileCatalog = $catalog
  return $script:ProfileCatalog
}

function Get-ProfileCertification {
  param([string]$SelectedProfile)

  $catalog = Get-ProfileCatalog
  if (-not $catalog.ContainsKey($SelectedProfile)) {
    $available = @($catalog.Keys | Sort-Object) -join ", "
    throw "Unknown profile '$SelectedProfile'. Available profiles: $available"
  }
  return $catalog[$SelectedProfile]
}

function Get-DefaultProfileName {
  $catalog = Get-ProfileCatalog
  $recommended = @(
    $catalog.Values |
      Where-Object { [bool]$_.default_recommended } |
      Sort-Object name
  ) | Select-Object -First 1
  if ($recommended) {
    return [string]$recommended.name
  }
  throw "Profile catalog has no default_recommended profile"
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
      "This profile requires an explicit research opt-in. " +
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
  param(
    [string]$SelectedProfile,
    [switch]$SkipTelegramInitialization
  )

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
  Remove-Item Env:\JARVIS_BACKEND_RUNTIME_GENERATION -ErrorAction SilentlyContinue
  if (-not $SkipTelegramInitialization) {
    Initialize-TelegramBridgeEnvironment -ApiToken $apiToken
  }
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

function Test-FrontendBindingMatchesMode {
  param([int]$Port)

  if ($script:LanMode) {
    return (Test-PortExposedForLan -Port $Port)
  }

  $addresses = @(Get-PortListenAddresses -Port $Port)
  if ($addresses.Count -eq 0) {
    return $false
  }
  $loopbackAddresses = @("127.0.0.1", "::1")
  return @($addresses | Where-Object { $_ -notin $loopbackAddresses }).Count -eq 0
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
      identity_known = $false
      ownership_provenance_known = $false
      exists = $null
      running = $false
      state = "unknown"
      status = "Docker is not available in PATH"
      image = ""
    }
  }

  try {
    $lines = @(
      & $docker.Source ps -a --filter "name=^/jarvis-gpt-dispatcher$" --format "{{json .}}" 2>$null
    )
    if ($LASTEXITCODE -ne 0) {
      throw "docker ps failed while resolving dispatcher identity"
    }
    $line = $lines | Select-Object -First 1
    if (-not $line) {
      return @{
        docker_available = $true
        identity_known = $true
        ownership_provenance_known = $false
        exists = $false
        running = $false
        state = "missing"
        status = "container not found"
        image = ""
      }
    }
    $container = $line | ConvertFrom-Json
    $containerId = [string](
      & $docker.Source inspect jarvis-gpt-dispatcher --format "{{.Id}}" 2>$null |
        Select-Object -First 1
    )
    $containerId = $containerId.Trim()
    if ($containerId -notmatch '^[0-9a-fA-F]{64}$') {
      throw "docker inspect did not return a valid full 64-hex dispatcher container ID"
    }
    $containerId = $containerId.ToLowerInvariant()
    $operationNonceOutput = @(
      & $docker.Source inspect jarvis-gpt-dispatcher --format `
        '{{ index .Config.Labels "com.jarvis-gpt.dispatcher.operation-nonce" }}' `
        2>$null
    )
    $operationNonceExitCode = $LASTEXITCODE
    $operationNonce = [string]($operationNonceOutput | Select-Object -First 1)
    $operationNonce = $operationNonce.Trim().ToLowerInvariant()
    $operationNonceKnown = [bool](
      $operationNonceExitCode -eq 0 -and
      $operationNonce -match '^[0-9a-f]{32}$'
    )
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
      identity_known = $true
      ownership_provenance_known = $operationNonceKnown
      exists = $true
      running = $state -eq "running"
      state = $state
      status = [string]$container.Status
      image = [string]$container.Image
      id = $containerId
      operation_nonce = $operationNonce
      command = $command
      runtime = Get-DispatcherContainerRuntime -Command $command
    }
  } catch {
    return @{
      docker_available = $true
      identity_known = $false
      ownership_provenance_known = $false
      exists = $null
      running = $false
      state = "unknown"
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
  Set-JarvisEnvironment -SelectedProfile $Profile -SkipTelegramInitialization

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
  } elseif (-not [bool]$container.identity_known) {
    $phase = "docker-unknown"
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
  $completionProbe = $null
  if ($ready) {
    # /v1/models only proves that the HTTP surface is present. Readiness also requires
    # one bounded, deterministic completion from the live configured model.
    $completionProbe = Invoke-ProfileHealthProbe
    if (-not $completionProbe.ok) {
      $ready = $false
      $unhealthyReason = [string]$completionProbe.error
      if ([bool]$completionProbe.warming) {
        $phase = "completion-warming"
      } else {
        $unhealthy = $true
        $phase = "unhealthy"
      }
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
    completion_ok = [bool]($completionProbe -and $completionProbe.ok)
    completion_status = if (-not $completionProbe) {
      "not-run"
    } elseif ($completionProbe.ok) {
      "ok"
    } elseif ($completionProbe.warming) {
      "warming"
    } else {
      "failed"
    }
    completion_error = if ($completionProbe) { [string]$completionProbe.error } else { "" }
    log_signals = Get-DispatcherLogSignals
    checked_at = (Get-Date).ToString("HH:mm:ss")
  }
}

function Wait-LlmReady {
  param(
    [int]$TimeoutSec,
    [int]$PollIntervalMilliseconds = 5000
  )

  $effectiveTimeoutSec = [Math]::Max(1, $TimeoutSec)
  $effectivePollMilliseconds = [Math]::Max(1, $PollIntervalMilliseconds)
  $deadline = (Get-Date).AddSeconds($effectiveTimeoutSec)
  $lastReadiness = $null
  while ($true) {
    $lastReadiness = Get-LlmReadiness
    if ([bool]$lastReadiness.ready) {
      return $lastReadiness
    }
    if ([bool]$lastReadiness.unhealthy) {
      $detail = [string]$lastReadiness.unhealthy_reason
      if ([string]::IsNullOrWhiteSpace($detail)) {
        $detail = "live completion probe reported an unhealthy model"
      }
      throw ("LLM failed live completion readiness: {0}" -f $detail)
    }

    $remainingMilliseconds = [int][Math]::Floor(
      ($deadline - (Get-Date)).TotalMilliseconds
    )
    if ($remainingMilliseconds -le 0) {
      break
    }
    Start-Sleep -Milliseconds ([Math]::Min($effectivePollMilliseconds, $remainingMilliseconds))
  }

  $phase = if ($lastReadiness) { [string]$lastReadiness.phase } else { "unknown" }
  $detail = if ($lastReadiness) { [string]$lastReadiness.completion_error } else { "" }
  if ([string]::IsNullOrWhiteSpace($detail)) {
    $detail = "last phase was $phase"
  }
  throw (
    "LLM did not pass live completion readiness within {0}s (phase={1}): {2}" -f
    $effectiveTimeoutSec,
    $phase,
    $detail
  )
}

function Invoke-ProfileHealthProbe {
  # Bounded live-model probe: the answer contract is intentionally exact so an HTTP
  # facade, a wrong model, or a degenerated completion can never be reported as ready.
  try {
    $body = @{
      model = "dispatcher"
      temperature = 0
      max_tokens = 16
      chat_template_kwargs = @{ enable_thinking = $false }
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
      return @{
        ok = $false
        warming = $false
        error = "health probe returned empty content"
        content = $content
        normalized_content = ""
      }
    }
    if ($content -match '(.)\1{11,}' -or $content -match '(\b\w+\b)(?:\s+\1){8,}') {
      return @{
        ok = $false
        warming = $false
        error = "health probe detected repeated-token degeneration"
        content = $content
        normalized_content = ""
      }
    }
    $normalizedContent = ($content -replace '\s+', ' ').Trim()
    if ($normalizedContent -ne "4") {
      return @{
        ok = $false
        warming = $false
        error = "health probe returned an unexpected answer to 2+2"
        content = $content
        normalized_content = $normalizedContent
      }
    }
    return @{
      ok = $true
      warming = $false
      error = ""
      content = $content
      normalized_content = $normalizedContent
    }
  } catch {
    $detail = [string]$_.Exception.Message
    if ([string]::IsNullOrWhiteSpace($detail)) {
      $detail = $_.Exception.GetType().Name
    }
    # /v1/models can appear before the first completion succeeds. Keep that state
    # explicitly in warmup, but never turn a timeout/HTTP failure into ready=true.
    return @{
      ok = $false
      warming = $true
      error = ("health completion probe failed: {0}" -f $detail)
      content = ""
      normalized_content = ""
    }
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
    "completion-warming" { "Yellow" }
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
    "telegram" {
      return (
        $text.Contains($repoNeedle) -and
        $text.Contains("jarvis.py") -and
        $text.Contains("telegram-bridge")
      )
    }
    default {
      return Test-JarvisProcess -ProcessInfo $ProcessInfo
    }
  }
}

function Get-ManagedTelegramProcesses {
  param([array]$Snapshot = $null)

  if ($null -eq $Snapshot) {
    $Snapshot = Get-ProcessSnapshot
  }
  return @(
    $Snapshot |
      Where-Object { Test-ManagedServiceProcess -ProcessInfo $_ -Service "telegram" }
  )
}

function Stop-ManagedTelegramProcesses {
  param([string]$Reason = "Telegram bridge disabled")

  $snapshot = Get-ProcessSnapshot
  $telegramProcesses = @(Get-ManagedTelegramProcesses -Snapshot $snapshot)
  if ($telegramProcesses.Count -eq 0) {
    return
  }

  $protected = Get-CurrentProcessFamilyIds
  foreach ($telegramProcess in $telegramProcesses) {
    Stop-ProcessTree `
      -ProcessId ([int]$telegramProcess.ProcessId) `
      -Reason $Reason `
      -Snapshot $snapshot `
      -ProtectedProcessIds $protected | Out-Null
  }

  $deadline = (Get-Date).AddSeconds(5)
  do {
    $remaining = @(Get-ManagedTelegramProcesses)
    if ($remaining.Count -eq 0) {
      return
    }
    Start-Sleep -Milliseconds 100
  } while ((Get-Date) -lt $deadline)

  $remainingIds = @($remaining | ForEach-Object { [int]$_.ProcessId }) -join ","
  throw (
    "Could not stop managed Telegram bridge process(es) pid={0}; launcher state was not rewritten." -f
    $remainingIds
  )
}

function Test-TelegramBridgeReuseState {
  param(
    $PreviousState,
    [array]$Snapshot = $null
  )

  if ($null -eq $Snapshot) {
    $Snapshot = Get-ProcessSnapshot
  }
  $telegramEntry = if ($PreviousState -and $PreviousState.services) {
    $PreviousState.services.telegram
  } else {
    $null
  }
  if (-not $telegramEntry -or -not $telegramEntry.pid) {
    return $false
  }

  $trackedPid = [int]$telegramEntry.pid
  $trackedProcess = $Snapshot |
    Where-Object { [int]$_.ProcessId -eq $trackedPid } |
    Select-Object -First 1
  if (-not (Test-ManagedServiceProcess -ProcessInfo $trackedProcess -Service "telegram")) {
    return $false
  }
  if (
    [string]$telegramEntry.realm_id -ne $script:TelegramRealmId -or
    [int64]$telegramEntry.bot_id -ne $script:TelegramBotId
  ) {
    return $false
  }

  # A normal py.exe launcher and its python child are one managed process tree. Any
  # matching process outside that tree is a duplicate bridge and forces replacement.
  $trackedTreeIds = @($trackedPid)
  $trackedTreeIds += @(
    Get-DescendantProcessIds -RootProcessId $trackedPid -Snapshot $Snapshot
  )
  $unexpectedManaged = @(
    Get-ManagedTelegramProcesses -Snapshot $Snapshot |
      Where-Object { $trackedTreeIds -notcontains [int]$_.ProcessId }
  )
  return $unexpectedManaged.Count -eq 0
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

function Wait-BackendApiReady {
  param([int]$TimeoutSec = 30)

  $deadline = (Get-Date).AddSeconds([Math]::Max(1, $TimeoutSec))
  while ((Get-Date) -lt $deadline) {
    $probe = Invoke-HttpProbe -Uri "http://127.0.0.1:8000/" -TimeoutSec 2 -Json
    if ($probe.ok -and [string]$probe.data.status -eq "online") {
      return $true
    }
    Start-Sleep -Milliseconds 250
  }
  return $false
}

function Wait-TelegramBridgeReady {
  param(
    [int]$ProcessId,
    [string]$StderrPath,
    [datetime]$StartedAtUtc,
    [int]$TimeoutSec = 45
  )

  $deadline = (Get-Date).AddSeconds([Math]::Max(1, $TimeoutSec))
  while ((Get-Date) -lt $deadline) {
    $processInfo = Get-ProcessSnapshot |
      Where-Object { [int]$_.ProcessId -eq $ProcessId } |
      Select-Object -First 1
    if (-not (Test-ManagedServiceProcess -ProcessInfo $processInfo -Service "telegram")) {
      return @{ ok = $false; error = "Telegram bridge process exited before readiness" }
    }
    if (Test-Path -LiteralPath $StderrPath) {
      $logFile = Get-Item -LiteralPath $StderrPath -Force
      if ($logFile.LastWriteTimeUtc -ge $StartedAtUtc.AddSeconds(-1)) {
        $content = Get-Content -LiteralPath $StderrPath -Raw -ErrorAction SilentlyContinue
        if ($content -and $content.Contains("Telegram bridge online as @")) {
          return @{ ok = $true; error = "" }
        }
      }
    }
    Start-Sleep -Milliseconds 250
  }
  return @{
    ok = $false
    error = "Telegram bridge did not pass getMe identity and durable-store readiness"
  }
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
  if (
    $container -and
    $container.Contains("identity_known") -and
    -not [bool]$container["identity_known"]
  ) {
    return "unknown"
  }
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

function Test-DispatcherContainerIdAbsent {
  param(
    [Parameter(Mandatory = $true)][string]$ContainerId,
    [Parameter(Mandatory = $true)][string]$DockerPath
  )

  $output = @(& $DockerPath inspect $ContainerId --format "{{.Id}}" 2>&1)
  $exitCode = $LASTEXITCODE
  if ($exitCode -eq 0) {
    return @{ ok = $true; absent = $false; error = "container still exists" }
  }
  $detail = (($output | ForEach-Object { [string]$_ }) -join "`n").Trim()
  if ($detail -match '(?i)(no such object|no such container)') {
    return @{ ok = $true; absent = $true; error = "" }
  }
  return @{ ok = $false; absent = $false; error = $detail }
}

function Stop-DispatcherRuntime {
  param(
    [Parameter(Mandatory = $true)][string]$ExpectedContainerId,
    [Parameter(Mandatory = $true)][string]$ExpectedOperationNonce,
    [switch]$DispatcherOperationLockHeld
  )

  $docker = Get-Command docker -ErrorAction SilentlyContinue
  if (-not $docker) {
    Write-Host "Docker CLI is not available; dispatcher container control skipped." -ForegroundColor DarkYellow
    return @{ ok = $false; stopped = $false; reason = "docker-cli-unavailable" }
  }

  $probe = Test-DockerReady
  if (-not $probe.ready) {
    Write-Host ("Docker API is not ready; dispatcher container control skipped: {0}" -f $probe.error) -ForegroundColor DarkYellow
    return @{ ok = $false; stopped = $false; reason = "docker-api-unavailable" }
  }

  $expected = $ExpectedContainerId.Trim()
  if ($expected -notmatch '^[0-9a-fA-F]{64}$') {
    Write-Host "Preserving LLM runtime because launcher state has no valid full 64-hex container ID." -ForegroundColor Cyan
    return @{ ok = $false; stopped = $false; reason = "invalid-expected-container-id" }
  }
  $expected = $expected.ToLowerInvariant()
  $expectedNonce = $ExpectedOperationNonce.Trim().ToLowerInvariant()
  if ($expectedNonce -notmatch '^[0-9a-f]{32}$') {
    Write-Host "Preserving LLM runtime because launcher state has no valid operation nonce." -ForegroundColor Cyan
    return @{ ok = $false; stopped = $false; reason = "invalid-expected-operation-nonce" }
  }

  $stopAction = {
    $current = Get-DispatcherContainerSnapshot
    $currentId = [string]$current.id
    $currentNonce = [string]$current.operation_nonce
    if (-not [bool]$current.identity_known) {
      Write-Host "Preserving dispatcher because Docker identity is unknown." -ForegroundColor DarkYellow
      return @{ ok = $false; stopped = $false; reason = "docker-identity-unknown" }
    }
    if (-not $current.exists) {
      $absence = Test-DispatcherContainerIdAbsent `
        -ContainerId $expected `
        -DockerPath $docker.Source
      if ($absence.ok -and $absence.absent) {
        Write-Host "Dispatcher container is already absent." -ForegroundColor DarkYellow
        return @{ ok = $true; stopped = $true; already_absent = $true; reason = "absent" }
      }
      return @{
        ok = $false
        stopped = $false
        reason = "expected-container-absence-unproven"
        verification = $absence
      }
    }
    if (
      -not [bool]$current.ownership_provenance_known -or
      $currentId -notmatch '^[0-9a-fA-F]{64}$' -or
      $currentNonce -notmatch '^[0-9a-fA-F]{32}$' -or
      $currentId.ToLowerInvariant() -ne $expected -or
      $currentNonce.ToLowerInvariant() -ne $expectedNonce
    ) {
      Write-Host (
        "Preserving replacement dispatcher because full ID/nonce CAS failed (expected={0}, current={1})." -f
        $expected,
        $currentId
      ) -ForegroundColor Cyan
      return @{
        ok = $false
        stopped = $false
        reason = "container-id-or-nonce-cas-mismatch"
        current_container_id = $currentId
      }
    }

    # Remove the exact immutable container ID while holding the shared backend lock.
    Write-Host ("Stopping dispatcher container id={0}..." -f $expected) -ForegroundColor Yellow
    $removeOutput = @(& $docker.Source rm -f $expected 2>&1)
    $removeExitCode = $LASTEXITCODE
    if ($removeExitCode -ne 0) {
      Write-Host "The owned dispatcher was not removed; a concurrent replacement is preserved." -ForegroundColor DarkYellow
      return @{
        ok = $false
        stopped = $false
        reason = "docker-rm-failed"
        returncode = $removeExitCode
        error = (($removeOutput | ForEach-Object { [string]$_ }) -join "`n").Trim()
      }
    }
    $absence = Test-DispatcherContainerIdAbsent `
      -ContainerId $expected `
      -DockerPath $docker.Source
    if (-not $absence.ok -or -not $absence.absent) {
      return @{
        ok = $false
        stopped = $false
        reason = "docker-rm-absence-unproven"
        verification = $absence
      }
    }
    return @{
      ok = $true
      stopped = $true
      reason = "exact-id-removed"
      container_id = $expected
      operation_nonce = $expectedNonce
    }
  }
  if ($DispatcherOperationLockHeld) {
    return & $stopAction
  }
  return Invoke-WithDispatcherOperationLock -Action $stopAction
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

function Invoke-JarvisJsonCommand {
  param(
    [string]$FilePath,
    [string[]]$Arguments,
    [string]$WorkingDirectory = $RepoRoot
  )

  Push-Location $WorkingDirectory
  try {
    $output = @(& $FilePath @Arguments)
    if ($LASTEXITCODE -ne 0) {
      throw "$FilePath exited with code $LASTEXITCODE"
    }
    try {
      return (($output -join [Environment]::NewLine) | ConvertFrom-Json)
    } catch {
      throw "$FilePath returned malformed JSON for a verified dispatcher mutation."
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

function Start-BackendProcess {
  param(
    [string]$FilePath,
    [string[]]$Arguments,
    [string]$WorkingDirectory,
    [string]$Stdout,
    [string]$Stderr
  )

  $generation = [Guid]::NewGuid().ToString("N").ToLowerInvariant()
  $hadPreviousGeneration = Test-Path Env:\JARVIS_BACKEND_RUNTIME_GENERATION
  $previousGeneration = [string]$env:JARVIS_BACKEND_RUNTIME_GENERATION
  try {
    # Scope the generation to the backend process tree. Uvicorn workers inherit the
    # same value, while the LLM, host bridge, frontend, and Telegram do not.
    $env:JARVIS_BACKEND_RUNTIME_GENERATION = $generation
    return Start-ManagedProcess `
      -Name "backend" `
      -FilePath $FilePath `
      -Arguments $Arguments `
      -WorkingDirectory $WorkingDirectory `
      -Stdout $Stdout `
      -Stderr $Stderr
  } finally {
    if ($hadPreviousGeneration) {
      $env:JARVIS_BACKEND_RUNTIME_GENERATION = $previousGeneration
    } else {
      Remove-Item Env:\JARVIS_BACKEND_RUNTIME_GENERATION -ErrorAction SilentlyContinue
    }
  }
}

function Invoke-WithLauncherStateLock {
  param(
    [Parameter(Mandatory = $true)][scriptblock]$Action,
    [int]$TimeoutMs = 5000
  )

  $lockPath = Join-Path $StateDir "launcher-state.lock"
  $deadline = [DateTime]::UtcNow.AddMilliseconds([Math]::Max(0, $TimeoutMs))
  $stream = $null
  while ($null -eq $stream) {
    $candidate = $null
    try {
      $candidate = [System.IO.File]::Open(
        $lockPath,
        [System.IO.FileMode]::OpenOrCreate,
        [System.IO.FileAccess]::ReadWrite,
        [System.IO.FileShare]::ReadWrite
      )
      if ($candidate.Length -eq 0) {
        $candidate.WriteByte(0)
        $candidate.Flush($true)
      }
      $candidate.Position = 0
      $candidate.Lock(0, 1)
      $stream = $candidate
    } catch [System.IO.IOException] {
      if ($candidate) {
        $candidate.Dispose()
      }
      if ([DateTime]::UtcNow -ge $deadline) {
        throw "Timed out acquiring launcher state lock: $lockPath"
      }
      Start-Sleep -Milliseconds 50
    }
  }

  try {
    return & $Action
  } finally {
    try {
      $stream.Position = 0
      $stream.Unlock(0, 1)
    } finally {
      $stream.Dispose()
    }
  }
}

function Invoke-WithDispatcherOperationLock {
  param(
    [Parameter(Mandatory = $true)][scriptblock]$Action,
    [int]$TimeoutMs = 5000
  )

  $lockPath = Join-Path $StateDir "dispatcher-operation.lock"
  $deadline = [DateTime]::UtcNow.AddMilliseconds([Math]::Max(0, $TimeoutMs))
  $stream = $null
  while ($null -eq $stream) {
    $candidate = $null
    try {
      $candidate = [System.IO.File]::Open(
        $lockPath,
        [System.IO.FileMode]::OpenOrCreate,
        [System.IO.FileAccess]::ReadWrite,
        [System.IO.FileShare]::ReadWrite
      )
      if ($candidate.Length -eq 0) {
        $candidate.WriteByte(0)
        $candidate.Flush($true)
      }
      $candidate.Position = 0
      $candidate.Lock(0, 1)
      $stream = $candidate
    } catch [System.IO.IOException] {
      if ($candidate) {
        $candidate.Dispose()
      }
      if ([DateTime]::UtcNow -ge $deadline) {
        throw "Timed out acquiring dispatcher operation lock: $lockPath"
      }
      Start-Sleep -Milliseconds 50
    }
  }

  try {
    return & $Action
  } finally {
    try {
      $stream.Position = 0
      $stream.Unlock(0, 1)
    } finally {
      $stream.Dispose()
    }
  }
}

function Save-LauncherState {
  param(
    [hashtable]$Services,
    [switch]$DispatcherOperationLockHeld
  )

  $saveAction = {
    Invoke-WithLauncherStateLock -Action {
    # Re-read Docker under the state lock, but never claim a replacement. Ownership
    # continues only when both full immutable IDs are valid and exactly equal.
    if ($Services.ContainsKey("dispatcher") -and [bool]$Services.dispatcher.started_by_launcher) {
      $dispatcherSnapshot = Get-DispatcherContainerSnapshot
      $recordedId = [string]$Services.dispatcher.container_id
      $recordedNonce = [string]$Services.dispatcher.operation_nonce
      $currentId = [string]$dispatcherSnapshot.id
      $currentNonce = [string]$dispatcherSnapshot.operation_nonce
      if (
        -not $dispatcherSnapshot.running -or
        -not [bool]$dispatcherSnapshot.ownership_provenance_known -or
        $recordedId -notmatch '^[0-9a-fA-F]{64}$' -or
        $recordedNonce -notmatch '^[0-9a-fA-F]{32}$' -or
        $currentId -notmatch '^[0-9a-fA-F]{64}$' -or
        $currentNonce -notmatch '^[0-9a-fA-F]{32}$' -or
        $recordedId.ToLowerInvariant() -ne $currentId.ToLowerInvariant() -or
        $recordedNonce.ToLowerInvariant() -ne $currentNonce.ToLowerInvariant()
      ) {
        throw (
          "Cannot save launcher ownership: full dispatcher ID/nonce CAS " +
          "did not match while holding launcher-state.lock."
        )
      }
    }
    $state = [ordered]@{
      profile = $Profile
      home = $HomePath
      lan = [bool]$script:LanMode
      lan_ip = if ($script:LanMode) { Get-LanIPv4 } else { $null }
      frontend_url = Get-FrontendUrl
      backend_url = Get-BackendUrl
      backend_environment_sha256 = (Get-BackendEnvironmentSha256)
      frontend_environment_sha256 = (Get-FrontendEnvironmentSha256)
      telegram_environment_sha256 = (Get-TelegramEnvironmentSha256)
      started_at = (Get-Date).ToString("o")
      services = $Services
    }
    $json = ($state | ConvertTo-Json -Depth 8) + [Environment]::NewLine
    $temporary = Join-Path $StateDir (
      "launcher-state.json.tmp.{0}.{1}" -f $PID, [Guid]::NewGuid().ToString("N")
    )
    $backup = Join-Path $StateDir (
      "launcher-state.json.bak.{0}.{1}" -f $PID, [Guid]::NewGuid().ToString("N")
    )
    $stream = $null
    try {
      $encoding = New-Object System.Text.UTF8Encoding($false)
      $bytes = $encoding.GetBytes($json)
      $stream = [System.IO.File]::Open(
        $temporary,
        [System.IO.FileMode]::CreateNew,
        [System.IO.FileAccess]::Write,
        [System.IO.FileShare]::None
      )
      $stream.Write($bytes, 0, $bytes.Length)
      $stream.Flush($true)
      $stream.Dispose()
      $stream = $null
      if (Test-Path -LiteralPath $StateFile) {
        # Windows PowerShell 5.1 binds $null to an illegal empty backup path for
        # File.Replace. A same-directory backup keeps the replacement atomic and
        # valid on the production runtime; it is removed after the commit.
        [System.IO.File]::Replace($temporary, $StateFile, $backup)
        if (Test-Path -LiteralPath $backup) {
          Remove-Item -LiteralPath $backup -Force
        }
      } else {
        [System.IO.File]::Move($temporary, $StateFile)
      }
      if (
        $Services.ContainsKey("dispatcher") -and
        [bool]$Services.dispatcher.started_by_launcher
      ) {
        $journal = Read-DispatcherOwnershipJournal
        if ($journal) {
          $recordedId = [string]$Services.dispatcher.container_id
          $recordedNonce = [string]$Services.dispatcher.operation_nonce
          $journalId = [string]$journal.container_id
          $journalNonce = [string]$journal.operation_nonce
          if (
            $recordedId -match '^[0-9a-fA-F]{64}$' -and
            $recordedNonce -match '^[0-9a-fA-F]{32}$' -and
            $journalNonce.ToLowerInvariant() -eq $recordedNonce.ToLowerInvariant() -and
            (
              [string]::IsNullOrWhiteSpace($journalId) -or
              (
                $journalId -match '^[0-9a-fA-F]{64}$' -and
                $journalId.ToLowerInvariant() -eq $recordedId.ToLowerInvariant()
              )
            )
          ) {
            Remove-Item -LiteralPath $DispatcherOwnershipJournal -Force
          }
        }
      }
    } finally {
      if ($stream) {
        $stream.Dispose()
      }
      if (Test-Path -LiteralPath $temporary) {
        Remove-Item -LiteralPath $temporary -Force
      }
      if (Test-Path -LiteralPath $backup) {
        Remove-Item -LiteralPath $backup -Force -ErrorAction SilentlyContinue
      }
    }
    }
  }
  if (
    $Services.ContainsKey("dispatcher") -and
    -not $DispatcherOperationLockHeld
  ) {
    Invoke-WithDispatcherOperationLock -Action $saveAction
    return
  }
  & $saveAction
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

function Get-LauncherControlFileFingerprint {
  param([Parameter(Mandatory = $true)][string]$Path)

  if (-not (Test-Path -LiteralPath $Path)) {
    return "absent"
  }
  try {
    $digest = [string](Get-FileHash -LiteralPath $Path -Algorithm SHA256).Hash
    if ($digest -notmatch '^[0-9a-fA-F]{64}$') {
      throw "SHA256 digest is malformed"
    }
    return ("sha256:" + $digest.ToLowerInvariant())
  } catch {
    throw "Could not fingerprint launcher control file ${Path}: $($_.Exception.Message)"
  }
}

function Read-DispatcherOwnershipJournal {
  if (
    [string]::IsNullOrWhiteSpace([string]$DispatcherOwnershipJournal) -or
    -not (Test-Path -LiteralPath $DispatcherOwnershipJournal)
  ) {
    return $null
  }
  try {
    $journal = Get-Content -LiteralPath $DispatcherOwnershipJournal -Raw | ConvertFrom-Json
    $hasLauncherOwned = $null -ne $journal.PSObject.Properties['launcher_owned']
    $hasRequiresStateSync = $null -ne $journal.PSObject.Properties['requires_state_sync']
    if (
      [int]$journal.version -ne 1 -or
      -not $hasLauncherOwned -or
      -not $hasRequiresStateSync -or
      [string]$journal.operation_nonce -notmatch '^[0-9a-fA-F]{32}$' -or
      @("intent", "candidate", "rollback-intent", "rollback-candidate", "stop-intent", "stopped") -notcontains
        [string]$journal.phase
    ) {
      return $null
    }
    $containerId = [string]$journal.container_id
    $previousId = [string]$journal.previous_container_id
    $stateExpectedId = [string]$journal.state_expected_container_id
    $stateExpectedNonce = [string]$journal.state_expected_operation_nonce
    if (
      (-not [string]::IsNullOrWhiteSpace($containerId) -and $containerId -notmatch '^[0-9a-fA-F]{64}$') -or
      (-not [string]::IsNullOrWhiteSpace($previousId) -and $previousId -notmatch '^[0-9a-fA-F]{64}$') -or
      (
        [string]::IsNullOrWhiteSpace($containerId) -and
        [string]::IsNullOrWhiteSpace($previousId) -and
        @("intent", "rollback-intent") -notcontains [string]$journal.phase
      )
    ) {
      return $null
    }
    if (
      [bool]$journal.requires_state_sync -and
      (
        $stateExpectedId -notmatch '^[0-9a-fA-F]{64}$' -or
        $stateExpectedNonce -notmatch '^[0-9a-fA-F]{32}$'
      )
    ) {
      return $null
    }
    return $journal
  } catch {
    return $null
  }
}

function Get-JournalOwnedDispatcherProof {
  param([System.Collections.IDictionary]$Snapshot)

  if (
    -not $Snapshot -or
    -not [bool]$Snapshot["identity_known"]
  ) {
    return $null
  }
  $journal = Read-DispatcherOwnershipJournal
  if (-not $journal) {
    return $null
  }
  $currentId = [string]$Snapshot["id"]
  $currentNonce = [string]$Snapshot["operation_nonce"]
  $journalId = [string]$journal.container_id
  $previousId = [string]$journal.previous_container_id
  $journalNonce = [string]$journal.operation_nonce
  if (-not [bool]$Snapshot["exists"]) {
    $absentTarget = if ($journalId -match '^[0-9a-fA-F]{64}$') {
      $journalId
    } else {
      $previousId
    }
    if ($absentTarget -match '^[0-9a-fA-F]{64}$') {
      return @{
        container_id = $absentTarget.ToLowerInvariant()
        operation_nonce = $journalNonce.ToLowerInvariant()
        source = "journal"
      }
    }
    return $null
  }
  if (
    -not [bool]$Snapshot["ownership_provenance_known"] -or
    $currentId -notmatch '^[0-9a-fA-F]{64}$' -or
    $currentNonce -notmatch '^[0-9a-fA-F]{32}$' -or
    $journalNonce.ToLowerInvariant() -ne $currentNonce.ToLowerInvariant() -or
    (
      -not [string]::IsNullOrWhiteSpace($journalId) -and
      (
        $journalId -notmatch '^[0-9a-fA-F]{64}$' -or
        $journalId.ToLowerInvariant() -ne $currentId.ToLowerInvariant()
      )
    )
  ) {
    return $null
  }
  return @{
    container_id = $currentId.ToLowerInvariant()
    operation_nonce = $currentNonce.ToLowerInvariant()
    source = "journal"
  }
}

function Get-JournalOwnedDispatcherId {
  param([System.Collections.IDictionary]$Snapshot)

  $proof = Get-JournalOwnedDispatcherProof -Snapshot $Snapshot
  if ($proof) {
    return [string]$proof.container_id
  }
  return ""
}

function Test-LauncherOwnsDispatcher {
  param($State)

  if (-not $State -or -not $State.services) {
    # Missing or truncated state cannot prove ownership after a power loss.
    return $false
  }
  $entry = $State.services.dispatcher
  if (-not $entry) {
    return $false
  }
  $ownership = $entry.PSObject.Properties["started_by_launcher"]
  if (-not $ownership -or -not [bool]$ownership.Value) {
    return $false
  }
  $recordedId = [string]$entry.container_id
  $recordedNonce = [string]$entry.operation_nonce
  return [bool](
    $recordedId -match '^[0-9a-fA-F]{64}$' -and
    $recordedNonce -match '^[0-9a-fA-F]{32}$'
  )
}

function Get-LauncherOwnedDispatcherProof {
  param(
    $State,
    [System.Collections.IDictionary]$Snapshot
  )

  $journalProof = Get-JournalOwnedDispatcherProof -Snapshot $Snapshot
  if ($journalProof) {
    return $journalProof
  }
  if (Test-LauncherOwnsDispatcher -State $State) {
    $stateId = ([string]$State.services.dispatcher.container_id).ToLowerInvariant()
    $stateNonce = ([string]$State.services.dispatcher.operation_nonce).ToLowerInvariant()
    $currentId = [string]$Snapshot["id"]
    $currentNonce = [string]$Snapshot["operation_nonce"]
    if (
      [bool]$Snapshot["identity_known"] -and
      -not [bool]$Snapshot["exists"]
    ) {
      return @{
        container_id = $stateId
        operation_nonce = $stateNonce
        source = "launcher-state"
      }
    }
    if (
      [bool]$Snapshot["identity_known"] -and
      [bool]$Snapshot["ownership_provenance_known"] -and
      [bool]$Snapshot["exists"] -and
      $currentId -match '^[0-9a-fA-F]{64}$' -and
      $currentNonce -match '^[0-9a-fA-F]{32}$' -and
      $currentId.ToLowerInvariant() -eq $stateId -and
      $currentNonce.ToLowerInvariant() -eq $stateNonce
    ) {
      return @{
        container_id = $stateId
        operation_nonce = $stateNonce
        source = "launcher-state"
      }
    }
  }
  return $null
}

function Get-LauncherOwnedDispatcherId {
  param(
    $State,
    [System.Collections.IDictionary]$Snapshot
  )

  $proof = Get-LauncherOwnedDispatcherProof -State $State -Snapshot $Snapshot
  if ($proof) {
    return [string]$proof.container_id
  }
  return ""
}

function Test-ReusedDispatcherOwnership {
  param(
    $State,
    [System.Collections.IDictionary]$Readiness
  )

  if (
    -not $Readiness -or
    -not $Readiness["container"] -or
    -not [bool]$Readiness["container"]["running"]
  ) {
    return $false
  }
  $currentId = [string]$Readiness["container"]["id"]
  $currentNonce = [string]$Readiness["container"]["operation_nonce"]
  if (
    -not [bool]$Readiness["container"]["ownership_provenance_known"] -or
    $currentId -notmatch '^[0-9a-fA-F]{64}$' -or
    $currentNonce -notmatch '^[0-9a-fA-F]{32}$'
  ) {
    return $false
  }
  if (Test-LauncherOwnsDispatcher -State $State) {
    $previousId = [string]$State.services.dispatcher.container_id
    $previousNonce = [string]$State.services.dispatcher.operation_nonce
    if (
      $previousId -match '^[0-9a-fA-F]{64}$' -and
      $previousNonce -match '^[0-9a-fA-F]{32}$' -and
      $previousId.ToLowerInvariant() -eq $currentId.ToLowerInvariant() -and
      $previousNonce.ToLowerInvariant() -eq $currentNonce.ToLowerInvariant()
    ) {
      return $true
    }
  }
  return [bool](Get-JournalOwnedDispatcherId -Snapshot $Readiness["container"])
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
    (Join-Path $FrontendRoot "lib"),
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
    [bool]$TelegramEnvironmentChanged,
    [string]$BackendBindHost,
    [string]$FrontendBindHost
  )

  # Idempotent start: when every required managed service is already healthy and
  # launch environments are unchanged, skip all mutating CLI (init/dispatcher-up)
  # so the live API lease is never contested.
  if (
    $BackendEnvironmentChanged -or
    $FrontendEnvironmentChanged -or
    $TelegramEnvironmentChanged
  ) {
    return $null
  }
  if (
    -not $NoFrontend -and
    -not (Test-FrontendBindingMatchesMode -Port 3000)
  ) {
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

  $telegramFastPathRequested = [bool](
    $script:TelegramBridgeEnabled -and -not $NoTelegram -and -not $NoBackend
  )
  $llmReadiness = $null
  if (-not $NoDispatcher -or $telegramFastPathRequested) {
    $llmReadiness = Get-LlmReadiness
    if (-not [bool]$llmReadiness.ready) {
      return $null
    }
  }

  if (-not $NoDispatcher) {
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
      operation_nonce = [string]$llmReadiness.container.operation_nonce
      phase = [string]$llmReadiness.phase
    }
  } else {
    $services.dispatcher = @{
      profile = $Profile
      skipped = "app-without-llm-launch"
      started_by_launcher = $false
    }
  }

  if ($telegramFastPathRequested) {
    $telegramEntry = if ($PreviousState -and $PreviousState.services) {
      $PreviousState.services.telegram
    } else {
      $null
    }
    if (-not (Test-TelegramBridgeReuseState -PreviousState $PreviousState)) {
      return $null
    }
    $services.telegram = @{
      pid = [int]$telegramEntry.pid
      reused = $true
      realm_id = $script:TelegramRealmId
      bot_id = $script:TelegramBotId
      readiness = "live-llm+getMe+store"
      stderr = [string]$telegramEntry.stderr
    }
  } else {
    $services.telegram = @{
      skipped = if ($NoTelegram) {
        "disabled-by-launcher-option"
      } elseif ($NoBackend) {
        "backend-disabled"
      } else {
        "not-configured"
      }
    }
  }

  return $services
}

function Start-JarvisStack {
  Ensure-LauncherFolders
  try {
    Set-JarvisEnvironment -SelectedProfile $Profile
  } catch {
    # A changed/invalid bot identity must not leave the previously configured bridge
    # polling with stale credentials after startup validation fails closed.
    Stop-ManagedTelegramProcesses -Reason "Telegram startup validation failed"
    throw
  }
  $previousState = Read-LauncherState
  if (
    (Test-Path -LiteralPath $DispatcherOwnershipJournal) -and
    -not (Read-DispatcherOwnershipJournal)
  ) {
    throw (
      "Dispatcher ownership journal is invalid; refusing stack mutation until " +
      "its provenance is recovered or the file is reviewed manually."
    )
  }
  $backendEnvironmentSha256 = Get-BackendEnvironmentSha256
  $frontendEnvironmentSha256 = Get-FrontendEnvironmentSha256
  $telegramEnvironmentSha256 = Get-TelegramEnvironmentSha256
  $backendEnvironmentChanged = [bool](
    -not $previousState -or
    [string]$previousState.backend_environment_sha256 -ne $backendEnvironmentSha256
  )
  $frontendEnvironmentChanged = [bool](
    -not $previousState -or
    [string]$previousState.frontend_environment_sha256 -ne $frontendEnvironmentSha256
  )
  $telegramEnvironmentChanged = [bool](
    -not $previousState -or
    [string]$previousState.telegram_environment_sha256 -ne $telegramEnvironmentSha256
  )
  Ensure-LanFirewallRules
  $backendBindHost = "127.0.0.1"
  $frontendBindHost = Get-FrontendBindHost

  Write-Banner
  $telegramLaunchRequested = [bool](
    $script:TelegramBridgeEnabled -and -not $NoTelegram -and -not $NoBackend
  )

  # Reconcile the disabled desired state before the idempotent fast path can rewrite
  # launcher-state.json. A stale bridge retains its old bot token and the dedicated
  # backend credential, so merely marking it as skipped would leave Telegram live.
  if (-not $script:TelegramBridgeEnabled -or $NoTelegram -or $NoBackend) {
    $telegramStopReason = if ($NoTelegram) {
      "Telegram bridge disabled by launcher option"
    } elseif ($NoBackend) {
      "Telegram bridge requires the backend API"
    } else {
      "Telegram bridge token is not configured"
    }
    Stop-ManagedTelegramProcesses -Reason $telegramStopReason
  }

  $alreadyRunningServices = Get-AlreadyRunningStackServices `
    -PreviousState $previousState `
    -BackendEnvironmentChanged $backendEnvironmentChanged `
    -FrontendEnvironmentChanged $frontendEnvironmentChanged `
    -TelegramEnvironmentChanged $telegramEnvironmentChanged `
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

  $telegramReplacementRequired = [bool](
    $telegramLaunchRequested -and
    (
      $telegramEnvironmentChanged -or
      -not (Test-TelegramBridgeReuseState -PreviousState $previousState)
    )
  )
  if ($telegramLaunchRequested) {
    $telegramReplacementReason = if ($backendEnvironmentChanged) {
      "backend launch environment changed"
    } elseif ($telegramReplacementRequired) {
      "Telegram bridge state requires replacement"
    } else {
      "stack restart requires fresh live-LLM readiness"
    }
    # Once the whole-stack fast path is missed, pause all inbound Telegram work
    # before init/reuse/warmup. Only the fast path may retain a previously gated
    # bridge; every other path starts it again after a new live completion.
    Stop-ManagedTelegramProcesses -Reason $telegramReplacementReason
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
    if (
      $llmReadiness.container.Contains("identity_known") -and
      -not [bool]$llmReadiness.container.identity_known
    ) {
      if (-not (Ensure-DockerReady -TimeoutSec $DockerWaitSec)) {
        throw (
          "Docker identity remained unknown; preserving the last dispatcher " +
          "ownership record without a partial start."
        )
      }
      $llmReadiness = Get-LlmReadiness
      $dispatcherStatus = Get-DispatcherDesiredStatus
    }
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
        operation_nonce = [string]$llmReadiness.container.operation_nonce
        phase = [string]$llmReadiness.phase
      }
    } elseif ($llmStartDecision -eq "unknown") {
      throw (
        "Docker dispatcher identity is unknown; preserving the last ownership " +
        "record without starting or replacing a container."
      )
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
      Set-DispatcherComposeModelPath
      $dispatcherStartResult = Invoke-JarvisJsonCommand `
        -FilePath "py.exe" `
        -Arguments @(
          "-3.11",
          ".\jarvis.py",
          "--profile",
          $Profile,
          "dispatcher-up",
          "--launcher-owned"
        )
      $expectedDispatcherId = [string]$dispatcherStartResult.container_id
      $expectedOperationNonce = [string]$dispatcherStartResult.operation_nonce
      if (
        -not [bool]$dispatcherStartResult.ok -or
        $expectedDispatcherId -notmatch '^[0-9a-fA-F]{64}$' -or
        $expectedOperationNonce -notmatch '^[0-9a-fA-F]{32}$'
      ) {
        throw "Dispatcher manager did not return valid full-ID/operation-nonce provenance."
      }
      $startedDispatcher = Get-DispatcherContainerSnapshot
      if (
        -not $startedDispatcher.running -or
        [string]$startedDispatcher.id -notmatch '^[0-9a-fA-F]{64}$' -or
        [string]$startedDispatcher.operation_nonce -notmatch '^[0-9a-fA-F]{32}$' -or
        [string]$startedDispatcher.id -ne $expectedDispatcherId -or
        [string]$startedDispatcher.operation_nonce -ne $expectedOperationNonce
      ) {
        throw "Dispatcher start returned without valid full-ID/operation-nonce provenance."
      }
      $services.dispatcher = @{
        profile = $Profile
        docker = $true
        reused = $false
        started_by_launcher = $true
        container_id = $expectedDispatcherId
        operation_nonce = $expectedOperationNonce
        phase = "starting"
      }
      # Write-ahead ownership: any later bridge/backend/frontend failure leaves an
      # exact-ID recoverable state instead of an orphaned dispatcher.
      Save-LauncherState -Services $services
      Write-Host "LLM readiness monitor: .\jarvis.cmd llm -WatchLlm" -ForegroundColor Cyan
    } else {
      throw (
        "Docker did not become ready; preserving the last dispatcher ownership " +
        "record and refusing a partial start."
      )
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
      $services.bridge = @{
        port = 8765
        pid = $bridgePid
        reused = $false
        phase = "starting"
      }
      Save-LauncherState -Services $services
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
        phase = "ready"
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
        phase = "starting"
        pid = Start-BackendProcess `
          -FilePath "py.exe" `
          -Arguments @("-3.11", (Join-Path $RepoRoot "jarvis.py"), "--profile", $Profile, "serve", "--host", $backendBindHost, "--port", "8000") `
          -WorkingDirectory $RepoRoot `
          -Stdout (Join-Path $LogDir "backend.out.log") `
          -Stderr (Join-Path $LogDir "backend.err.log")
      }
      Save-LauncherState -Services $services
    }
  }

  if (-not $NoFrontend) {
    $frontendRebuilt = Ensure-FrontendReady
    if (Test-PortOpen -Port 3000) {
      if (-not (Test-ManagedPortOwner -Port 3000 -Service "frontend")) {
        throw "TCP 3000 is occupied by a process not managed by Jarvis. Stop it or choose another port."
      }
      $frontendBindingMismatch = -not (Test-FrontendBindingMatchesMode -Port 3000)
      if (
        $frontendRebuilt -or
        $frontendEnvironmentChanged -or
        $frontendBindingMismatch
      ) {
        $reason = if ($frontendRebuilt) {
          "the frontend build changed"
        } elseif ($frontendEnvironmentChanged) {
          "the frontend launch environment changed"
        } elseif ($script:LanMode) {
          "LAN binding is required"
        } else {
          "loopback-only binding is required"
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
        phase = "starting"
        pid = Start-ManagedProcess `
          -Name "frontend" `
          -FilePath "npm.cmd" `
          -Arguments $frontendArgs `
          -WorkingDirectory $FrontendRoot `
          -Stdout (Join-Path $LogDir "frontend.out.log") `
          -Stderr (Join-Path $LogDir "frontend.err.log")
      }
      Save-LauncherState -Services $services
    }
  }

  if ($telegramLaunchRequested) {
    # Publish an honest recoverable phase before the potentially long model warmup.
    # The bridge is intentionally absent here, so getUpdates cannot consume a turn.
    $services.telegram = @{
      skipped = "waiting-for-live-llm-readiness"
      realm_id = $script:TelegramRealmId
      bot_id = $script:TelegramBotId
    }
  } else {
    $services.telegram = @{
      skipped = if ($NoTelegram) {
        "disabled-by-launcher-option"
      } elseif ($NoBackend) {
        "backend-disabled"
      } else {
        "not-configured"
      }
    }
  }
  Save-LauncherState -Services $services

  $managedDispatcherNeedsReadiness = [bool](
    -not $NoDispatcher -and
    $services.ContainsKey("dispatcher") -and
    -not $services.dispatcher.ContainsKey("skipped")
  )
  if ($managedDispatcherNeedsReadiness -or $telegramLaunchRequested) {
    $readinessDeadlineSec = [int](
      (Get-ProfileCertification -SelectedProfile $Profile).readiness_deadline_sec
    )
    Write-Host "Waiting for live LLM completion readiness..." -ForegroundColor Yellow
    $readyLlm = Wait-LlmReady -TimeoutSec $readinessDeadlineSec
    if ($services.ContainsKey("dispatcher")) {
      $services.dispatcher.phase = [string]$readyLlm.phase
    }
    Save-LauncherState -Services $services
  }

  if ($telegramLaunchRequested) {
    # This block must stay after Wait-LlmReady: run() drains the durable inbox before
    # its first getUpdates call, and a warming model would otherwise spend retries.
    if (-not (Wait-BackendApiReady -TimeoutSec 30)) {
      throw "Telegram bridge requires the loopback backend API, but it did not become ready."
    }
    $telegramEntry = if ($previousState -and $previousState.services) {
      $previousState.services.telegram
    } else {
      $null
    }
    $telegramSnapshot = Get-ProcessSnapshot
    $reuseTelegram = [bool](
      -not $telegramEnvironmentChanged -and
      (Test-TelegramBridgeReuseState `
        -PreviousState $previousState `
        -Snapshot $telegramSnapshot)
    )
    if ($reuseTelegram) {
      $services.telegram = @{
        pid = [int]$telegramEntry.pid
        reused = $true
        realm_id = $script:TelegramRealmId
        bot_id = $script:TelegramBotId
        readiness = "live-llm+getMe+store"
        stderr = [string]$telegramEntry.stderr
      }
      Write-Host "Telegram bridge already passed live-LLM/getMe/store readiness." -ForegroundColor DarkYellow
    } else {
      Stop-ManagedTelegramProcesses -Reason "Telegram bridge replacement before start"
      $telegramStdout = Join-Path $LogDir "telegram-bridge.out.log"
      $telegramStderr = Join-Path $LogDir "telegram-bridge.err.log"
      $telegramStartedAt = (Get-Date).ToUniversalTime()
      $telegramPid = Start-ManagedProcess `
        -Name "Telegram bridge" `
        -FilePath "py.exe" `
        -Arguments @("-3.11", (Join-Path $RepoRoot "jarvis.py"), "--profile", $Profile, "telegram-bridge") `
        -WorkingDirectory $RepoRoot `
        -Stdout $telegramStdout `
        -Stderr $telegramStderr
      $services.telegram = @{
        pid = $telegramPid
        reused = $false
        phase = "starting"
        realm_id = $script:TelegramRealmId
        bot_id = $script:TelegramBotId
        stderr = $telegramStderr
      }
      Save-LauncherState -Services $services
      $telegramReady = Wait-TelegramBridgeReady `
        -ProcessId $telegramPid `
        -StderrPath $telegramStderr `
        -StartedAtUtc $telegramStartedAt `
        -TimeoutSec 45
      if (-not $telegramReady.ok) {
        $failedSnapshot = Get-ProcessSnapshot
        $failedProcess = $failedSnapshot |
          Where-Object { [int]$_.ProcessId -eq $telegramPid } |
          Select-Object -First 1
        if (Test-ManagedServiceProcess -ProcessInfo $failedProcess -Service "telegram") {
          Stop-ProcessTree `
            -ProcessId $telegramPid `
            -Reason "failed Telegram getMe/store readiness" `
            -Snapshot $failedSnapshot `
            -ProtectedProcessIds (Get-CurrentProcessFamilyIds) | Out-Null
        }
        throw ("Telegram bridge failed readiness: {0}. See {1}" -f $telegramReady.error, $telegramStderr)
      }
      $services.telegram = @{
        pid = $telegramPid
        reused = $false
        realm_id = $script:TelegramRealmId
        bot_id = $script:TelegramBotId
        readiness = "live-llm+getMe+store"
        stderr = $telegramStderr
      }
    }
    Save-LauncherState -Services $services
  }
  Start-Sleep -Seconds 3
  Show-JarvisStatus
}

function Stop-JarvisStack {
  Ensure-LauncherFolders
  Set-JarvisEnvironment -SelectedProfile $Profile -SkipTelegramInitialization
  Write-Banner

  $snapshot = Get-ProcessSnapshot
  $protected = Get-CurrentProcessFamilyIds
  $state = Read-LauncherState
  if ($state -and $state.services) {
    foreach ($service in @("frontend", "backend", "bridge", "telegram")) {
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

  $dispatcherFinalization = Invoke-WithDispatcherOperationLock -Action {
    $dispatcherState = Read-LauncherState
    $stateFingerprint = Get-LauncherControlFileFingerprint -Path $StateFile
    $journalFingerprint = Get-LauncherControlFileFingerprint `
      -Path $DispatcherOwnershipJournal
    $dispatcherContainer = Get-DispatcherContainerSnapshot
    $pendingOwnershipJournalPresent = Test-Path -LiteralPath $DispatcherOwnershipJournal
    $pendingOwnershipJournal = Read-DispatcherOwnershipJournal
    if ($pendingOwnershipJournalPresent -and -not $pendingOwnershipJournal) {
      throw "Dispatcher ownership journal is invalid; recovery state was preserved."
    }
    $ownedDispatcherProof = Get-LauncherOwnedDispatcherProof `
      -State $dispatcherState `
      -Snapshot $dispatcherContainer
    $dispatcherStop = $null
    if ($ownedDispatcherProof) {
      $dispatcherStop = Stop-DispatcherRuntime `
        -ExpectedContainerId ([string]$ownedDispatcherProof.container_id) `
        -ExpectedOperationNonce ([string]$ownedDispatcherProof.operation_nonce) `
        -DispatcherOperationLockHeld
    } else {
      Write-Host "Preserving LLM runtime because exact launcher ID/nonce ownership was not proven." -ForegroundColor Cyan
    }

    if ($ownedDispatcherProof -and -not [bool]$dispatcherStop.ok) {
      throw (
        "Dispatcher stop was not proven; launcher ownership state was preserved " +
        "for recovery (reason={0})." -f [string]$dispatcherStop.reason
      )
    }
    if (
      -not $ownedDispatcherProof -and
      $pendingOwnershipJournalPresent -and
      -not (
        [bool]$dispatcherContainer.identity_known -and
        -not [bool]$dispatcherContainer.exists -and
        [string]$pendingOwnershipJournal.phase -eq "intent" -and
        [string]::IsNullOrWhiteSpace([string]$pendingOwnershipJournal.container_id) -and
        [string]::IsNullOrWhiteSpace([string]$pendingOwnershipJournal.previous_container_id)
      )
    ) {
      throw (
        "Dispatcher ownership could not be proven while a launcher-owned mutation " +
        "journal exists; recovery state was preserved."
      )
    }

    Invoke-WithLauncherStateLock -Action {
      $lockedStateFingerprint = Get-LauncherControlFileFingerprint -Path $StateFile
      $lockedJournalFingerprint = Get-LauncherControlFileFingerprint `
        -Path $DispatcherOwnershipJournal
      if (
        $lockedStateFingerprint -ne $stateFingerprint -or
        $lockedJournalFingerprint -ne $journalFingerprint
      ) {
        throw (
          "Launcher state/journal changed during dispatcher stop; exact CAS cleanup " +
          "was refused."
        )
      }
      if ($ownedDispatcherProof) {
        $postStopDispatcher = Get-DispatcherContainerSnapshot
        if (
          -not [bool]$postStopDispatcher.identity_known -or
          [bool]$postStopDispatcher.exists
        ) {
          throw (
            "Dispatcher absence changed before state cleanup; ownership records " +
            "were preserved."
          )
        }
      }
      if (Test-Path -LiteralPath $StateFile) {
        Remove-Item -LiteralPath $StateFile -Force
      }
      if (Test-Path -LiteralPath $DispatcherOwnershipJournal) {
        Remove-Item -LiteralPath $DispatcherOwnershipJournal -Force
      }
    }
    return @{
      stopped = [bool]($ownedDispatcherProof -and $dispatcherStop.ok)
      owned = [bool]$ownedDispatcherProof
    }
  }
  if ([bool]$dispatcherFinalization.stopped) {
    Stop-PortOwner -Port 8001 -SkipDockerEngineOwner
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

function Show-ManagedProcessRow {
  param(
    [string]$Name,
    [string]$Service
  )

  $processInfo = Get-ProcessSnapshot |
    Where-Object { Test-ManagedServiceProcess -ProcessInfo $_ -Service $Service } |
    Select-Object -First 1
  if ($processInfo) {
    Write-Host ("| {0,-14} | {1,-8} | {2,-11} | {3,-26} |" -f $Name, "online", ("pid {0}" -f $processInfo.ProcessId), "long polling") -ForegroundColor Green
  } else {
    Write-Host ("| {0,-14} | {1,-8} | {2,-11} | {3,-26} |" -f $Name, "offline", "-", "long polling") -ForegroundColor DarkGray
  }
}

function Get-EffectiveLanMode {
  return $false
}

function Get-EffectivePublicHost {
  return "127.0.0.1"
}

function Show-JarvisStatus {
  Set-JarvisEnvironment -SelectedProfile $Profile -SkipTelegramInitialization
  $statusHost = Get-EffectivePublicHost
  Write-Banner
  Write-Host "+----------------+----------+-------------+----------------------------+" -ForegroundColor DarkCyan
  Write-Host "| Service        | State    | Process     | URL                        |" -ForegroundColor Cyan
  Write-Host "+----------------+----------+-------------+----------------------------+" -ForegroundColor DarkCyan
  Show-ServiceRow -Name "Backend" -Port 8000 -Url "http://127.0.0.1:8000"
  Show-ServiceRow -Name "Frontend" -Port 3000 -Url ("http://{0}:3000" -f $statusHost)
  Show-ServiceRow -Name "Host bridge" -Port 8765 -Url "http://127.0.0.1:8765"
  Show-ManagedProcessRow -Name "Telegram bot" -Service "telegram"
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
    @{ Label = "Telegram bot log"; Value = Join-Path $LogDir "telegram-bridge.err.log"; Hint = "Telegram bridge stderr" },
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
    # Render the Python profile registry directly; shell policy must never drift from
    # config.py when a model is added or re-certified.
    $catalog = Get-ProfileCatalog
    $visibleProfiles = @(
      $catalog.Values |
        Where-Object {
          ([bool]$_.menu_visible -and -not [bool]$_.requires_experimental_opt_in) -or
          [bool]$script:AllowExperimentalProfiles
        } |
        Sort-Object @{ Expression = { -[int][bool]$_.default_recommended } }, name
    )
    $profiles = @(
      foreach ($item in $visibleProfiles) {
        $traits = @([string]$item.certification)
        if ([bool]$item.vision_capable) { $traits += "vision" }
        if ([bool]$item.research_only) { $traits += "research-only" }
        if ([bool]$item.requires_experimental_opt_in) { $traits += "requires confirmation" }
        $recommended = if ([bool]$item.default_recommended) { " (recommended)" } else { "" }
        @{
          Label = "$($item.title) - $(([string]$item.certification).ToUpper())$recommended"
          Value = [string]$item.name
          Hint = (
            "$($traits -join ' | ') | $($item.model_dir_name) | " +
            "readiness deadline $([int]$item.readiness_deadline_sec)s"
          )
        }
      }
    )
    $profileChoice = Select-Menu -Title "Select LLM profile" -Items $profiles
    if (-not $profileChoice) {
      return
    }
    $script:Profile = $profileChoice.Value
    $selectedProfile = Get-ProfileCertification -SelectedProfile $script:Profile
    if ([bool]$selectedProfile.requires_experimental_opt_in) {
      if (-not $script:IUnderstandExperimentalProfile) {
        $confirm = Select-Menu -Title "Confirm experimental/unsupported profile" -Items @(
          @{ Label = "Cancel - use recommended profile"; Value = "cancel"; Hint = "recommended" },
          @{ Label = "I understand this profile is experimental/research-only"; Value = "confirm"; Hint = "advanced opt-in" }
        )
        if (-not $confirm -or $confirm.Value -ne "confirm") {
          $script:Profile = Get-DefaultProfileName
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

if ([string]::IsNullOrWhiteSpace($script:Profile)) {
  $script:Profile = Get-DefaultProfileName
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
