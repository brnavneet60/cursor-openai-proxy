"""
Cursor OpenAI Proxy — OpenAI-compatible chat API backed by Cursor.

Backends:
  - sdk (default): Cursor Python SDK local runtime in-process/pod
  - cli: Cursor Agent CLI (`agent -p`) subprocess

Exposes:
  GET  /health
  GET  /v1/models
  POST /v1/chat/completions

Auth: config.yaml / CURSOR_API_KEY / Authorization bearer.
SDK: https://cursor.com/docs/sdk/python
CLI: https://cursor.com/docs/cli/overview
"""

from __future__ import annotations

import atexit
import asyncio
import json
import logging
import os
import re
import shutil
import tempfile
import time
import uuid
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

import yaml
from fastapi import FastAPI, Header, HTTPException, Request
from fastapi.responses import JSONResponse, StreamingResponse
from pydantic import BaseModel

BASE_DIR = Path(__file__).resolve().parent
CONFIG_PATH = Path(os.environ.get("CURSOR_BRIDGE_CONFIG", str(BASE_DIR / "config.yaml")))
APP_VERSION = "0.2.0"

logger = logging.getLogger("cursor-openai-proxy")


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
# When true, run in a stable empty chat dir (pure chat; no project tools).
CHAT_ONLY = _cfg_bool("chat_only", "CURSOR_BRIDGE_CHAT_ONLY", True)
# Keep Cursor worker-server processes between requests (latency win).
REUSE_WORKERS = _cfg_bool("reuse_workers", "CURSOR_BRIDGE_REUSE_WORKERS", True)
# Emergency: kill workers before every request (old cold-start behavior).
FORCE_KILL_WORKERS = _cfg_bool(
    "force_kill_workers", "CURSOR_BRIDGE_FORCE_KILL_WORKERS", False
)
MAX_CONCURRENT = _cfg_int("max_concurrent", "CURSOR_BRIDGE_MAX_CONCURRENT", 1)
BACKEND = _cfg_str("backend", "CURSOR_BRIDGE_BACKEND", "sdk").lower()
if BACKEND not in {"sdk", "cli"}:
    raise RuntimeError(f"backend must be 'sdk' or 'cli', got {BACKEND!r}")
CURSOR_API_KEY = cursor_api_key_from_config()

# Process-lifetime chat workspace (avoids per-request temp dir churn).
_CHAT_WORKSPACE: Path | None = None
_SDK_CLIENT: Any = None


@asynccontextmanager
async def lifespan(app: FastAPI):
    global _SDK_CLIENT
    if BACKEND == "sdk":
        from sdk_runtime import launch_sdk_client

        ws = str(get_chat_workspace()) if CHAT_ONLY and not DEFAULT_WORKSPACE else (
            os.path.abspath(DEFAULT_WORKSPACE) if DEFAULT_WORKSPACE else os.getcwd()
        )
        try:
            _SDK_CLIENT = await launch_sdk_client(workspace=ws)
            app.state.sdk_client = _SDK_CLIENT
            logger.info("SDK local bridge ready workspace=%s", ws)
        except Exception:
            logger.exception("failed to launch Cursor SDK bridge")
            raise
    yield
    if _SDK_CLIENT is not None:
        try:
            await _SDK_CLIENT.aclose()
        except Exception:
            logger.exception("error closing SDK client")
        _SDK_CLIENT = None


app = FastAPI(title="cursor-openai-proxy", version=APP_VERSION, lifespan=lifespan)


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


_PREAMBLE_RE = re.compile(
    r"(?is)^\s*(?:"
    r"(?:i(?:'|’)?ll|let\s+me|i\s+will)\s+"
    r"(?:inspect|check|look\s+at|examine|search|explore)\b"
    r".*?"
    r"(?:\.|\n+)"
    r")+"
)


def sanitize_assistant_text(text: str) -> str:
    """Strip leading workspace-inspection / tool-narration preambles."""
    if not text:
        return text
    cleaned = _PREAMBLE_RE.sub("", text, count=1).lstrip()
    return cleaned if cleaned else text


