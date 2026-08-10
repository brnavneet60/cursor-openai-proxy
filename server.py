"""
Cursor OpenAI Proxy — OpenAI-compatible chat API backed by Cursor Agent CLI.

Exposes:
  GET  /health
  GET  /v1/models
  POST /v1/chat/completions

Auth: config.yaml / CURSOR_API_KEY / Authorization bearer.
CLI: https://cursor.com/docs/cli/overview
"""

from __future__ import annotations

import asyncio
import json
import os
import shutil
import tempfile
import time
import uuid
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

import yaml
from fastapi import FastAPI, Header, HTTPException, Request
from fastapi.responses import JSONResponse, StreamingResponse
from pydantic import BaseModel

BASE_DIR = Path(__file__).resolve().parent
CONFIG_PATH = Path(os.environ.get("CURSOR_BRIDGE_CONFIG", str(BASE_DIR / "config.yaml")))


def load_config(path: Path = CONFIG_PATH) -> dict[str, Any]:
    """Load settings from YAML config. Missing file → empty dict."""
    if not path.is_file():
        return {}
    with path.open("r", encoding="utf-8") as fh:
        data = yaml.safe_load(fh) or {}
    if not isinstance(data, dict):
        raise RuntimeError(f"Config must be a mapping: {path}")
    return data


CONFIG = load_config()


def _cfg_str(key: str, env_key: str, default: str) -> str:
    if env_key in os.environ and os.environ[env_key] != "":
        return os.environ[env_key]
    val = CONFIG.get(key)
    if val is None or val == "":
        return default
    return str(val)


def _cfg_bool(key: str, env_key: str, default: bool) -> bool:
    if env_key in os.environ and os.environ[env_key] != "":
        return os.environ[env_key].lower() in {"1", "true", "yes"}
    val = CONFIG.get(key)
    if val is None or val == "":
        return default
    if isinstance(val, bool):
        return val
    return str(val).lower() in {"1", "true", "yes"}


def _cfg_int(key: str, env_key: str, default: int) -> int:
    if env_key in os.environ and os.environ[env_key] != "":
        return int(os.environ[env_key])
    val = CONFIG.get(key)
    if val is None or val == "":
        return default
    return int(val)


def cursor_api_key_from_config() -> str:
    """API key from config.yaml (or CURSOR_API_KEY env override)."""
    key = _cfg_str("cursor_api_key", "CURSOR_API_KEY", "")
    if key in {"cursor_REPLACE_ME", "REPLACE_ME"}:
        return ""
    return key.strip()


AGENT_BIN = _cfg_str("agent_bin", "CURSOR_AGENT_BIN", "agent")
DEFAULT_MODE = _cfg_str("mode", "CURSOR_BRIDGE_MODE", "ask")  # ask | plan | agent
DEFAULT_WORKSPACE = _cfg_str("workspace", "CURSOR_BRIDGE_WORKSPACE", "")
PORT = _cfg_int("port", "PORT", 8765)
HOST = _cfg_str("host", "HOST", "127.0.0.1")
# When true, run in a fresh empty temp dir (pure chat; no project tools).
CHAT_ONLY = _cfg_bool("chat_only", "CURSOR_BRIDGE_CHAT_ONLY", True)
CURSOR_API_KEY = cursor_api_key_from_config()

app = FastAPI(title="cursor-openai-proxy", version="0.1.3")


class ChatMessage(BaseModel):
    role: str
    content: str | list[dict[str, Any]] | None = None


class ChatCompletionRequest(BaseModel):
    model: str = "auto"
    messages: list[ChatMessage]
    stream: bool = False
    temperature: float | None = None
    max_tokens: int | None = None


def _content_to_text(content: str | list[dict[str, Any]] | None) -> str:
    if content is None:
        return ""
    if isinstance(content, str):
        return content
    parts: list[str] = []
    for block in content:
        if isinstance(block, dict) and block.get("type") == "text":
            parts.append(str(block.get("text", "")))
        elif isinstance(block, dict) and "text" in block:
            parts.append(str(block["text"]))
    return "\n".join(parts)


