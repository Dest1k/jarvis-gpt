from __future__ import annotations

import hashlib
import importlib.util
import json
import re
import shutil
import subprocess
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
VLLM_PATCHER_PATH = REPO_ROOT / "docker/vllm-asyncio/patch_serve.py"


def _load_vllm_patcher():
    spec = importlib.util.spec_from_file_location("jarvis_vllm_serve_patcher", VLLM_PATCHER_PATH)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


VLLM_PATCHER = _load_vllm_patcher()

_DOTENV_ASSIGNMENT_RE = re.compile(
    r"^[ \t]*(?:export[ \t]+)?(?P<name>[A-Za-z_][A-Za-z0-9_]*)[ \t]*=",
    flags=re.MULTILINE,
)

_VLLM_SERVE_FIXTURE = b"""\
import argparse
import uvloop


async def run_server(args):
    return args


def main(args):
    if args:
        if args.enabled:
            uvloop.run(run_server(args))
"""


def _read(relative_path: str) -> str:
    return (REPO_ROOT / relative_path).read_text(encoding="utf-8")


def test_launcher_is_ascii_safe_for_windows_powershell_5() -> None:
    launcher = (REPO_ROOT / "scripts/jarvis-launcher.ps1").read_bytes()

    assert launcher.isascii(), (
        "BOM-less Windows PowerShell 5 scripts must stay ASCII; UTF-8 punctuation can be "
        "decoded as parser-significant smart quotes."
    )


def test_backend_image_installs_sandboxed_chromium_and_drops_root() -> None:
    dockerfile = _read("backend/Dockerfile")
    entrypoint = _read("backend/docker-entrypoint.sh")

    assert "rm -rf /var/lib/apt/lists/*" in dockerfile
    assert "chromium-sandbox" in dockerfile
    assert "PLAYWRIGHT_BROWSERS_PATH=/ms-playwright" in dockerfile
    assert "playwright install --with-deps --only-shell chromium" in dockerfile
    assert "p.chromium.launch(headless=True)" in dockerfile
    assert "chromium_headless_shell-*" in dockerfile
    assert "Acquire::Retries" in dockerfile
    assert "--no-sandbox" not in dockerfile
    assert 'ENTRYPOINT ["jarvis-docker-entrypoint"]' in dockerfile
    assert "CMD gosu jarvis python -c" in dockerfile
    assert 'exec gosu jarvis "$@"' in entrypoint
    assert "Refusing unsafe JARVIS_HOME outside /runtime" in entrypoint
    assert "Refusing unsafe JARVIS_MODEL_ROOT outside JARVIS_HOME" in entrypoint
    assert "Refusing unexpected HOME" in entrypoint


def test_bundled_web_surfer_dependencies_are_pinned_for_native_and_container_runs() -> None:
    requirements = _read("backend/requirements.txt")
    pyproject = _read("pyproject.toml")

    for dependency in (
        "beautifulsoup4==4.15.0",
        "lxml==6.1.1",
        "playwright==1.61.0",
        "playwright-stealth==2.0.3",
    ):
        assert dependency in requirements
        assert f'"{dependency}"' in pyproject


def test_compose_defaults_to_loopback_and_propagates_build_contract() -> None:
    compose = _read("docker-compose.yml")

    assert "jarvis-compose-loopback-only" not in compose
    assert '${JARVIS_API_BIND_ADDRESS:-127.0.0.1}' in compose
    assert '"127.0.0.1:3000:3000"' in compose
    assert "JARVIS_FRONTEND_BIND_ADDRESS" not in compose
    assert "NEXT_PUBLIC_JARVIS_API_URL" not in compose
    assert "NEXT_PUBLIC_JARVIS_API_TOKEN" not in compose
    assert "JARVIS_BACKEND_URL: http://backend:8000" in compose
    assert "JARVIS_CORS_ORIGINS:" in compose
    assert "JARVIS_API_REQUIRE_TOKEN_ON_LOOPBACK:" in compose
    assert compose.count("${JARVIS_API_TOKEN:?") == 2
    assert "${JARVIS_QWEN_MODEL_PATH:?" in compose
    assert "JARVIS_QWEN_MODEL_PATH:-/models/" not in compose
    assert "${JARVIS_QWEN_EXTRA_ARGS:-}" in compose
    assert (
        "com.jarvis-gpt.dispatcher.operation-nonce: "
        "${JARVIS_DISPATCHER_OPERATION_NONCE:-unmanaged}"
    ) in compose
    assert "shm_size: 512m" in compose
    assert compose.count("no-new-privileges:true") == 2
    assert "seccomp=./backend/chromium-seccomp.json" in compose
    assert "cap_drop:" in compose
    assert compose.count("read_only: true") >= 3


def test_qwen_vllm_derivative_is_digest_pinned_and_patches_only_http_loop() -> None:
    dockerfile = _read("docker/vllm-asyncio/Dockerfile")

    # Keep the derivative mechanically auditable: no continuation/heredoc can hide
    # commands, parser directives cannot change how the file is read, and every
    # non-comment line must be one of the three reviewed instructions below.
    assert "\\\n" not in dockerfile
    assert not re.search(r"^[ \t]*#[ \t]*(?:escape|syntax)[ \t]*=", dockerfile, re.MULTILINE)
    instruction_lines = [
        line.strip()
        for line in dockerfile.splitlines()
        if line.strip() and not line.lstrip().startswith("#")
    ]
    instructions = []
    for line in instruction_lines:
        match = re.fullmatch(r"(?i)([a-z][a-z0-9]*)[ \t]+(.+)", line)
        assert match is not None, f"unparsed Dockerfile content: {line!r}"
        instructions.append((match.group(1).upper(), match.group(2)))

    assert len(instructions) == 3
    from_instruction, copy_instruction, run_instruction = instructions
    assert from_instruction == (
        "FROM",
        "vllm/vllm-openai:v0.25.1@sha256:"
        "e4f88a835143cd22aee2397a26ec6bb80b3a4a6fe0c882bcbc63822904766089",
    )
    assert copy_instruction == (
        "COPY",
        "docker/vllm-asyncio/patch_serve.py "
        "/usr/local/share/jarvis/patch_vllm_serve.py",
    )
    assert run_instruction[0] == "RUN"
    assert json.loads(run_instruction[1]) == [
        "python3",
        "/usr/local/share/jarvis/patch_vllm_serve.py",
        "/usr/local/lib/python3.12/dist-packages/vllm/entrypoints/cli/serve.py",
    ]


def test_vllm_serve_patcher_produces_only_the_reviewed_byte_changes(monkeypatch) -> None:
    source_sha256 = hashlib.sha256(_VLLM_SERVE_FIXTURE).hexdigest()
    monkeypatch.setattr(VLLM_PATCHER, "EXPECTED_SOURCE_SHA256", source_sha256)

    patched = VLLM_PATCHER.patch_source(_VLLM_SERVE_FIXTURE)

    expected = _VLLM_SERVE_FIXTURE.replace(
        b"import argparse\n",
        b"import argparse\nimport asyncio\n",
    ).replace(
        b"            uvloop.run(run_server(args))\n",
        b"            asyncio.run(run_server(args))\n",
    )
    assert patched == expected


def test_vllm_serve_patcher_rejects_hash_mismatch_without_writing(tmp_path: Path) -> None:
    target = tmp_path / "serve.py"
    target.write_bytes(_VLLM_SERVE_FIXTURE)

    with pytest.raises(VLLM_PATCHER.PatchError, match="unexpected upstream serve.py SHA256"):
        VLLM_PATCHER.patch_file(target)

    assert target.read_bytes() == _VLLM_SERVE_FIXTURE


@pytest.mark.parametrize(
    ("source", "message"),
    [
        (
            _VLLM_SERVE_FIXTURE.replace(
                b"import uvloop\n",
                b"import argparse\nimport uvloop\n",
            ),
            "expected exactly one argparse import anchor; found 2",
        ),
        (
            _VLLM_SERVE_FIXTURE.replace(
                b"            uvloop.run(run_server(args))\n",
                b"            return None\n",
            ),
            "expected exactly one uvloop runner; found 0",
        ),
        (
            _VLLM_SERVE_FIXTURE.replace(
                b"            uvloop.run(run_server(args))\n",
                b"            uvloop.run(run_server(args))\n"
                b"            uvloop.run(run_server(args))\n",
            ),
            "expected exactly one uvloop runner; found 2",
        ),
    ],
)
def test_vllm_serve_patcher_rejects_missing_or_ambiguous_anchors(
    source: bytes,
    message: str,
    monkeypatch,
) -> None:
    monkeypatch.setattr(VLLM_PATCHER, "EXPECTED_SOURCE_SHA256", hashlib.sha256(source).hexdigest())
    with pytest.raises(VLLM_PATCHER.PatchError, match=re.escape(message)):
        VLLM_PATCHER.patch_source(source)


