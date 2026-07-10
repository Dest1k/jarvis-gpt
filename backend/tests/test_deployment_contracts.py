from __future__ import annotations

from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]


def _read(relative_path: str) -> str:
    return (REPO_ROOT / relative_path).read_text(encoding="utf-8")


def test_backend_image_installs_sandboxed_chromium_and_drops_root() -> None:
    dockerfile = _read("backend/Dockerfile")
    entrypoint = _read("backend/docker-entrypoint.sh")

    assert "rm -rf /var/lib/apt/lists/*" in dockerfile
    assert "chromium-sandbox" in dockerfile
    assert "Acquire::Retries" in dockerfile
    assert "--no-sandbox" not in dockerfile
    assert 'ENTRYPOINT ["jarvis-docker-entrypoint"]' in dockerfile
    assert "CMD gosu jarvis python -c" in dockerfile
    assert 'exec gosu jarvis "$@"' in entrypoint
    assert "Refusing unsafe JARVIS_HOME outside /runtime" in entrypoint
    assert "Refusing unsafe JARVIS_MODEL_ROOT outside JARVIS_HOME" in entrypoint
    assert "Refusing unexpected HOME" in entrypoint


def test_compose_defaults_to_loopback_and_propagates_build_contract() -> None:
    compose = _read("docker-compose.yml")

    assert "jarvis-compose-loopback-only" not in compose
    assert '${JARVIS_API_BIND_ADDRESS:-127.0.0.1}' in compose
    assert '${JARVIS_FRONTEND_BIND_ADDRESS:-127.0.0.1}' in compose
    assert "NEXT_PUBLIC_JARVIS_API_URL" not in compose
    assert "NEXT_PUBLIC_JARVIS_API_TOKEN" not in compose
    assert "JARVIS_BACKEND_URL: http://backend:8000" in compose
    assert "JARVIS_CORS_ORIGINS:" in compose
    assert "JARVIS_API_REQUIRE_TOKEN_ON_LOOPBACK:" in compose
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


def test_launcher_requires_explicit_lan_and_preserves_foreign_listeners() -> None:
    launcher = _read("scripts/jarvis-launcher.ps1")

    assert "$script:LanMode = [bool]$Lan" in launcher
    assert 'Label = "Start with LAN"' in launcher
    assert 'Label = "Start app without LLM"' in launcher
    assert '"app" { $script:NoDispatcher = $true; Start-JarvisStack }' in launcher
    assert "function Get-LlmStartDecision" in launcher
    assert 'return "reuse"' in launcher
    assert 'return "conflict"' in launcher
    assert "LLM is already started" in launcher
    assert "started_by_launcher = $true" in launcher
    assert "function Test-LauncherOwnsDispatcher" in launcher
    assert "function Test-ReusedDispatcherOwnership" in launcher
    assert 'container_id = [string]$llmReadiness.container.id' in launcher
    assert '$phase = "external-ready"' in launcher
    assert "Preserving LLM runtime" in launcher
    assert "Get-OrCreateApiToken" in launcher
    assert "Protect-ApiTokenFile" in launcher
    assert "Get-FrontendEnvironmentSha256" in launcher
    assert '$env:JARVIS_API_HOST = "127.0.0.1"' in launcher
    assert "password source:" in launcher
    assert 'Stop-PortOwner -Port 3000 -ManagedOnly -Service "frontend"' in launcher
    assert 'Stop-PortOwner -Port 8000 -ManagedOnly -Service "backend"' in launcher
    assert 'Stop-PortOwner -Port 8765 -ManagedOnly -Service "bridge"' in launcher
    assert 'Join-Path $RepoRoot "jarvis.py"' in launcher
    assert '"jarvis-gpt-command-center"' not in launcher


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