def messages_to_prompt(messages: list[ChatMessage]) -> str:
    """Flatten OpenAI chat messages into a single CLI prompt."""
    lines: list[str] = [
        "You are answering via an OpenAI-compatible HTTP bridge.",
        "Reply with the final answer only — no tool narration unless asked.",
        "",
    ]
    for msg in messages:
        role = msg.role.upper()
        text = _content_to_text(msg.content).strip()
        if not text:
            continue
        lines.append(f"{role}:\n{text}\n")
    lines.append("ASSISTANT:")
    return "\n".join(lines)


def resolve_agent_bin() -> str:
    path = shutil.which(AGENT_BIN) or AGENT_BIN
    if not shutil.which(AGENT_BIN) and not os.path.isfile(AGENT_BIN):
        raise HTTPException(
            status_code=503,
            detail=(
                f"Cursor CLI '{AGENT_BIN}' not found on PATH. "
                "Install: curl https://cursor.com/install -fsS | bash"
            ),
        )
    return path


def build_agent_cmd(
    *,
    prompt: str,
    model: str,
    mode: str,
    workspace: str,
    stream: bool,
) -> list[str]:
    cmd = [
        resolve_agent_bin(),
        "-p",
        "--trust",
        f"--workspace={workspace}",
        f"--output-format={'stream-json' if stream else 'json'}",
    ]
    if stream:
        cmd.append("--stream-partial-output")
    if mode in {"ask", "plan"}:
        cmd.append(f"--mode={mode}")
    # agent mode is default when --mode is omitted
    if model and model not in {"auto", "default"}:
        cmd.append(f"--model={model}")
    cmd.append(prompt)
    return cmd


AGENT_TIMEOUT_S = _cfg_int("agent_timeout_seconds", "CURSOR_BRIDGE_AGENT_TIMEOUT", 120)
_AGENT_SEM: asyncio.Semaphore | None = None


def agent_sem() -> asyncio.Semaphore:
    """Lazy semaphore bound to the running event loop (not import-time)."""
    global _AGENT_SEM
    if _AGENT_SEM is None:
        _AGENT_SEM = asyncio.Semaphore(
            int(os.environ.get("CURSOR_BRIDGE_MAX_CONCURRENT", "1"))
        )
    return _AGENT_SEM


def _kill_stale_agent_workers() -> None:
    """Cursor agent leaves worker-server node processes that can wedge later calls."""
    proc_root = Path("/proc")
    if not proc_root.is_dir():
        return
    my_pid = os.getpid()
    for entry in proc_root.iterdir():
        if not entry.name.isdigit():
            continue
        pid = int(entry.name)
        if pid == my_pid:
            continue
        try:
            cmd = (entry / "cmdline").read_bytes().decode("utf-8", errors="ignore")
        except OSError:
            continue
        if "cursor-agent" not in cmd and "cursor-agent/versions" not in cmd:
            continue
        try:
            os.kill(pid, 9)
        except OSError:
            pass


def _run_agent_sync(cmd: list[str], env: dict[str, str]) -> tuple[int, str, str]:
    """Run agent CLI in a worker thread (more reliable under uvicorn than raw asyncio)."""
    import subprocess

    _kill_stale_agent_workers()
    proc = subprocess.Popen(
        cmd,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        env=env,
        start_new_session=True,
    )
    try:
        stdout_b, stderr_b = proc.communicate(timeout=AGENT_TIMEOUT_S)
    except subprocess.TimeoutExpired:
        _kill_process_group_pid(proc.pid)
        _kill_stale_agent_workers()
        try:
            proc.kill()
        except OSError:
            pass
        proc.wait(timeout=5)
        raise TimeoutError(f"agent timed out after {AGENT_TIMEOUT_S}s") from None
    return (
        int(proc.returncode or 0),
        stdout_b.decode("utf-8", errors="replace").strip(),
        stderr_b.decode("utf-8", errors="replace").strip(),
    )