def test_example_environment_does_not_override_profile_specific_vllm_image() -> None:
    example = _read(".env.example")

    assignments = {match.group("name") for match in _DOTENV_ASSIGNMENT_RE.finditer(example)}
    assert "JARVIS_VLLM_IMAGE" not in assignments


def test_dotenv_assignment_detection_covers_export_and_whitespace_forms() -> None:
    assignments = """
JARVIS_VLLM_IMAGE=first
  JARVIS_VLLM_IMAGE = second
export JARVIS_VLLM_IMAGE=third
\texport\tJARVIS_VLLM_IMAGE \t= fourth
# JARVIS_VLLM_IMAGE=commented
  # export JARVIS_VLLM_IMAGE=also_commented
"""

    matches = [match.group("name") for match in _DOTENV_ASSIGNMENT_RE.finditer(assignments)]
    assert matches == ["JARVIS_VLLM_IMAGE"] * 4


def test_chromium_seccomp_profile_keeps_default_deny_and_allows_namespaces() -> None:
    profile = _read("backend/chromium-seccomp.json")

    assert '"defaultAction": "SCMP_ACT_ERRNO"' in profile
    assert '"comment": "Allow create user namespaces"' in profile
    for syscall in ('"clone"', '"setns"', '"unshare"'):
        assert syscall in profile


def test_launcher_exposes_only_authenticated_ui_to_explicit_lan() -> None:
    launcher = _read("scripts/jarvis-launcher.ps1")
    dispatcher_script = _read("scripts/dispatcher.ps1")
    dev_script = _read("scripts/dev.ps1")
    frontend_package = json.loads(_read("frontend/package.json"))
    frontend_dockerfile = _read("frontend/Dockerfile")
    lan_server = _read("frontend/lan-server.mjs")

    assert '[string]$LanSubnet = "192.168.31.0/24"' in launcher
    assert "$script:LanMode = [bool]$Lan" in launcher
    assert "Initialize-LanModeFromState" in launcher
    assert "JARVIS_UI_LAN_BIND_ADDRESS" in launcher
    assert "JARVIS_UI_ALLOWED_CIDRS" in launcher
    assert "-LocalAddress $lanIp" in launcher
    assert "-RemoteAddress $script:LanSubnet" in launcher
    assert '"lan-server.mjs"' in launcher
    assert '$env:JARVIS_API_HOST = "127.0.0.1"' in launcher
    assert '$env:JARVIS_BACKEND_URL = "http://127.0.0.1:8000"' in launcher
    assert frontend_package["scripts"]["dev"] == "next dev --hostname 127.0.0.1"
    assert frontend_package["scripts"]["start"] == "next start --hostname 127.0.0.1"
    assert frontend_package["scripts"]["test:network-access"] == (
        "node tests/network-access.mjs"
    )
    assert "npm run dev -- --hostname 127.0.0.1" in dev_script
    assert (
        'CMD ["node", "node_modules/next/dist/bin/next", "start", '
        '"--hostname", "0.0.0.0"]'
    ) in frontend_dockerfile
    assert "temporarily disabled" not in launcher
    assert 'const loopbackAddress = "127.0.0.1"' in lan_server
    assert "request.socket.remoteAddress" in lan_server
    assert "isIPv4Allowed" in lan_server
    assert "Client network is not allowed" in lan_server
    assert "Command Center did not establish the required loopback" in launcher
    assert 'Label = "Start app without LLM"' in launcher
    assert '"app" { $script:NoDispatcher = $true; Start-JarvisStack }' in launcher
    assert "function Get-LlmStartDecision" in launcher
    assert 'language_model_only = [bool]($Command -contains "--language-model-only")' in launcher
    assert 'Name "max-num-batched-tokens"' in launcher
    assert 'return "reuse"' in launcher
    assert 'return "replace"' in launcher
    assert 'return "conflict"' in launcher
    assert "runtime_matches_desired" in launcher
    assert "Replacing mismatched dispatcher" in launcher
    assert "function Set-DispatcherComposeModelPath" in launcher
    assert launcher.count("Set-DispatcherComposeModelPath") >= 3
    assert "$env:JARVIS_QWEN_MODEL_PATH" in dispatcher_script
    assert "LLM is already started" in launcher
    assert "started_by_launcher = $true" in launcher
    assert "function Test-LauncherOwnsDispatcher" in launcher
    assert "function Test-ReusedDispatcherOwnership" in launcher
    assert "function Test-ManagedJarvisPort" in launcher
    assert "function Get-AlreadyRunningStackServices" in launcher
    assert "function Test-FrontendBindingMatchesMode" in launcher
    assert '$loopbackAddresses = @("127.0.0.1", "::1")' in launcher
    assert launcher.count("-not (Test-FrontendBindingMatchesMode -Port 3000)") == 4
    assert "-not $NoFrontend -and" in launcher
    assert "$frontendBindingMismatch" in launcher
    assert '"loopback-only binding is required"' in launcher
    assert "function Test-BridgeActionReady" in launcher
    assert "already-running status without" in launcher
    assert "mutating CLI verification" in launcher
    assert "skipping mutating init" in launcher
    assert "Stopping managed backend before mutating init" in launcher
    assert "Cannot start/replace dispatcher while the managed API owns executive" in launcher
    assert launcher.index("Get-AlreadyRunningStackServices") < launcher.index(
        'Arguments @("-3.11", ".\\jarvis.py", "--profile", $Profile, "init")'
    )
    init_args = 'Arguments @("-3.11", ".\\jarvis.py", "--profile", $Profile, "init")'
    assert launcher.count(init_args) == 1
    assert 'container_id = [string]$llmReadiness.container.id' in launcher
    assert '$phase = "external-ready"' in launcher
    assert "Preserving LLM runtime" in launcher
    assert "Get-OrCreateApiToken" in launcher
    assert "Protect-ApiTokenFile" in launcher
    assert "Get-FrontendEnvironmentSha256" in launcher
    assert "JARVIS_UI_SESSION_SECRET" in launcher
    assert "JARVIS_TRUST_PROXY_HEADERS" in launcher
    assert "JARVIS_TELEGRAM_SESSION_TTL_SECONDS" in launcher
    assert "JARVIS_TELEGRAM_USER_RATE_LIMIT_PER_MINUTE" in launcher
    assert '(Join-Path $FrontendRoot "lib")' in launcher
    assert "JARVIS_EXECUTION_CAPABILITIES_FILE" in launcher
    assert "JARVIS_BRIDGE_APP_PATHS_JSON" in launcher
    assert '$env:JARVIS_API_HOST = "127.0.0.1"' in launcher
    assert "password source:" not in launcher
    assert "Command Center Basic auth" not in dev_script
    assert 'Stop-PortOwner -Port 3000 -ManagedOnly -Service "frontend"' in launcher
    assert 'Stop-PortOwner -Port 8000 -ManagedOnly -Service "backend"' in launcher
    assert 'Stop-PortOwner -Port 8765 -ManagedOnly -Service "bridge"' in launcher
    assert 'Invoke-HttpProbe -Uri "http://127.0.0.1:8765/health"' in launcher
    assert '[string]$bridgeHealth.data.contract -eq "action.v1"' in launcher
    assert "function Invoke-BridgeCapabilitiesProbe" in launcher
    assert '-Headers @{ Authorization = "Bearer $token" }' in launcher
    assert 'action = "capabilities"' in launcher
    assert "function Wait-BridgeReady" in launcher
    assert "Wait-BridgeReady -TimeoutSec 15" in launcher
    assert '$BridgePolicyRevision = "native-app-v3"' in launcher
    assert "data.policy_revision -eq $BridgePolicyRevision" in launcher
    assert "data.app_paths_sha256 -eq $expectedAppPathsSha256" in launcher
    assert "Restarting stale or unauthenticated host bridge" in launcher
    assert "failed authenticated action.v1 readiness" in launcher
    assert "function ConvertTo-WindowsCommandLineArgument" in launcher
    assert "ConvertTo-WindowsCommandLineArgument -Argument" in launcher
    assert "-ArgumentList $Arguments" not in launcher
    assert 'Join-Path $RepoRoot "jarvis.py"' in launcher
    assert '"jarvis-gpt-command-center"' not in launcher


