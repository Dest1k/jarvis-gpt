"""Hybrid "frontier brain" — PREPARED SCAFFOLD, INACTIVE BY DEFAULT.

================================================================================
WHAT THIS IS
================================================================================
Jarvis's active brain is the local Gemma model (``gemma4-turbo``). This module is
a *ready-but-dormant* second brain that can delegate hard reasoning, planning and
final synthesis to a frontier model (Claude) **when — and only when — the owner
turns it on**. Nothing in the running system routes here until then.

================================================================================
HOW IT REACHES THE FRONTIER MODEL — THE OWNER'S SUBSCRIPTION, NOT AN API KEY
================================================================================
By explicit owner requirement it does NOT use a billed Anthropic API key. It
shells out to the already-logged-in **Claude Code CLI** (`claude`), which answers
using the owner's own authenticated subscription session. The owner also fixed the
frontier model and reasoning depth: **Opus 4.8 at medium effort**:

    claude -p "<prompt>" --system-prompt "<system>" \
           --model claude-opus-4-8 --effort medium --output-format text

So activating the hybrid brain costs nothing beyond the subscription the owner
already has, and no secret ever needs to live in this repo.

================================================================================
HOW TO ACTIVATE (LATER — DO NOT DO THIS AS PART OF THE SCAFFOLD WORK)
================================================================================
1. Make sure `claude` is on PATH and logged in (`claude` interactive once).
2. Set the environment flag:  JARVIS_ENABLE_HYBRID_BRAIN=1
3. (optional) pick a model:    JARVIS_FRONTIER_MODEL=opus   (default)
   (optional) CLI path:        JARVIS_FRONTIER_CLI=claude
   (optional) timeout seconds: JARVIS_FRONTIER_TIMEOUT_SEC=180
4. Decide *where* to delegate. ``select_brain()`` is the single flip-point; wire
   the agent's hard-reasoning / synthesis calls through ``FrontierBrain.complete``
   when it returns ``"frontier"``.

While ``JARVIS_ENABLE_HYBRID_BRAIN`` is unset/0, ``build_frontier_brain`` returns
``None`` and ``select_brain`` always answers ``"local"`` — the scaffold is inert.
"""

from __future__ import annotations

import asyncio
import shutil
import tempfile
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:  # avoid any import cycle at runtime
    from .config import JarvisSettings
    from .llm import LLMResult


# Character budget for what we hand the CLI on one call. Frontier delegation is for
# reasoning/synthesis, not for shovelling unbounded context, so we keep the prompt
# well under the OS command-line limit and let the caller decide what matters.
_MAX_PROMPT_CHARS = 48_000