def _kill_process_group_pid(pid: int) -> None:
    try:
        os.killpg(pid, 9)
    except (ProcessLookupError, PermissionError, OSError):
        try:
            os.kill(pid, 9)
        except OSError:
            pass


def _kill_process_group(proc: asyncio.subprocess.Process) -> None:
    if proc.pid is None:
        return
    _kill_process_group_pid(proc.pid)


async def run_agent_json(cmd: list[str], env: dict[str, str]) -> dict[str, Any]:
    async with agent_sem():
        try:
            returncode, stdout, stderr = await asyncio.to_thread(
                _run_agent_sync, cmd, env
            )
        except TimeoutError:
            raise HTTPException(
                status_code=504,
                detail={
                    "message": "Cursor agent CLI timed out",
                    "timeout_seconds": AGENT_TIMEOUT_S,
                },
            ) from None
        if returncode != 0:
            raise HTTPException(
                status_code=502,
                detail={
                    "message": "Cursor agent CLI failed",
                    "exit_code": returncode,
                    "stderr": stderr[-4000:],
                    "stdout": stdout[-2000:],
                },
            )
        try:
            return json.loads(stdout)
        except json.JSONDecodeError as exc:
            raise HTTPException(
                status_code=502,
                detail={
                    "message": "Failed to parse agent JSON output",
                    "stdout": stdout[-2000:],
                    "stderr": stderr[-2000:],
                    "error": str(exc),
                },
            ) from exc


def openai_completion_response(
    *,
    model: str,
    text: str,
    completion_id: str | None = None,
) -> dict[str, Any]:
    cid = completion_id or f"chatcmpl-{uuid.uuid4().hex[:24]}"
    created = int(time.time())
    return {
        "id": cid,
        "object": "chat.completion",
        "created": created,
        "model": model,
        "choices": [
            {
                "index": 0,
                "message": {"role": "assistant", "content": text},
                "finish_reason": "stop",
            }
        ],
        "usage": {
            "prompt_tokens": 0,
            "completion_tokens": 0,
            "total_tokens": 0,
        },
    }


def sse_chunk(
    *,
    model: str,
    completion_id: str,
    created: int,
    delta: dict[str, Any],
    finish_reason: str | None = None,
) -> str:
    payload = {
        "id": completion_id,
        "object": "chat.completion.chunk",
        "created": created,
        "model": model,
        "choices": [
            {
                "index": 0,
                "delta": delta,
                "finish_reason": finish_reason,
            }
        ],
    }
    return f"data: {json.dumps(payload, ensure_ascii=False)}\n\n"


def extract_assistant_delta_text(event: dict[str, Any]) -> str | None:
    """
    Per Cursor docs: with --stream-partial-output, only use assistant events
    that have timestamp_ms and do NOT have model_call_id.
    """
    if event.get("type") != "assistant":
        return None
    if "timestamp_ms" not in event:
        return None
    if "model_call_id" in event:
        return None
    message = event.get("message") or {}
    content = message.get("content") or []
    parts: list[str] = []
    for block in content:
        if isinstance(block, dict) and block.get("type") == "text":
            parts.append(str(block.get("text", "")))
    return "".join(parts) if parts else None