def test_launcher_repeat_start_is_idempotent_contract() -> None:
    """SPARK-0014: warm start must not contest the API primary lease."""

    launcher = _read("scripts/jarvis-launcher.ps1")

    # Already-running path must short-circuit before mutating init.
    already_fn = launcher.index("function Get-AlreadyRunningStackServices")
    start_fn = launcher.index("function Start-JarvisStack")
    init_call = launcher.index(
        'Arguments @("-3.11", ".\\jarvis.py", "--profile", $Profile, "init")',
        start_fn,
    )
    already_call = launcher.index("Get-AlreadyRunningStackServices", start_fn)
    assert already_fn < start_fn
    assert already_call < init_call
    already_fn_body = launcher[already_fn:start_fn]
    assert "[bool]$TelegramEnvironmentChanged" in already_fn_body
    assert "if (-not $NoDispatcher -or $telegramFastPathRequested)" in already_fn_body
    assert "if (-not [bool]$llmReadiness.ready)" in already_fn_body
    assert 'if ($llmStartDecision -ne "reuse")' in already_fn_body
    assert (
        "$BackendEnvironmentChanged -or\n"
        "    $FrontendEnvironmentChanged -or\n"
        "    $TelegramEnvironmentChanged"
    ) in already_fn_body
    already_call_body = launcher[
        already_call : launcher.index(
            "if ($null -ne $alreadyRunningServices)", already_call
        )
    ]
    assert "-TelegramEnvironmentChanged $telegramEnvironmentChanged" in already_call_body
    assert "reporting already-running status without" in launcher
    assert "mutating CLI verification" in launcher
    start_body = launcher[start_fn : launcher.index("function Stop-JarvisStack", start_fn)]
    state_save = start_body.index("Save-LauncherState -Services $services")
    readiness_wait = start_body.index("Wait-LlmReady -TimeoutSec $readinessDeadlineSec")
    final_status = start_body.index("Show-JarvisStatus", readiness_wait)
    assert state_save < readiness_wait < final_status
    assert '-not $services.dispatcher.ContainsKey("skipped")' in start_body
    # Mutating init is gated on live managed backend / lease ownership.
    assert "skipping mutating init" in launcher
    assert "Live managed backend already owns executive state" in launcher
    # Env-change restart releases the lease before init.
    assert "Stopping managed backend before mutating init" in launcher
    # No second init call after lease-sensitive paths.
    assert launcher.count(
        'Arguments @("-3.11", ".\\jarvis.py", "--profile", $Profile, "init")'
    ) == 1


def test_launcher_serializes_state_writes_with_backend_sync() -> None:
    launcher = _read("scripts/jarvis-launcher.ps1")
    lock_start = launcher.index("function Invoke-WithLauncherStateLock")
    save_start = launcher.index("function Save-LauncherState")
    read_start = launcher.index("function Read-LauncherState")
    lock_body = launcher[lock_start:save_start]
    save_body = launcher[save_start:read_start]

    assert 'Join-Path $StateDir "launcher-state.lock"' in lock_body
    assert "$candidate.Lock(0, 1)" in lock_body
    assert "$stream.Unlock(0, 1)" in lock_body
    assert "Invoke-WithLauncherStateLock -Action" in save_body
    assert "Get-DispatcherContainerSnapshot" in save_body
    assert "$Services.dispatcher.container_id" in save_body
    assert "Cannot save launcher ownership" in save_body
    assert "Set-Content -Path $StateFile" not in save_body
    assert "$stream.Flush($true)" in save_body
    assert "[System.IO.File]::Replace($temporary, $StateFile, $backup)" in save_body
    assert "launcher-state.json.bak." in save_body
    assert "File.Replace($temporary, $StateFile, $null)" not in save_body
    assert "[System.IO.File]::Move($temporary, $StateFile)" in save_body
    assert "launcher-state.json.tmp." in save_body
    assert "$Services.dispatcher.container_id =" not in save_body

    operation_lock_start = launcher.index("function Invoke-WithDispatcherOperationLock")
    operation_lock_body = launcher[operation_lock_start:save_start]
    assert 'Join-Path $StateDir "dispatcher-operation.lock"' in operation_lock_body
    assert "$candidate.Lock(0, 1)" in operation_lock_body
    assert "$stream.Unlock(0, 1)" in operation_lock_body
    stop_start = launcher.index("function Stop-DispatcherRuntime")
    stop_end = launcher.index("function Invoke-JarvisCommand", stop_start)
    assert "Invoke-WithDispatcherOperationLock -Action" in launcher[stop_start:stop_end]

    ownership_start = launcher.index("function Test-LauncherOwnsDispatcher")
    ownership_end = launcher.index("function Test-ReusedDispatcherOwnership")
    ownership_body = launcher[ownership_start:ownership_end]
    missing_state_guard = ownership_body.index(
        "if (-not $State -or -not $State.services)"
    )
    assert ownership_body.index("return $false", missing_state_guard) > missing_state_guard
    assert "^[0-9a-fA-F]{64}$" in ownership_body
    assert "^[0-9a-fA-F]{32}$" in ownership_body or "operation_nonce" in ownership_body

    # PowerShell mangles nested quotes in native docker --format templates, so the
    # snapshot must read the operation nonce via JSON labels (same as dispatcher.py).
    snapshot_start = launcher.index("function Get-DispatcherContainerSnapshot")
    snapshot_end = launcher.index("function Get-DispatcherLogSignals", snapshot_start)
    snapshot_body = launcher[snapshot_start:snapshot_end]
    assert "{{json .Config.Labels}}" in snapshot_body
    assert 'com.jarvis-gpt.dispatcher.operation-nonce' in snapshot_body
    assert 'index .Config.Labels "com.jarvis-gpt.dispatcher.operation-nonce"' not in (
        snapshot_body
    )

    stop_stack = launcher[launcher.index("function Stop-JarvisStack") :]
    assert "Get-LauncherControlFileFingerprint" in stop_stack
    assert "DispatcherOperationLockHeld" in stop_stack
    assert "Invoke-WithDispatcherOperationLock" in stop_stack
    assert "Remove-Item -LiteralPath $StateFile -Force" in stop_stack


def test_launcher_writes_starting_state_before_follow_on_start_can_fail() -> None:
    launcher = _read("scripts/jarvis-launcher.ps1")
    start = launcher.index("function Start-JarvisStack")
    stop = launcher.index("function Stop-JarvisStack", start)
    body = launcher[start:stop]
    dispatcher_up = body.index('"dispatcher-up",')
    starting_phase = body.index('phase = "starting"', dispatcher_up)
    provisional_save = body.index("Save-LauncherState -Services $services", starting_phase)
    bridge_start = body.index('-Name "host bridge"', provisional_save)
    backend_start = body.index("pid = Start-BackendProcess", provisional_save)
    frontend_start = body.index('-Name "frontend"', provisional_save)

    assert dispatcher_up < starting_phase < provisional_save
    assert provisional_save < bridge_start < backend_start < frontend_start
    assert "^[0-9a-fA-F]{64}$" in body[dispatcher_up:provisional_save]
    assert "^[0-9a-fA-F]{32}$" in body[dispatcher_up:provisional_save]


def test_launcher_atomic_state_replace_works_in_windows_powershell() -> None:
    powershell = shutil.which("powershell.exe") or shutil.which("pwsh")
    if powershell is None:
        pytest.skip("PowerShell is required for the launcher behavior regression")

    script = r"""
$path = Join-Path (Get-Location) "scripts\jarvis-launcher.ps1"
$tokens = $null
$errors = $null
$ast = [System.Management.Automation.Language.Parser]::ParseFile(
  $path,
  [ref]$tokens,
  [ref]$errors
)
if ($errors.Count -ne 0) {
  throw ($errors | ForEach-Object { $_.Message } | Out-String)
}
foreach ($name in @("Invoke-WithLauncherStateLock", "Save-LauncherState")) {
  $functionAst = $ast.Find({
    param($node)
    $node -is [System.Management.Automation.Language.FunctionDefinitionAst] -and
      $node.Name -eq $name
  }, $true)
  if (-not $functionAst) { throw "$name was not found" }
  Invoke-Expression $functionAst.Extent.Text
}

$global:StateDir = Join-Path `
  ([IO.Path]::GetTempPath()) `
  ("jarvis-state-" + [guid]::NewGuid().ToString("N"))
$global:StateFile = Join-Path $StateDir "launcher-state.json"
$global:Profile = "qwen36-vl"
$global:HomePath = "D:\jarvis"
$script:LanMode = $false
New-Item -ItemType Directory -Path $StateDir -Force | Out-Null
[IO.File]::WriteAllText($StateFile, '{"old":true}', [Text.UTF8Encoding]::new($false))
function Get-LanIPv4 { return $null }
function Get-FrontendUrl { return "http://127.0.0.1:3000" }
function Get-BackendUrl { return "http://127.0.0.1:8000" }
function Get-BackendEnvironmentSha256 { return "backend" }
function Get-FrontendEnvironmentSha256 { return "frontend" }
function Get-TelegramEnvironmentSha256 { return "telegram" }

try {
  Save-LauncherState -Services @{ backend = @{ pid = 1234 } }
  $saved = Get-Content -LiteralPath $StateFile -Raw | ConvertFrom-Json
  if ($saved.profile -ne "qwen36-vl" -or [int]$saved.services.backend.pid -ne 1234) {
    throw "atomic state replacement did not persist the new state"
  }
  $leftovers = @(Get-ChildItem -LiteralPath $StateDir -Force | Where-Object {
    $_.Name -like "launcher-state.json.tmp.*" -or
    $_.Name -like "launcher-state.json.bak.*"
  })
  if ($leftovers.Count -ne 0) {
    throw "atomic state replacement left temporary artifacts"
  }
} finally {
  Remove-Item -LiteralPath $StateDir -Recurse -Force -ErrorAction SilentlyContinue
}
Write-Output "OK"
"""
    completed = subprocess.run(  # noqa: S603 - resolved interpreter, fixed script
        [powershell, "-NoProfile", "-NonInteractive", "-Command", script],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        timeout=20,
        check=False,
    )
    assert completed.returncode == 0, completed.stdout + completed.stderr
    assert completed.stdout.strip() == "OK"