def estimate_tokens(text: str) -> int:
    return max(1, len(text) // 4) if text else 0


def messages_to_prompt(messages: list[ChatMessage]) -> str:
    """Flatten OpenAI chat messages into a single CLI prompt."""
    lines: list[str] = [
        "You are answering via an OpenAI-compatible HTTP bridge.",
        "Reply with the final answer only.",
        "Do not narrate tools, plans, or workspace inspection.",
        "Do not say you will inspect/check the workspace.",
        "Start your reply with the answer content immediately.",
    ]
    if CHAT_ONLY:
        lines.append(
            "This session has no project files — answer from knowledge alone; "
            "do not attempt to read or search a codebase."
        )
    lines.append("")
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
        _AGENT_SEM = asyncio.Semaphore(max(1, MAX_CONCURRENT))
    return _AGENT_SEM


def _should_kill_workers_before_run() -> bool:
    return FORCE_KILL_WORKERS or not REUSE_WORKERS


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

    if _should_kill_workers_before_run():
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
    returncode = int(proc.returncode or 0)
    # On failure, clear workers so the next request starts clean.
    if returncode != 0:
        _kill_stale_agent_workers()
    return (
        returncode,
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
    prompt_text: str = "",
    completion_id: str | None = None,
    usage: dict[str, int] | None = None,
) -> dict[str, Any]:
    cid = completion_id or f"chatcmpl-{uuid.uuid4().hex[:24]}"
    created = int(time.time())
    if usage and (usage.get("completion_tokens") or usage.get("prompt_tokens")):
        ptok = int(usage.get("prompt_tokens") or 0)
        ctok = int(usage.get("completion_tokens") or 0)
        total = int(usage.get("total_tokens") or (ptok + ctok))
        estimated = False
    else:
        ctok = estimate_tokens(text)
        ptok = estimate_tokens(prompt_text)
        total = ptok + ctok
        estimated = True
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
            "prompt_tokens": ptok,
            "completion_tokens": ctok,
            "total_tokens": total,
        },
        "_token_estimate": estimated,
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
        if _should_kill_workers_before_run():
            await asyncio.to_thread(_kill_stale_agent_workers)
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
        failed = False

        try:
            while True:
                try:
                    line_b = await asyncio.wait_for(
                        proc.stdout.readline(),
                        timeout=AGENT_TIMEOUT_S,
                    )
                except TimeoutError:
                    failed = True
                    _kill_process_group(proc)
                    await asyncio.to_thread(_kill_stale_agent_workers)
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
                    piece = sanitize_assistant_text(delta_text) if not emitted_any else delta_text
                    if not piece:
                        continue
                    emitted_any = True
                    yield sse_chunk(
                        model=model,
                        completion_id=completion_id,
                        created=created,
                        delta={"content": piece},
                    )
                    continue

                if event.get("type") == "result" and not emitted_any:
                    result_text = sanitize_assistant_text(str(event.get("result") or ""))
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
            failed = True
            err = stderr_buf.decode("utf-8", errors="replace")[-2000:]
            yield sse_chunk(
                model=model,
                completion_id=completion_id,
                created=created,
                delta={"content": f"\n\n[cursor-bridge error exit={proc.returncode}] {err}"},
            )
        if failed:
            await asyncio.to_thread(_kill_stale_agent_workers)

    yield sse_chunk(
        model=model,
        completion_id=completion_id,
        created=created,
        delta={},
        finish_reason="stop",
    )
    yield "data: [DONE]\n\n"


def _cleanup_chat_workspace() -> None:
    global _CHAT_WORKSPACE
    if _CHAT_WORKSPACE is None:
        return
    try:
        shutil.rmtree(_CHAT_WORKSPACE, ignore_errors=True)
    finally:
        _CHAT_WORKSPACE = None


def get_chat_workspace() -> Path:
    """Stable empty workspace for chat_only mode (process lifetime)."""
    global _CHAT_WORKSPACE
    if _CHAT_WORKSPACE is not None and _CHAT_WORKSPACE.is_dir():
        return _CHAT_WORKSPACE
    root = Path(tempfile.gettempdir()) / "cursor-openai-bridge-chat"
    root.mkdir(parents=True, exist_ok=True)
    # Marker so operators can identify the dir.
    (root / ".cursor-openai-bridge").write_text(APP_VERSION + "\n", encoding="utf-8")
    _CHAT_WORKSPACE = root
    atexit.register(_cleanup_chat_workspace)
    return _CHAT_WORKSPACE