async def stream_agent_sse(
    cmd: list[str],
    env: dict[str, str],
    model: str,
) -> AsyncIterator[str]:
    completion_id = f"chatcmpl-{uuid.uuid4().hex[:24]}"
    created = int(time.time())
    yield sse_chunk(
        model=model,
        completion_id=completion_id,
        created=created,
        delta={"role": "assistant"},
    )

    async with agent_sem():
        proc = await asyncio.create_subprocess_exec(
            *cmd,
            stdin=asyncio.subprocess.DEVNULL,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            env=env,
            start_new_session=True,
        )
        assert proc.stdout is not None
        stderr_buf = bytearray()

        async def _drain_stderr() -> None:
            assert proc.stderr is not None
            while True:
                chunk = await proc.stderr.read(4096)
                if not chunk:
                    break
                stderr_buf.extend(chunk)

        stderr_task = asyncio.create_task(_drain_stderr())
        emitted_any = False

        try:
            while True:
                try:
                    line_b = await asyncio.wait_for(
                        proc.stdout.readline(),
                        timeout=AGENT_TIMEOUT_S,
                    )
                except TimeoutError:
                    _kill_process_group(proc)
                    yield sse_chunk(
                        model=model,
                        completion_id=completion_id,
                        created=created,
                        delta={
                            "content": f"\n\n[cursor-bridge error] agent timed out after {AGENT_TIMEOUT_S}s"
                        },
                    )
                    break
                if not line_b:
                    break
                line = line_b.decode("utf-8", errors="replace").strip()
                if not line:
                    continue
                try:
                    event = json.loads(line)
                except json.JSONDecodeError:
                    continue

                delta_text = extract_assistant_delta_text(event)
                if delta_text:
                    emitted_any = True
                    yield sse_chunk(
                        model=model,
                        completion_id=completion_id,
                        created=created,
                        delta={"content": delta_text},
                    )
                    continue

                if event.get("type") == "result" and not emitted_any:
                    result_text = str(event.get("result") or "")
                    if result_text:
                        yield sse_chunk(
                            model=model,
                            completion_id=completion_id,
                            created=created,
                            delta={"content": result_text},
                        )
                        emitted_any = True
        finally:
            if proc.returncode is None:
                _kill_process_group(proc)
            await proc.wait()
            await stderr_task

        if proc.returncode not in (0, None) and proc.returncode != -9:
            err = stderr_buf.decode("utf-8", errors="replace")[-2000:]
            yield sse_chunk(
                model=model,
                completion_id=completion_id,
                created=created,
                delta={"content": f"\n\n[cursor-bridge error exit={proc.returncode}] {err}"},
            )

    yield sse_chunk(
        model=model,
        completion_id=completion_id,
        created=created,
        delta={},
        finish_reason="stop",
    )
    yield "data: [DONE]\n\n"


def prepare_workspace(
    header_workspace: str | None,
) -> tuple[str, tempfile.TemporaryDirectory[str] | None]:
    if header_workspace:
        path = os.path.abspath(header_workspace)
        if not os.path.isdir(path):
            raise HTTPException(status_code=400, detail=f"Workspace not found: {path}")
        return path, None
    if DEFAULT_WORKSPACE:
        path = os.path.abspath(DEFAULT_WORKSPACE)
        if not os.path.isdir(path):
            raise HTTPException(
                status_code=500,
                detail=f"CURSOR_BRIDGE_WORKSPACE not a directory: {path}",
            )
        return path, None
    if CHAT_ONLY:
        tmp = tempfile.TemporaryDirectory(prefix="cursor-openai-bridge-")
        return tmp.name, tmp
    return os.getcwd(), None


def bridge_env(request: Request) -> dict[str, str]:
    env = os.environ.copy()
    # 1) config.yaml / CURSOR_API_KEY env (loaded at startup)
    if CURSOR_API_KEY:
        env["CURSOR_API_KEY"] = CURSOR_API_KEY
    # 2) per-request Authorization bearer overrides config
    auth = request.headers.get("authorization") or ""
    if auth.lower().startswith("bearer "):
        token = auth[7:].strip()
        if token and token.lower() not in {"not-needed", "unused", "none", "sk-local"}:
            env["CURSOR_API_KEY"] = token
    return env


def agent_env() -> dict[str, str]:
    """Env for CLI calls outside a request (e.g. /v1/models)."""
    env = os.environ.copy()
    if CURSOR_API_KEY:
        env["CURSOR_API_KEY"] = CURSOR_API_KEY
    return env


