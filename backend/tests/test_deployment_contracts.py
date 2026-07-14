from __future__ import annotations

from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]


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
    assert "${JARVIS_QWEN_MODEL_PATH:?" in compose
    assert "JARVIS_QWEN_MODEL_PATH:-/models/" not in compose
    assert "${JARVIS_QWEN_EXTRA_ARGS:-}" in compose
    assert "shm_size: 512m" in compose
    assert compose.count("no-new-privileges:true") == 2
    assert "seccomp=./backend/chromium-seccomp.json" in compose
    assert "cap_drop:" in compose
    assert compose.count("read_only: true") >= 3


def test_chromium_seccomp_profile_keeps_default_deny_and_allows_namespaces() -> None:
    profile = _read("backend/chromium-seccomp.json")

    assert '"defaultAction": "SCMP_ACT_ERRNO"' in profile
    assert '"comment": "Allow create user namespaces"' in profile
    for syscall in ('"clone"', '"setns"', '"unshare"'):
        assert syscall in profile


def test_launcher_is_local_only_and_preserves_foreign_listeners() -> None:
    launcher = _read("scripts/jarvis-launcher.ps1")
    dispatcher_script = _read("scripts/dispatcher.ps1")
    dev_script = _read("scripts/dev.ps1")

    assert "$script:LanMode = $false" in launcher
    assert 'Label = "Start with LAN"' not in launcher
    assert "temporarily disabled" in launcher
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
    assert '$BridgePolicyRevision = "native-app-v2"' in launcher
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
    assert "reporting already-running status without" in launcher
    assert "mutating CLI verification" in launcher
    # Mutating init is gated on live managed backend / lease ownership.
    assert "skipping mutating init" in launcher
    assert "Live managed backend already owns executive state" in launcher
    # Env-change restart releases the lease before init.
    assert "Stopping managed backend before mutating init" in launcher
    # No second init call after lease-sensitive paths.
    assert launcher.count(
        'Arguments @("-3.11", ".\\jarvis.py", "--profile", $Profile, "init")'
    ) == 1


def test_launcher_profile_safety_menu_and_opt_in_contract() -> None:
    """SPARK-0013: normal menu is turbo-only; experimental needs explicit opt-in."""

    launcher = _read("scripts/jarvis-launcher.ps1")
    assert "function Get-ProfileCertification" in launcher
    assert "function Assert-ProfileAllowed" in launcher
    assert "function Invoke-ProfileHealthProbe" in launcher
    assert "AllowExperimentalProfiles" in launcher
    assert "IUnderstandExperimentalProfile" in launcher
    assert "certified interactive (recommended)" in launcher
    assert "readiness_deadline_sec" in launcher or "readiness_deadline" in launcher
    assert "RESOLVED_BY_PRODUCT_DECISION" in launcher
    # Certified turbo remains visible; mono profiles require advanced opt-in.
    assert launcher.count('Value = "gemma4-turbo"') >= 1
    assert "EXPERIMENTAL research-only" in launcher
    assert "UNSUPPORTED interactive" in launcher


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