def prepare_workspace(
    header_workspace: str | None,
) -> tuple[str, None]:
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
        return str(get_chat_workspace()), None
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
    chat_ws = str(_CHAT_WORKSPACE) if _CHAT_WORKSPACE is not None else None
    if CHAT_ONLY and chat_ws is None and not DEFAULT_WORKSPACE:
        chat_ws = str(Path(tempfile.gettempdir()) / "cursor-openai-bridge-chat")
    return {
        "ok": True,
        "version": APP_VERSION,
        "backend": BACKEND,
        "sdk_bridge_ready": _SDK_CLIENT is not None,
        "config_path": str(CONFIG_PATH),
        "config_loaded": CONFIG_PATH.is_file(),
        "api_key_configured": bool(CURSOR_API_KEY),
        "agent_bin": AGENT_BIN,
        "agent_found": bool(agent_path),
        "agent_path": agent_path,
        "default_mode": DEFAULT_MODE,
        "chat_only": CHAT_ONLY,
        "chat_workspace": chat_ws,
        "reuse_workers": REUSE_WORKERS and not FORCE_KILL_WORKERS,
        "force_kill_workers": FORCE_KILL_WORKERS,
        "max_concurrent": MAX_CONCURRENT,
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

    workspace, _ = prepare_workspace(x_cursor_workspace)
    prompt = messages_to_prompt(body.messages)
    env = bridge_env(request)
    api_key = env.get("CURSOR_API_KEY") or CURSOR_API_KEY
    if not api_key:
        raise HTTPException(status_code=401, detail="CURSOR_API_KEY not configured")

    if BACKEND == "sdk":
        return await _chat_via_sdk(
            body=body,
            prompt=prompt,
            workspace=workspace,
            api_key=api_key,
            mode=mode,
        )

    cmd = build_agent_cmd(
        prompt=prompt,
        model=body.model,
        mode=mode,
        workspace=workspace,
        stream=body.stream,
    )
    estimate_headers = {"X-Cursor-Bridge-Token-Estimate": "1", "X-Cursor-Bridge-Backend": "cli"}

    if body.stream:

        async def _gen() -> AsyncIterator[str]:
            async for chunk in stream_agent_sse(cmd, env, body.model):
                yield chunk

        return StreamingResponse(
            _gen(),
            media_type="text/event-stream",
            headers={
                "Cache-Control": "no-cache",
                "Connection": "keep-alive",
                "X-Accel-Buffering": "no",
                **estimate_headers,
            },
        )

    result = await run_agent_json(cmd, env)
    text = sanitize_assistant_text(str(result.get("result") or ""))
    payload = openai_completion_response(
        model=body.model,
        text=text,
        prompt_text=prompt,
    )
    estimated = payload.pop("_token_estimate", True)
    headers = {**estimate_headers}
    if not estimated:
        headers.pop("X-Cursor-Bridge-Token-Estimate", None)
    return JSONResponse(payload, headers=headers)


async def _chat_via_sdk(
    *,
    body: ChatCompletionRequest,
    prompt: str,
    workspace: str,
    api_key: str,
    mode: str,
) -> Any:
    from sdk_runtime import sdk_complete, sdk_stream_text

    client = _SDK_CLIENT
    if client is None:
        raise HTTPException(status_code=503, detail="Cursor SDK bridge not ready")

    headers_base = {"X-Cursor-Bridge-Backend": "sdk"}

    if body.stream:
        completion_id = f"chatcmpl-{uuid.uuid4().hex[:24]}"
        created = int(time.time())

        async def _gen() -> AsyncIterator[str]:
            yield sse_chunk(
                model=body.model,
                completion_id=completion_id,
                created=created,
                delta={"role": "assistant"},
            )
            emitted = False
            try:
                async with agent_sem():
                    async for piece in sdk_stream_text(
                        client=client,
                        api_key=api_key,
                        prompt=prompt,
                        model=body.model,
                        workspace=workspace,
                        chat_only=CHAT_ONLY,
                        mode=mode,
                    ):
                        text = sanitize_assistant_text(piece) if not emitted else piece
                        if not text:
                            continue
                        emitted = True
                        yield sse_chunk(
                            model=body.model,
                            completion_id=completion_id,
                            created=created,
                            delta={"content": text},
                        )
            except Exception as exc:
                yield sse_chunk(
                    model=body.model,
                    completion_id=completion_id,
                    created=created,
                    delta={"content": f"\n\n[cursor-bridge sdk error] {exc}"},
                )
            yield sse_chunk(
                model=body.model,
                completion_id=completion_id,
                created=created,
                delta={},
                finish_reason="stop",
            )
            yield "data: [DONE]\n\n"

        return StreamingResponse(
            _gen(),
            media_type="text/event-stream",
            headers={
                "Cache-Control": "no-cache",
                "Connection": "keep-alive",
                "X-Accel-Buffering": "no",
                **headers_base,
            },
        )

    try:
        async with agent_sem():
            text, usage = await sdk_complete(
                client=client,
                api_key=api_key,
                prompt=prompt,
                model=body.model,
                workspace=workspace,
                chat_only=CHAT_ONLY,
                mode=mode,
            )
    except Exception as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc

    text = sanitize_assistant_text(text)
    payload = openai_completion_response(
        model=body.model,
        text=text,
        prompt_text=prompt,
        usage=usage,
    )
    estimated = payload.pop("_token_estimate", True)
    headers = {**headers_base}
    if estimated:
        headers["X-Cursor-Bridge-Token-Estimate"] = "1"
    return JSONResponse(payload, headers=headers)


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