class FrontierBrain:
    """A dormant delegate to the frontier model via the logged-in Claude Code CLI.

    Constructed only when the owner has enabled the hybrid brain. Every method is
    defensive: any failure yields a not-ok result so the caller transparently falls
    back to the local brain instead of surfacing an error to the operator.
    """

    def __init__(
        self,
        *,
        cli_path: str = "claude",
        model: str = "claude-opus-4-8",
        effort: str = "medium",
        timeout_sec: float = 180.0,
    ) -> None:
        self.cli_path = cli_path or "claude"
        self.model = model or "claude-opus-4-8"
        self.effort = effort or "medium"
        self.timeout_sec = float(timeout_sec) if timeout_sec else 180.0

    def is_available(self) -> bool:
        """True when the Claude Code CLI is actually resolvable on this host."""

        return shutil.which(self.cli_path) is not None

    @staticmethod
    def _split_messages(messages: list[dict[str, str]]) -> tuple[str, str]:
        """Render chat messages into (system_prompt, user_prompt) for the CLI."""

        system_parts: list[str] = []
        convo_parts: list[str] = []
        for message in messages:
            role = str(message.get("role") or "user")
            content = str(message.get("content") or "").strip()
            if not content:
                continue
            if role == "system":
                system_parts.append(content)
            elif role == "assistant":
                convo_parts.append(f"ASSISTANT: {content}")
            else:
                convo_parts.append(f"USER: {content}")
        system_prompt = "\n\n".join(system_parts)
        # A single user turn is passed verbatim; multi-turn is labelled so the
        # frontier model can follow the exchange and answer the final turn.
        if len(convo_parts) == 1 and convo_parts[0].startswith("USER: "):
            user_prompt = convo_parts[0][len("USER: ") :]
        else:
            user_prompt = "\n\n".join(convo_parts)
        return system_prompt[:_MAX_PROMPT_CHARS], user_prompt[:_MAX_PROMPT_CHARS]

    def _build_command(self, system_prompt: str, user_prompt: str) -> list[str]:
        command = [
            self.cli_path,
            "-p",
            user_prompt,
            "--model",
            self.model,
            "--effort",
            self.effort,
            "--output-format",
            "text",
        ]
        if system_prompt:
            command += ["--system-prompt", system_prompt]
        return command

    async def complete(
        self,
        messages: list[dict[str, str]],
        *,
        temperature: float | None = None,  # noqa: ARG002 - CLI has no temperature knob
        max_tokens: int | None = None,  # noqa: ARG002 - bounded by the CLI/session
        thinking_enabled: bool = True,  # noqa: ARG002 - frontier model always reasons
    ) -> LLMResult:
        """Return an ``LLMResult`` from the frontier model, or a not-ok fallback.

        Drop-in shaped like ``LLMRouter.complete`` so a future flip-point can call
        either brain behind the same interface.
        """

        from .llm import LLMResult  # local import breaks the import cycle

        if not self.is_available():
            return LLMResult(
                ok=False,
                content="",
                error=(
                    f"Frontier brain unavailable: CLI {self.cli_path!r} not found on PATH. "
                    "Install/login Claude Code, then retry."
                ),
            )
        system_prompt, user_prompt = self._split_messages(messages)
        if not user_prompt:
            return LLMResult(ok=False, content="", error="Frontier brain got an empty prompt.")
        command = self._build_command(system_prompt, user_prompt)
        try:
            with tempfile.TemporaryDirectory(prefix="jarvis-frontier-") as workdir:
                proc = await asyncio.create_subprocess_exec(
                    *command,
                    stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.PIPE,
                    cwd=workdir,
                )
                try:
                    stdout, stderr = await asyncio.wait_for(
                        proc.communicate(), timeout=self.timeout_sec
                    )
                except TimeoutError:
                    proc.kill()
                    await proc.wait()
                    return LLMResult(
                        ok=False,
                        content="",
                        error=f"Frontier brain timed out after {self.timeout_sec:.0f}s.",
                    )
        except OSError as exc:
            return LLMResult(ok=False, content="", error=f"Frontier brain launch failed: {exc}")
        if proc.returncode != 0:
            detail = (stderr.decode("utf-8", "replace").strip() or "no stderr")[:500]
            return LLMResult(
                ok=False,
                content="",
                error=f"Frontier brain exited {proc.returncode}: {detail}",
            )
        content = stdout.decode("utf-8", "replace").strip()
        if not content:
            return LLMResult(ok=False, content="", error="Frontier brain returned empty output.")
        return LLMResult(
            ok=True,
            content=content,
            raw={"backend": "claude-code-cli", "model": self.model, "effort": self.effort},
        )

    def describe(self) -> dict[str, Any]:
        """Diagnostic snapshot (safe to log; contains no secrets)."""

        return {
            "backend": "claude-code-cli-subscription",
            "cli_path": self.cli_path,
            "cli_resolved": shutil.which(self.cli_path),
            "available": self.is_available(),
            "model": self.model,
            "effort": self.effort,
            "timeout_sec": self.timeout_sec,
        }


def build_frontier_brain(settings: JarvisSettings) -> FrontierBrain | None:
    """Construct the frontier brain only when the owner has enabled it.

    Returns ``None`` while the hybrid brain is a dormant scaffold, which is exactly
    what keeps it inert: there is simply no object for anything to call.
    """

    if not getattr(settings, "hybrid_brain_enabled", False):
        return None
    return FrontierBrain(
        cli_path=getattr(settings, "frontier_cli_path", "claude"),
        model=getattr(settings, "frontier_model", "claude-opus-4-8"),
        effort=getattr(settings, "frontier_effort", "medium"),
        timeout_sec=getattr(settings, "frontier_timeout_sec", 180.0),
    )


def select_brain(settings: JarvisSettings) -> str:
    """The single flip-point for hard-task routing: ``"frontier"`` or ``"local"``.

    Today this always returns ``"local"`` unless the owner has switched the hybrid
    brain on, so callers can be wired through it now with zero behaviour change and
    the delegation lights up later by flipping one environment flag.
    """

    return "frontier" if getattr(settings, "hybrid_brain_enabled", False) else "local"
