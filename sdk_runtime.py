"""Cursor SDK local runtime for the OpenAI-compatible bridge.

Runs the agent harness inside this process/pod via AsyncClient.launch_bridge
(not Cursor Cloud VMs). See https://cursor.com/docs/sdk/python
"""

from __future__ import annotations

import logging
from collections.abc import AsyncIterator
from typing import Any

from cursor_sdk import (
    AgentOptions,
    AsyncAgent,
    AsyncClient,
    CursorAgentError,
    LocalAgentOptions,
    ModelSelection,
)

logger = logging.getLogger("cursor-openai-proxy.sdk")

# OpenAI "auto" / unset → Composer (Cursor Models pool; required for local SDK).
DEFAULT_SDK_MODEL = "composer-2.5"


def resolve_sdk_model(model: str | None) -> str | ModelSelection:
    mid = (model or "").strip()
    if not mid or mid in {"auto", "default"}:
        return DEFAULT_SDK_MODEL
    return mid


async def launch_sdk_client(*, workspace: str) -> AsyncClient:
    """Start the local cursor-sdk-bridge subprocess for this workspace."""
    logger.info("launching cursor-sdk bridge workspace=%s", workspace)
    return await AsyncClient.launch_bridge(
        workspace=workspace,
        timeout=60,
        allow_api_key_env_fallback=True,
    )


def _agent_options(
    *,
    api_key: str,
    model: str | None,
    workspace: str,
    chat_only: bool,
    mode: str,
) -> AgentOptions:
    # Local SDK mode is agent|plan (no ask). Chat-only uses tools=[] → text-only.
    sdk_mode = "plan" if mode == "plan" else "agent"
    tools: list[str] | None = [] if chat_only else None
    return AgentOptions(
        model=resolve_sdk_model(model),
        api_key=api_key,
        local=LocalAgentOptions(cwd=workspace),
        mode=sdk_mode,
        tools=tools,
    )


async def sdk_complete(
    *,
    client: AsyncClient,
    api_key: str,
    prompt: str,
    model: str | None,
    workspace: str,
    chat_only: bool,
    mode: str,
) -> tuple[str, dict[str, Any]]:
    """One-shot non-streaming completion. Returns (text, usage_dict)."""
    opts = _agent_options(
        api_key=api_key,
        model=model,
        workspace=workspace,
        chat_only=chat_only,
        mode=mode,
    )
    try:
        result = await AsyncAgent.prompt(prompt, opts, client=client)
    except CursorAgentError as exc:
        raise RuntimeError(
            f"Cursor SDK failed to start: {exc.message} "
            f"(retryable={exc.is_retryable} code={exc.code})"
        ) from exc

    if result.status == "error":
        raise RuntimeError(f"Cursor SDK run error id={result.id}")
    if result.status == "cancelled":
        raise RuntimeError(f"Cursor SDK run cancelled id={result.id}")

    text = str(result.result or "")
    usage: dict[str, Any] = {
        "prompt_tokens": 0,
        "completion_tokens": 0,
        "total_tokens": 0,
    }
    if result.usage is not None:
        usage = {
            "prompt_tokens": int(result.usage.input_tokens or 0),
            "completion_tokens": int(result.usage.output_tokens or 0),
            "total_tokens": int(result.usage.total_tokens or 0),
        }
    return text, usage


async def sdk_stream_text(
    *,
    client: AsyncClient,
    api_key: str,
    prompt: str,
    model: str | None,
    workspace: str,
    chat_only: bool,
    mode: str,
) -> AsyncIterator[str]:
    """Yield assistant text deltas from a local SDK run."""
    opts = _agent_options(
        api_key=api_key,
        model=model,
        workspace=workspace,
        chat_only=chat_only,
        mode=mode,
    )
    try:
        agent = await AsyncAgent.create(opts, client=client)
    except CursorAgentError as exc:
        raise RuntimeError(
            f"Cursor SDK failed to start: {exc.message} "
            f"(retryable={exc.is_retryable} code={exc.code})"
        ) from exc

    try:
        run = await agent.send(prompt)
        async for chunk in run.iter_text():
            if chunk:
                yield chunk
        result = await run.wait()
        if result.status == "error":
            yield f"\n\n[cursor-bridge sdk error] run {result.id} failed"
        elif result.status == "cancelled":
            yield f"\n\n[cursor-bridge sdk error] run {result.id} cancelled"
    finally:
        await agent.close()