def test_launcher_dispatcher_identity_and_stop_are_container_id_cas() -> None:
    powershell = shutil.which("powershell.exe") or shutil.which("pwsh")
    if powershell is None:
        pytest.skip("PowerShell is required for the launcher behavior regression")

    script = r"""
$path = Join-Path (Get-Location) "scripts\jarvis-launcher.ps1"
$tokens = $null
$errors = $null
$ast = [System.Management.Automation.Language.Parser]::ParseFile(
  $path,
  [ref]$tokens,
  [ref]$errors
)
if ($errors.Count -ne 0) {
  throw ($errors | ForEach-Object { $_.Message } | Out-String)
}
foreach ($name in @(
  "Get-DispatcherContainerSnapshot",
  "Test-DispatcherContainerIdAbsent",
  "Stop-DispatcherRuntime"
)) {
  $functionAst = $ast.Find({
    param($node)
    $node -is [System.Management.Automation.Language.FunctionDefinitionAst] -and
      $node.Name -eq $name
  }, $true)
  if (-not $functionAst) { throw "$name was not found" }
  Invoke-Expression $functionAst.Extent.Text
}

$script:FullId = ("a" * 64)
$script:Nonce = ("c" * 32)
$script:DockerCalls = @()
$script:SnapshotMode = $false
$script:ContainerPresent = $true
$script:RemoveFails = $false
function Get-Command {
  param([string]$Name, [object]$ErrorAction)
  if ($Name -eq "docker") { return [pscustomobject]@{ Source = "Invoke-FakeDocker" } }
  Microsoft.PowerShell.Core\Get-Command @PSBoundParameters
}
function Invoke-FakeDocker {
  $joined = ($args -join " ")
  $script:DockerCalls += ,@($args)
  if ($args.Count -gt 0 -and $args[0] -eq "rm") {
    if ($script:RemoveFails) {
      $global:LASTEXITCODE = 1
      return "synthetic rm failure"
    }
    $script:ContainerPresent = $false
    $global:LASTEXITCODE = 0
    return
  }
  if ($args.Count -gt 0 -and $args[0] -eq "ps") {
    $global:LASTEXITCODE = 0
    return '{"State":"running","Status":"Up","Image":"jarvis/test:latest","ID":"aaaaaaaaaaaa"}'
  }
  if ($args.Count -gt 0 -and $args[0] -eq "inspect") {
    if (-not $script:ContainerPresent) {
      $global:LASTEXITCODE = 1
      return "Error: No such object"
    }
    if ($joined -match '\{\{\.Id\}\}') {
      $global:LASTEXITCODE = 0
      return $script:FullId
    }
    # Launcher must read the nonce via JSON labels (not nested-quoted index templates).
    if ($joined -match 'json \.Config\.Labels') {
      $global:LASTEXITCODE = 0
      return (
        @{ "com.jarvis-gpt.dispatcher.operation-nonce" = $script:Nonce } |
          ConvertTo-Json -Compress
      )
    }
    if ($joined -match 'operation-nonce') {
      throw "legacy nested-quoted operation-nonce docker format must not be used"
    }
    if ($joined -match 'Config\.Cmd' -or $joined -match 'json \.Config\.Cmd') {
      $global:LASTEXITCODE = 0
      return '["--model","/models/test"]'
    }
    $global:LASTEXITCODE = 0
    return ""
  }
}
function Test-DockerReady { return @{ ready = $true; error = "" } }
function Get-DispatcherContainerRuntime { param([array]$Command) return @{ command = $Command } }
function Invoke-WithDispatcherOperationLock { param([scriptblock]$Action) & $Action }

$snapshot = Get-DispatcherContainerSnapshot
if ($snapshot.id -ne $script:FullId) {
  throw (
    "snapshot did not use the full immutable docker inspect ID: id={0}; calls={1}" -f
    [string]$snapshot.id,
    (($script:DockerCalls | ForEach-Object { $_ -join "," }) -join ";")
  )
}
if (-not $snapshot.ownership_provenance_known -or $snapshot.operation_nonce -ne $script:Nonce) {
  throw (
    "snapshot did not parse operation nonce from JSON labels: known={0}; nonce={1}" -f
    [bool]$snapshot.ownership_provenance_known,
    [string]$snapshot.operation_nonce
  )
}

$originalSnapshot = ${function:Get-DispatcherContainerSnapshot}
function Get-DispatcherContainerSnapshot {
  return @{
    identity_known = $true
    ownership_provenance_known = $true
    exists = $true
    running = $true
    id = $script:CurrentId
    operation_nonce = $script:CurrentNonce
  }
}
$script:DockerCalls = @()
$script:CurrentId = ("b" * 64)
$script:CurrentNonce = $script:Nonce
Stop-DispatcherRuntime -ExpectedContainerId $script:FullId -ExpectedOperationNonce $script:Nonce
if ($script:DockerCalls.Count -ne 0) {
  throw "stale ownership state attempted to remove a replacement container"
}

$script:CurrentId = $script:FullId
Stop-DispatcherRuntime -ExpectedContainerId "aaaaaaaaaaaa" -ExpectedOperationNonce $script:Nonce
if ($script:DockerCalls.Count -ne 0) {
  throw "short legacy ownership ID attempted to remove a dispatcher"
}

$script:CurrentId = $script:FullId
$script:CurrentNonce = ("d" * 32)
Stop-DispatcherRuntime -ExpectedContainerId $script:FullId -ExpectedOperationNonce $script:Nonce
if ($script:DockerCalls.Count -ne 0) {
  throw "same-id wrong-nonce ownership proof attempted to remove a dispatcher"
}

$script:CurrentId = $script:FullId
$script:CurrentNonce = $script:Nonce
$script:ContainerPresent = $true
$stopResult = Stop-DispatcherRuntime -ExpectedContainerId $script:FullId -ExpectedOperationNonce $script:Nonce
if (-not $stopResult.ok -or -not $stopResult.stopped) {
  throw "verified exact-ID/nonce stop did not report success"
}
$removals = @($script:DockerCalls | Where-Object {
  $_.Count -eq 3 -and $_[0] -eq "rm" -and $_[1] -eq "-f"
})
if ($removals.Count -ne 1 -or $removals[0][2] -ne $script:FullId) {
  throw "owned dispatcher must be removed exactly once by immutable container ID"
}
if (@($script:DockerCalls | Where-Object { $_ -contains "jarvis-gpt-dispatcher" }).Count -ne 0) {
  throw "stop must never remove dispatcher by mutable container name"
}
Write-Output "OK"
"""
    completed = subprocess.run(  # noqa: S603 - resolved interpreter, fixed script
        [powershell, "-NoProfile", "-NonInteractive", "-Command", script],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=20,
        check=False,
    )
    out = completed.stdout or ""
    err = completed.stderr or ""
    assert completed.returncode == 0, out + err
    assert out.strip().splitlines()[-1] == "OK"