@app.get("/health")
async def health() -> dict[str, Any]:
    agent_path = shutil.which(AGENT_BIN)
    return {
        "ok": True,
        "config_path": str(CONFIG_PATH),
        "config_loaded": CONFIG_PATH.is_file(),
        "api_key_configured": bool(CURSOR_API_KEY),
        "agent_bin": AGENT_BIN,
        "agent_found": bool(agent_path),
        "agent_path": agent_path,
        "default_mode": DEFAULT_MODE,
        "chat_only": CHAT_ONLY,
    }


@app.get("/v1/models")
async def list_models() -> dict[str, Any]:
    """Best-effort model list via `agent --list-models` / `agent models`."""
    agent = resolve_agent_bin()
    created = int(time.time())
    models: list[dict[str, Any]] = [
        {"id": "auto", "object": "model", "created": created, "owned_by": "cursor"}
    ]

    for args in (["--list-models"], ["models"]):
        try:
            proc = await asyncio.create_subprocess_exec(
                agent,
                *args,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                env=agent_env(),
            )
            stdout_b, _ = await asyncio.wait_for(proc.communicate(), timeout=30)
            if proc.returncode != 0:
                continue
            text = stdout_b.decode("utf-8", errors="replace")
            # Parse free-form CLI output: collect non-empty tokens that look like ids.
            seen = {"auto"}
            for line in text.splitlines():
                line = line.strip()
                if not line or line.lower().startswith(("available", "model", "---", "name")):
                    continue
                # Common formats: "composer-2.5 ..." or JSON-ish
                token = line.split()[0].strip(",").strip('"')
                if token and token not in seen and len(token) < 80:
                    seen.add(token)
                    models.append(
                        {
                            "id": token,
                            "object": "model",
                            "created": created,
                            "owned_by": "cursor",
                        }
                    )
            if len(models) > 1:
                break
        except (TimeoutError, OSError):
            continue

    return {"object": "list", "data": models}


@app.post("/v1/chat/completions")
async def chat_completions(
    body: ChatCompletionRequest,
    request: Request,
    x_cursor_mode: str | None = Header(default=None, alias="X-Cursor-Mode"),
    x_cursor_workspace: str | None = Header(default=None, alias="X-Cursor-Workspace"),
) -> Any:
    if not body.messages:
        raise HTTPException(status_code=400, detail="messages must be non-empty")

    mode = (x_cursor_mode or DEFAULT_MODE).lower()
    if mode not in {"ask", "plan", "agent"}:
        raise HTTPException(
            status_code=400,
            detail="X-Cursor-Mode must be one of: ask, plan, agent",
        )

    workspace, tmp = prepare_workspace(x_cursor_workspace)
    prompt = messages_to_prompt(body.messages)
    cmd = build_agent_cmd(
        prompt=prompt,
        model=body.model,
        mode=mode,
        workspace=workspace,
        stream=body.stream,
    )
    env = bridge_env(request)

    try:
        if body.stream:
            async def _gen() -> AsyncIterator[str]:
                try:
                    async for chunk in stream_agent_sse(cmd, env, body.model):
                        yield chunk
                finally:
                    if tmp is not None:
                        tmp.cleanup()

            return StreamingResponse(
                _gen(),
                media_type="text/event-stream",
                headers={
                    "Cache-Control": "no-cache",
                    "Connection": "keep-alive",
                    "X-Accel-Buffering": "no",
                },
            )

        result = await run_agent_json(cmd, env)
        text = str(result.get("result") or "")
        return JSONResponse(openai_completion_response(model=body.model, text=text))
    finally:
        if tmp is not None and not body.stream:
            tmp.cleanup()


def main() -> None:
    import uvicorn

    uvicorn.run(
        "server:app",
        host=HOST,
        port=PORT,
        reload=False,
    )


if __name__ == "__main__":
    main()