def test_launcher_ownership_requires_equal_full_container_ids() -> None:
    powershell = shutil.which("powershell.exe") or shutil.which("pwsh")
    if powershell is None:
        pytest.skip("PowerShell is required for the launcher ownership regression")

    script = r"""
$path = Join-Path (Get-Location) "scripts\jarvis-launcher.ps1"
$tokens = $null
$errors = $null
$ast = [System.Management.Automation.Language.Parser]::ParseFile(
  $path, [ref]$tokens, [ref]$errors
)
if ($errors.Count -ne 0) { throw ($errors | Out-String) }
foreach ($name in @(
  "Read-DispatcherOwnershipJournal",
  "Get-JournalOwnedDispatcherId",
  "Test-LauncherOwnsDispatcher",
  "Get-LauncherOwnedDispatcherId",
  "Test-ReusedDispatcherOwnership"
)) {
  $functionAst = $ast.Find({
    param($node)
    $node -is [System.Management.Automation.Language.FunctionDefinitionAst] -and
      $node.Name -eq $name
  }, $true)
  Invoke-Expression $functionAst.Extent.Text
}
$global:DispatcherOwnershipJournal = ""
$fullId = "a" * 64
$foreignId = "b" * 64
$legacy = [pscustomobject]@{
  services = [pscustomobject]@{
    dispatcher = [pscustomobject]@{ started_by_launcher = $true; container_id = "aaaaaaaaaaaa" }
  }
}
if (Test-LauncherOwnsDispatcher -State $legacy) {
  throw "short legacy ID granted launcher ownership"
}
$nonce = "c" * 32
$owned = [pscustomobject]@{
  services = [pscustomobject]@{
    dispatcher = [pscustomobject]@{
      started_by_launcher = $true
      container_id = $fullId
      operation_nonce = $nonce
    }
  }
}
$readiness = @{
  container = @{
    running = $true
    identity_known = $true
    ownership_provenance_known = $true
    exists = $true
    id = $fullId
    operation_nonce = $nonce
  }
}
if (-not (Test-ReusedDispatcherOwnership -State $owned -Readiness $readiness)) {
  throw "equal full IDs did not continue ownership"
}
$readiness.container.id = $foreignId
if (Test-ReusedDispatcherOwnership -State $owned -Readiness $readiness) {
  throw "foreign full ID continued ownership"
}
$readiness.container.id = $fullId
$readiness.container.operation_nonce = "d" * 32
if (Test-ReusedDispatcherOwnership -State $owned -Readiness $readiness) {
  throw "same full ID with foreign nonce continued ownership"
}
$readiness.container.operation_nonce = $nonce
$readiness.container.id = "aaaaaaaaaaaa"
if (Test-ReusedDispatcherOwnership -State $owned -Readiness $readiness) {
  throw "short current ID continued ownership"
}
$journalPath = Join-Path ([IO.Path]::GetTempPath()) (
  "jarvis-dispatcher-journal-{0}.json" -f [Guid]::NewGuid().ToString("N")
)
$global:DispatcherOwnershipJournal = $journalPath
@{
  version = 1
  phase = "candidate"
  launcher_owned = $true
  requires_state_sync = $false
  container_id = $fullId
  previous_container_id = ""
  operation_nonce = $nonce
} | ConvertTo-Json | Set-Content -LiteralPath $journalPath -Encoding Ascii
try {
  $journalReadiness = @{
    container = @{
      running = $true
      identity_known = $true
      ownership_provenance_known = $true
      exists = $true
      id = $fullId
      operation_nonce = $nonce
    }
  }
  if (-not (Test-ReusedDispatcherOwnership -State $null -Readiness $journalReadiness)) {
    throw "nonce-bound journal did not recover ownership after launcher-state loss"
  }
  $absentSnapshot = @{
    identity_known = $true
    ownership_provenance_known = $false
    exists = $false
    running = $false
  }
  if ((Get-JournalOwnedDispatcherId -Snapshot $absentSnapshot) -ne $fullId) {
    throw "journal target was lost before exact-ID absence verification"
  }
  $staleState = [pscustomobject]@{
    services = [pscustomobject]@{
      dispatcher = [pscustomobject]@{
        started_by_launcher = $true
        container_id = $foreignId
        operation_nonce = $nonce
      }
    }
  }
  if ((Get-LauncherOwnedDispatcherId -State $staleState -Snapshot $absentSnapshot) -ne $fullId) {
    throw "stale launcher state overrode the newer journal target"
  }
  $journalReadiness.container.operation_nonce = "d" * 32
  if (Test-ReusedDispatcherOwnership -State $null -Readiness $journalReadiness) {
    throw "foreign nonce recovered launcher ownership"
  }
} finally {
  Remove-Item -LiteralPath $journalPath -Force -ErrorAction SilentlyContinue
}
Write-Output "OK"
"""
    completed = subprocess.run(  # noqa: S603 - fixed local test script
        [powershell, "-NoProfile", "-NonInteractive", "-Command", script],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=20,
        check=False,
    )
    out = completed.stdout or ""
    err = completed.stderr or ""
    assert completed.returncode == 0, out + err
    assert out.strip().splitlines()[-1] == "OK"


def test_launcher_manages_telegram_bridge_with_dedicated_atomic_secret() -> None:
    launcher = _read("scripts/jarvis-launcher.ps1")

    assert (
        '$TelegramBridgeTokenFile = Join-Path $HomePath '
        '".jarvis\\telegram-bridge.token"'
    ) in launcher
    assert "function Get-OrCreateTelegramBridgeToken" in launcher
    assert "New-RandomBase64UrlToken" in launcher
    assert "New-Object byte[] 32" in launcher
    assert "Protect-TelegramBridgeTokenFile -Path $temporary" in launcher
    assert "[System.IO.File]::Move($temporary, $TelegramBridgeTokenFile)" in launcher
    assert '"/inheritance:r"' in launcher
    assert '"/grant:r", "*$($identity):(F)"' in launcher
    assert "$env:JARVIS_TELEGRAM_BRIDGE_SECRET = $bridgeSecret" in launcher
    assert "telegram:$botId" in launcher
    assert "$env:JARVIS_TELEGRAM_BOT_ID = [string]$botId" in launcher
    assert "$env:JARVIS_TELEGRAM_REALM_ID = $canonicalRealm" in launcher
    assert "function Wait-TelegramBridgeReady" in launcher
    assert 'content.Contains("Telegram bridge online as @")' in launcher
    assert '-Service "telegram"' in launcher
    assert (
        '-Arguments @("-3.11", (Join-Path $RepoRoot "jarvis.py"), '
        '"--profile", $Profile, "telegram-bridge")'
    ) in launcher
    assert 'foreach ($service in @("frontend", "backend", "bridge", "telegram"))' in launcher
    assert "telegram_environment_sha256" in launcher
    assert "telegram-bridge.token" not in launcher[launcher.index("$services.telegram = @{") :]

    backend_start = launcher.index(
        "pid = Start-BackendProcess", launcher.index("function Start-JarvisStack")
    )
    telegram_start = launcher.index(
        '-Name "Telegram bridge"', launcher.index("function Start-JarvisStack")
    )
    assert backend_start < telegram_start


def test_launcher_requires_explicit_legacy_telegram_realm_mapping() -> None:
    launcher = _read("scripts/jarvis-launcher.ps1")

    initialize = launcher.index("function Initialize-TelegramBridgeEnvironment")
    next_function = launcher.index("function Get-StringSha256", initialize)
    body = launcher[initialize:next_function]
    missing_guard = body.index("if ($legacyStoreExists -and -not $legacyRealm)")
    mismatch_guard = body.index(
        "if ($legacyStoreExists -and $legacyRealm -ne $canonicalRealm)"
    )
    export_mapping = body.index(
        "$env:JARVIS_TELEGRAM_LEGACY_REALM_ID = $legacyRealm"
    )
    source_guard = body.index("if ($legacySourceRealm -eq $canonicalRealm)")
    export_source = body.index(
        "$env:JARVIS_TELEGRAM_LEGACY_SOURCE_REALM_ID = $legacySourceRealm"
    )

    assert "Realm-less Telegram history requires an explicit" in body
    assert "after verifying the bot with getMe" in body
    assert missing_guard < mismatch_guard < export_mapping
    assert source_guard < export_source
    assert "$env:JARVIS_TELEGRAM_LEGACY_REALM_ID = $canonicalRealm" not in body
    assert "must differ from the canonical destination realm" in body
    assert "JARVIS_TELEGRAM_LEGACY_SOURCE_REALM_ID" in launcher[
        launcher.index("function Get-TelegramEnvironmentSha256") :
    ]

    stop_function = launcher[
        launcher.index("function Stop-JarvisStack") : launcher.index(
            "function Show-ServiceRow"
        )
    ]
    status_function = launcher[
        launcher.index("function Show-JarvisStatus") : launcher.index(
            "function Show-Logs"
        )
    ]
    assert (
        "Set-JarvisEnvironment -SelectedProfile $Profile "
        "-SkipTelegramInitialization"
    ) in stop_function
    assert "-SkipTelegramInitialization" in status_function


def test_launcher_reconciles_disabled_telegram_before_rewriting_state() -> None:
    launcher = _read("scripts/jarvis-launcher.ps1")

    stop_fn = launcher.index("function Stop-ManagedTelegramProcesses")
    next_fn = launcher.index("function Invoke-BridgeCapabilitiesProbe", stop_fn)
    stop_body = launcher[stop_fn:next_fn]
    assert "Get-ManagedTelegramProcesses -Snapshot $snapshot" in stop_body
    assert 'Test-ManagedServiceProcess -ProcessInfo $_ -Service "telegram"' in launcher
    assert "Stop-ProcessTree" in stop_body
    assert "launcher state was not rewritten" in stop_body
    assert "Stop-JarvisProcessesBySignature" not in stop_body

    start_fn = launcher.index("function Start-JarvisStack")
    initialization = launcher.index("Set-JarvisEnvironment -SelectedProfile $Profile", start_fn)
    initialization_failure_stop = launcher.index(
        'Stop-ManagedTelegramProcesses -Reason "Telegram startup validation failed"',
        initialization,
    )
    reconcile = launcher.index(
        "Stop-ManagedTelegramProcesses -Reason", initialization_failure_stop + 1
    )
    already_running = launcher.index("Get-AlreadyRunningStackServices", start_fn)
    early_state_write = launcher.index(
        "Save-LauncherState -Services $alreadyRunningServices", already_running
    )
    reconcile_guard = launcher.rindex(
        "if (-not $script:TelegramBridgeEnabled -or $NoTelegram -or $NoBackend)",
        start_fn,
        reconcile,
    )
    assert reconcile_guard < reconcile < already_running < early_state_write
    assert initialization < initialization_failure_stop < already_running


def test_launcher_replaces_telegram_before_backend_mutation_and_after_readiness() -> None:
    launcher = _read("scripts/jarvis-launcher.ps1")
    start_fn = launcher.index("function Start-JarvisStack")
    stop_telegram = launcher.index(
        "Stop-ManagedTelegramProcesses -Reason $telegramReplacementReason", start_fn
    )
    stop_backend = launcher.index(
        "Stopping managed backend before mutating init", stop_telegram
    )
    mutating_init = launcher.index(
        'Invoke-JarvisCommand -FilePath "py.exe" -Arguments '
        '@("-3.11", ".\\jarvis.py", "--profile", $Profile, "init")',
        stop_backend,
    )
    backend_ready = launcher.index("Wait-BackendApiReady -TimeoutSec 30", mutating_init)
    start_telegram = launcher.index('-Name "Telegram bridge"', backend_ready)
    live_llm_ready = launcher.rindex(
        "Wait-LlmReady -TimeoutSec $readinessDeadlineSec",
        mutating_init,
        backend_ready,
    )
    replacement_required = launcher.index(
        "$telegramReplacementRequired = [bool](", start_fn
    )
    replacement_guard = launcher[replacement_required:stop_telegram]

    assert "if ($telegramLaunchRequested)" in replacement_guard
    assert "stack restart requires fresh live-LLM readiness" in replacement_guard
    assert (
        stop_telegram
        < stop_backend
        < mutating_init
        < live_llm_ready
        < backend_ready
        < start_telegram
    )

    already_fn = launcher.index("function Get-AlreadyRunningStackServices")
    already_body = launcher[already_fn:start_fn]
    already_llm_gate = already_body.index("$llmReadiness = Get-LlmReadiness")
    already_ready_guard = already_body.index("-not [bool]$llmReadiness.ready")
    telegram_ready_record = already_body.index(
        'readiness = "live-llm+getMe+store"'
    )
    already_return = already_body.rindex("return $services")
    assert (
        already_llm_gate
        < already_ready_guard
        < telegram_ready_record
        < already_return
    )

    reuse_fn = launcher.index("function Test-TelegramBridgeReuseState")
    reuse_body = launcher[
        reuse_fn : launcher.index("function Invoke-BridgeCapabilitiesProbe", reuse_fn)
    ]
    assert "Get-DescendantProcessIds" in reuse_body
    assert "$unexpectedManaged" in reuse_body
    assert "Get-ManagedTelegramProcesses -Snapshot $Snapshot" in reuse_body


def test_telegram_reuse_state_rejects_duplicate_managed_bridge() -> None:
    powershell = shutil.which("powershell.exe") or shutil.which("pwsh")
    if powershell is None:
        pytest.skip("PowerShell is required for the launcher behavior regression")

    script = r"""
$path = Join-Path (Get-Location) "scripts\jarvis-launcher.ps1"
$tokens = $null
$errors = $null
$ast = [System.Management.Automation.Language.Parser]::ParseFile(
  $path,
  [ref]$tokens,
  [ref]$errors
)
if ($errors.Count -ne 0) {
  throw ($errors | ForEach-Object { $_.Message } | Out-String)
}
$functionAst = $ast.Find({
  param($node)
  $node -is [System.Management.Automation.Language.FunctionDefinitionAst] -and
    $node.Name -eq "Test-TelegramBridgeReuseState"
}, $true)
if (-not $functionAst) {
  throw "Test-TelegramBridgeReuseState was not found"
}
Invoke-Expression $functionAst.Extent.Text

$script:TelegramRealmId = "telegram:700001"
$script:TelegramBotId = [int64]700001
function Test-ManagedServiceProcess {
  param($ProcessInfo, [string]$Service)
  return (
    $Service -eq "telegram" -and
    $null -ne $ProcessInfo -and
    [bool]$ProcessInfo.Managed
  )
}
function Get-ManagedTelegramProcesses {
  param([array]$Snapshot = $null)
  return @(
    $Snapshot |
      Where-Object { Test-ManagedServiceProcess -ProcessInfo $_ -Service "telegram" }
  )
}
function Get-DescendantProcessIds {
  param([int]$RootProcessId, [array]$Snapshot)
  return @(
    $Snapshot |
      Where-Object { [int]$_.ParentProcessId -eq $RootProcessId } |
      ForEach-Object { [int]$_.ProcessId }
  )
}

$state = [pscustomobject]@{
  services = [pscustomobject]@{
    telegram = [pscustomobject]@{
      pid = 10
      realm_id = "telegram:700001"
      bot_id = [int64]700001
    }
  }
}
$snapshot = @(
  [pscustomobject]@{ ProcessId = 10; ParentProcessId = 1; Managed = $true },
  [pscustomobject]@{ ProcessId = 11; ParentProcessId = 10; Managed = $true },
  [pscustomobject]@{ ProcessId = 99; ParentProcessId = 1; Managed = $false }
)
if (-not (Test-TelegramBridgeReuseState -PreviousState $state -Snapshot $snapshot)) {
  throw "tracked bridge tree plus a foreign process must be reusable"
}

$duplicateSnapshot = @($snapshot) + @(
  [pscustomobject]@{ ProcessId = 20; ParentProcessId = 1; Managed = $true }
)
if (Test-TelegramBridgeReuseState -PreviousState $state -Snapshot $duplicateSnapshot) {
  throw "a duplicate managed bridge outside the tracked tree must force replacement"
}

$state.services.telegram.realm_id = "telegram:700002"
if (Test-TelegramBridgeReuseState -PreviousState $state -Snapshot $snapshot) {
  throw "a realm mismatch must force replacement"
}
Write-Output "OK"
"""
    completed = subprocess.run(  # noqa: S603 - resolved interpreter, fixed script
        [powershell, "-NoProfile", "-NonInteractive", "-Command", script],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        timeout=20,
        check=False,
    )
    assert completed.returncode == 0, completed.stderr or completed.stdout
    assert completed.stdout.strip() == "OK"


def test_launcher_profile_registry_is_not_duplicated_in_powershell() -> None:
    """Launcher policy comes from config.py, including newly registered profiles."""

    launcher = _read("scripts/jarvis-launcher.ps1")
    assert "function Get-ProfileCatalog" in launcher
    assert "function Get-ProfileCertification" in launcher
    assert "function Assert-ProfileAllowed" in launcher
    assert "function Invoke-ProfileHealthProbe" in launcher
    assert ".\\jarvis.py profiles" in launcher
    assert '$script:ProfileCatalog' in launcher
    assert "AllowExperimentalProfiles" in launcher
    assert "IUnderstandExperimentalProfile" in launcher
    assert "readiness_deadline_sec" in launcher or "readiness_deadline" in launcher
    assert "requires_experimental_opt_in" in launcher
    assert '[ValidateSet("gemma4-turbo"' not in launcher
    assert 'switch ($SelectedProfile)' not in launcher
    assert 'Value = "gemma4-turbo"' not in launcher
    assert '[string]$Profile = ""' in launcher
    assert ".\\jarvis.py profiles 2>&1" not in launcher
    assert "-not $script:IUnderstandExperimentalProfile" in launcher
    assert launcher.rindex("$script:Profile = Get-DefaultProfileName") < launcher.rindex(
        "switch ($Action)"
    )


def test_launcher_live_completion_readiness_is_fail_closed() -> None:
    powershell = shutil.which("powershell.exe") or shutil.which("pwsh")
    if powershell is None:
        pytest.skip("PowerShell is required for the launcher behavior regression")

    script = r"""
$path = Join-Path (Get-Location) "scripts\jarvis-launcher.ps1"
$tokens = $null
$errors = $null
$ast = [System.Management.Automation.Language.Parser]::ParseFile(
  $path,
  [ref]$tokens,
  [ref]$errors
)
if ($errors.Count -ne 0) {
  throw ($errors | ForEach-Object { $_.Message } | Out-String)
}
foreach ($name in @("Invoke-ProfileHealthProbe", "Get-LlmReadiness", "Wait-LlmReady")) {
  $functionAst = $ast.Find({
    param($node)
    $node -is [System.Management.Automation.Language.FunctionDefinitionAst] -and
      $node.Name -eq $name
  }, $true)
  if (-not $functionAst) {
    throw "$name was not found"
  }
  Invoke-Expression $functionAst.Extent.Text
}

$script:ProbeMode = "four"
function Invoke-RestMethod {
  param(
    [string]$Method,
    [string]$Uri,
    [string]$ContentType,
    [string]$Body,
    [int]$TimeoutSec
  )
  $payload = $Body | ConvertFrom-Json
  if ($payload.chat_template_kwargs.enable_thinking -ne $false) {
    throw "readiness probe must disable model thinking"
  }
  if ($script:ProbeMode -eq "error") {
    throw "synthetic completion timeout"
  }
  $content = switch ($script:ProbeMode) {
    "four" { " `r`n4`t " }
    "wrong" { "5" }
    "empty" { "" }
    "repeated" { "aaaaaaaaaaaa" }
    default { "unexpected" }
  }
  return [pscustomobject]@{
    choices = @(
      [pscustomobject]@{
        message = [pscustomobject]@{ content = $content }
      }
    )
  }
}

$valid = Invoke-ProfileHealthProbe
if (-not $valid.ok -or $valid.normalized_content -ne "4") {
  throw "a normalized exact answer of 4 must pass"
}
foreach ($mode in @("wrong", "empty", "repeated")) {
  $script:ProbeMode = $mode
  $failed = Invoke-ProfileHealthProbe
  if ($failed.ok -or $failed.warming) {
    throw "$mode completion must fail as unhealthy"
  }
}
$script:ProbeMode = "error"
$transportFailure = Invoke-ProfileHealthProbe
if ($transportFailure.ok -or -not $transportFailure.warming) {
  throw "completion transport failure must stay non-ready warmup"
}
if ($transportFailure.ContainsKey("skipped")) {
  throw "completion transport failure must not be reported as skipped success"
}

$global:Profile = "qwen36-vl"
function Set-JarvisEnvironment {
  param([string]$SelectedProfile, [switch]$SkipTelegramInitialization)
}
function Get-DispatcherContainerSnapshot {
  return @{
    docker_available = $true
    identity_known = $true
    exists = $true
    running = $true
    state = "running"
    status = "Up"
    id = "container-1"
    runtime = @{ model_id = "model-1" }
  }
}
function Test-PortOpen { param([int]$Port) return $true }
function Invoke-HttpProbe {
  param([string]$Uri, [int]$TimeoutSec, [switch]$Json)
  if ($Uri.EndsWith("/v1/models")) {
    return @{
      ok = $true
      status = 200
      error = ""
      data = @{ data = @(@{ id = "dispatcher" }) }
    }
  }
  return @{ ok = $true; status = 200; error = ""; data = $null }
}
function Get-ProfileCertification {
  param([string]$SelectedProfile)
  return @{
    readiness_deadline_sec = 900
    certification = "certified"
    certification_reason = "test"
    interactive_certified = $true
  }
}
function Get-DispatcherLogSignals { return @() }

$script:ProbeMode = "error"
$warming = Get-LlmReadiness
if ($warming.ready -or $warming.unhealthy -or $warming.phase -ne "completion-warming") {
  throw "failed live completion must remain a separate non-ready warmup phase"
}
if ($warming.completion_status -ne "warming") {
  throw "completion status must expose warmup"
}

$script:ProbeMode = "wrong"
$unhealthy = Get-LlmReadiness
if ($unhealthy.ready -or -not $unhealthy.unhealthy -or $unhealthy.phase -ne "unhealthy") {
  throw "a wrong live completion must make readiness unhealthy"
}

$script:ProbeMode = "four"
$ready = Get-LlmReadiness
if (-not $ready.ready -or $ready.phase -ne "ready" -or -not $ready.completion_ok) {
  throw "readiness requires a successful exact live completion"
}

$script:WaitProbeCount = 0
function Get-LlmReadiness {
  $script:WaitProbeCount += 1
  if ($script:WaitProbeCount -eq 1) {
    return @{
      ready = $false
      unhealthy = $false
      phase = "completion-warming"
      completion_error = "synthetic warmup"
    }
  }
  return @{
    ready = $true
    unhealthy = $false
    phase = "ready"
    completion_error = ""
  }
}
$waited = Wait-LlmReady -TimeoutSec 2 -PollIntervalMilliseconds 1
if (-not $waited.ready -or $script:WaitProbeCount -ne 2) {
  throw "start gating must preserve a warming model and wait for live completion"
}

function Get-LlmReadiness {
  return @{
    ready = $false
    unhealthy = $true
    unhealthy_reason = "synthetic wrong answer"
    phase = "unhealthy"
    completion_error = "synthetic wrong answer"
  }
}
$unhealthyRejected = $false
try {
  Wait-LlmReady -TimeoutSec 2 -PollIntervalMilliseconds 1 | Out-Null
} catch {
  $unhealthyRejected = $_.Exception.Message.Contains("synthetic wrong answer")
}
if (-not $unhealthyRejected) {
  throw "start gating must reject an unhealthy live completion"
}
Write-Output "OK"
"""
    completed = subprocess.run(  # noqa: S603 - resolved interpreter, fixed script
        [powershell, "-NoProfile", "-NonInteractive", "-Command", script],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )
    assert completed.returncode == 0, completed.stderr or completed.stdout
    assert completed.stdout.strip() == "OK"


def test_frontend_runtime_uses_unprivileged_node_user() -> None:
    dockerfile = _read("frontend/Dockerfile")
    dockerignore = _read("frontend/.dockerignore")

    assert "NEXT_PUBLIC_JARVIS_API_URL" not in dockerfile
    assert "NEXT_PUBLIC_JARVIS_API_TOKEN" not in dockerfile
    assert "COPY --chown=node:node" in dockerfile
    assert "USER node" in dockerfile
    assert "node_modules/" in dockerignore
    assert ".next/" in dockerignore
    assert ".env.*" in dockerignore
    assert not (REPO_ROOT / "frontend/proxy.ts").exists()
    route = _read("frontend/app/jarvis-api/[...path]/route.ts")
    assert "Server-side JARVIS_API_TOKEN is required" in route


def test_launcher_scopes_unique_runtime_generation_to_each_backend_launch() -> None:
    powershell = shutil.which("powershell.exe") or shutil.which("pwsh")
    if powershell is None:
        pytest.skip("PowerShell is required for the backend generation regression")

    script = r"""
$path = Join-Path (Get-Location) "scripts\jarvis-launcher.ps1"
$tokens = $null
$errors = $null
$ast = [System.Management.Automation.Language.Parser]::ParseFile(
  $path, [ref]$tokens, [ref]$errors
)
if ($errors.Count -ne 0) { throw ($errors | Out-String) }
$functionAst = $ast.Find({
  param($node)
  $node -is [System.Management.Automation.Language.FunctionDefinitionAst] -and
    $node.Name -eq "Start-BackendProcess"
}, $true)
Invoke-Expression $functionAst.Extent.Text
$script:Generations = @()
function Start-ManagedProcess {
  param(
    [string]$Name,
    [string]$FilePath,
    [string[]]$Arguments,
    [string]$WorkingDirectory,
    [string]$Stdout,
    [string]$Stderr
  )
  if ($Name -ne "backend") { throw "runtime generation escaped backend scope" }
  $script:Generations += [string]$env:JARVIS_BACKEND_RUNTIME_GENERATION
  return 42
}
Remove-Item Env:\JARVIS_BACKEND_RUNTIME_GENERATION -ErrorAction SilentlyContinue
$first = Start-BackendProcess `
  -FilePath "py.exe" -Arguments @("serve") -WorkingDirectory "." `
  -Stdout "out.log" -Stderr "err.log"
if ($first -ne 42 -or (Test-Path Env:\JARVIS_BACKEND_RUNTIME_GENERATION)) {
  throw "backend generation leaked into later launcher children"
}
$second = Start-BackendProcess `
  -FilePath "py.exe" -Arguments @("serve") -WorkingDirectory "." `
  -Stdout "out.log" -Stderr "err.log"
if ($second -ne 42 -or $script:Generations.Count -ne 2) {
  throw "backend launches did not receive one generation each"
}
if (
  $script:Generations[0] -notmatch '^[0-9a-f]{32}$' -or
  $script:Generations[1] -notmatch '^[0-9a-f]{32}$' -or
  $script:Generations[0] -eq $script:Generations[1]
) {
  throw "backend runtime generations were not unique high-entropy IDs"
}
$env:JARVIS_BACKEND_RUNTIME_GENERATION = "caller-value"
[void](Start-BackendProcess `
  -FilePath "py.exe" -Arguments @("serve") -WorkingDirectory "." `
  -Stdout "out.log" -Stderr "err.log")
if ($env:JARVIS_BACKEND_RUNTIME_GENERATION -ne "caller-value") {
  throw "launcher did not restore the caller environment after backend spawn"
}
Remove-Item Env:\JARVIS_BACKEND_RUNTIME_GENERATION -ErrorAction SilentlyContinue
Write-Output "OK"
"""
    completed = subprocess.run(  # noqa: S603 - fixed local test script
        [powershell, "-NoProfile", "-NonInteractive", "-Command", script],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        timeout=20,
        check=False,
    )
    assert completed.returncode == 0, completed.stdout + completed.stderr
    assert completed.stdout.strip().splitlines()[-1] == "OK"

    launcher = _read("scripts/jarvis-launcher.ps1")
    backend_start = launcher.index("function Start-BackendProcess")
    stack_start = launcher.index("function Start-JarvisStack")
    assert "JARVIS_BACKEND_RUNTIME_GENERATION" in launcher[backend_start:stack_start]
    environment_start = launcher.index("function Set-JarvisEnvironment")
    environment_end = launcher.index("function Ensure-LauncherFolders", environment_start)
    assert (
        "Remove-Item Env:\\JARVIS_BACKEND_RUNTIME_GENERATION"
        in launcher[environment_start:environment_end]
    )


def test_launcher_stop_preserves_ownership_on_docker_and_rm_failures() -> None:
    powershell = shutil.which("powershell.exe") or shutil.which("pwsh")
    if powershell is None:
        pytest.skip("PowerShell is required for the launcher stop regression")

    script = r"""
$path = Join-Path (Get-Location) "scripts\jarvis-launcher.ps1"
$tokens = $null
$errors = $null
$ast = [System.Management.Automation.Language.Parser]::ParseFile(
  $path, [ref]$tokens, [ref]$errors
)
if ($errors.Count -ne 0) { throw ($errors | Out-String) }
foreach ($name in @("Test-DispatcherContainerIdAbsent", "Stop-DispatcherRuntime")) {
  $functionAst = $ast.Find({
    param($node)
    $node -is [System.Management.Automation.Language.FunctionDefinitionAst] -and
      $node.Name -eq $name
  }, $true)
  Invoke-Expression $functionAst.Extent.Text
}
$script:FullId = "a" * 64
$script:DockerAvailable = $false
$script:RemoveCalls = 0
function Get-Command {
  param([string]$Name, [object]$ErrorAction)
  if ($Name -eq "docker") {
    if ($script:DockerAvailable) {
      return [pscustomobject]@{ Source = "Invoke-FailingDocker" }
    }
    return $null
  }
  Microsoft.PowerShell.Core\Get-Command @PSBoundParameters
}
function Test-DockerReady { return @{ ready = $true; error = "" } }
function Invoke-WithDispatcherOperationLock { param([scriptblock]$Action) & $Action }
$script:Nonce = "c" * 32
function Get-DispatcherContainerSnapshot {
  return @{
    identity_known = $true
    ownership_provenance_known = $true
    exists = $true
    running = $true
    id = $script:FullId
    operation_nonce = $script:Nonce
  }
}
function Invoke-FailingDocker {
  if ($args[0] -eq "rm") {
    $script:RemoveCalls += 1
    $global:LASTEXITCODE = 1
    return "synthetic rm failure"
  }
  $global:LASTEXITCODE = 1
  return "synthetic inspect failure"
}
$unavailable = Stop-DispatcherRuntime -ExpectedContainerId $script:FullId -ExpectedOperationNonce $script:Nonce
if ($unavailable.ok -or $unavailable.reason -ne "docker-cli-unavailable") {
  throw "Docker-unavailable stop was not reported as an unproven failure"
}
$script:DockerAvailable = $true
$rmFailure = Stop-DispatcherRuntime -ExpectedContainerId $script:FullId -ExpectedOperationNonce $script:Nonce
if ($rmFailure.ok -or $rmFailure.reason -ne "docker-rm-failed") {
  throw "docker rm failure was not reported as an unproven failure"
}
if ($script:RemoveCalls -ne 1) { throw "exact-ID rm was not attempted once" }
Write-Output "OK"
"""
    completed = subprocess.run(  # noqa: S603 - fixed local test script
        [powershell, "-NoProfile", "-NonInteractive", "-Command", script],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=20,
        check=False,
    )
    out = completed.stdout or ""
    err = completed.stderr or ""
    assert completed.returncode == 0, out + err
    assert out.strip().splitlines()[-1] == "OK"

    launcher = _read("scripts/jarvis-launcher.ps1")
    stop_stack = launcher[launcher.index("function Stop-JarvisStack") :]
    failure_guard = stop_stack.index(
        "Dispatcher stop was not proven; launcher ownership state was preserved"
    )
    journal_guard = stop_stack.index(
        "Dispatcher ownership could not be proven while a launcher-owned mutation"
    )
    state_delete = stop_stack.index("Remove-Item -LiteralPath $StateFile -Force")
    assert failure_guard < state_delete
    assert journal_guard < state_delete
    assert "$pendingOwnershipJournalPresent" in stop_stack[:state_delete]

    start_stack = launcher[
        launcher.index("function Start-JarvisStack") : launcher.index(
            "function Stop-JarvisStack"
        )
    ]
    assert "Dispatcher ownership journal is invalid" in start_stack


def test_launcher_docker_identity_unknown_is_not_treated_as_absent() -> None:
    powershell = shutil.which("powershell.exe") or shutil.which("pwsh")
    if powershell is None:
        pytest.skip("PowerShell is required for the tri-state Docker regression")

    script = r"""
$path = Join-Path (Get-Location) "scripts\jarvis-launcher.ps1"
$tokens = $null
$errors = $null
$ast = [System.Management.Automation.Language.Parser]::ParseFile(
  $path, [ref]$tokens, [ref]$errors
)
if ($errors.Count -ne 0) { throw ($errors | Out-String) }
$functionAst = $ast.Find({
  param($node)
  $node -is [System.Management.Automation.Language.FunctionDefinitionAst] -and
    $node.Name -eq "Get-LlmStartDecision"
}, $true)
Invoke-Expression $functionAst.Extent.Text
$readiness = @{
  container = @{
    identity_known = $false
    exists = $null
    running = $false
    state = "unknown"
  }
  models_ok = $false
  port_open = $false
}
$decision = Get-LlmStartDecision `
  -Readiness $readiness `
  -DispatcherStatus @{ runtime_matches_desired = $false }
if ($decision -ne "unknown") {
  throw "transient Docker identity was collapsed into an absent/start decision"
}
Write-Output "OK"
"""
    completed = subprocess.run(  # noqa: S603 - fixed local test script
        [powershell, "-NoProfile", "-NonInteractive", "-Command", script],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        timeout=20,
        check=False,
    )
    assert completed.returncode == 0, completed.stdout + completed.stderr
    assert completed.stdout.strip().splitlines()[-1] == "OK"

    launcher = _read("scripts/jarvis-launcher.ps1")
    assert 'identity_known = $false' in launcher
    assert 'return "unknown"' in launcher
    assert 'skipped = "docker-not-ready"' not in launcher
