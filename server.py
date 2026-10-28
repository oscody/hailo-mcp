"""hailo-mcp: a very small MCP server that lets agents use the Hailo-10H on this Pi.

Tools:
  hailo_status  device firmware, who holds /dev/hailo0, hailo-ollama health, models
  hailo_ask     run one short prompt on a Hailo LLM through hailo-ollama

Design notes (measured on this Pi, hailo-ollama 5.1.1):
  * hailo-ollama allows one connection plus one pending request, so every call
    takes a cross-process file lock before talking to it.
  * Context is about 2K tokens, so prompts are capped well below that and
    rejected with a clear error instead of a server 500.
  * Requests must be JSON with Content-Type set (httpx does this).
  * No escaping of newlines or backslashes is needed on this version.
"""

from __future__ import annotations

import asyncio
import fcntl
import logging
import os
import shutil
import time
from pathlib import Path

import httpx
from mcp.server.mcpserver import MCPServer
from mcp.server.mcpserver.exceptions import ToolError

HAILO_URL = os.environ.get("HAILO_OLLAMA_URL", "http://localhost:8000")
DEFAULT_MODEL = os.environ.get("HAILO_MODEL", "qwen2.5-instruct:1.5b")
MAX_INPUT_CHARS = int(os.environ.get("HAILO_MAX_INPUT_CHARS", "6000"))  # about 1,500 tokens
MAX_OUTPUT_TOKENS = 512
REQUEST_TIMEOUT_S = 240.0  # first call loads the model, about 70 s
QUEUE_TIMEOUT_S = float(os.environ.get("HAILO_QUEUE_TIMEOUT", "120"))
LOCK_PATH = Path(os.environ.get("XDG_RUNTIME_DIR", "/tmp")) / "hailo-mcp.lock"

mcp = MCPServer("hailo")
logging.getLogger("httpx").setLevel(logging.WARNING)


async def _acquire_lock() -> int:
    """Wait for the shared lock so only one request reaches hailo-ollama at a time."""
    fd = os.open(LOCK_PATH, os.O_CREAT | os.O_RDWR, 0o600)
    deadline = time.monotonic() + QUEUE_TIMEOUT_S
    while True:
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            return fd
        except BlockingIOError:
            if time.monotonic() > deadline:
                os.close(fd)
                raise ToolError(
                    f"Hailo is busy with another request (waited {QUEUE_TIMEOUT_S:.0f} s). Try again later."
                )
            await asyncio.sleep(0.5)


def _release_lock(fd: int) -> None:
    fcntl.flock(fd, fcntl.LOCK_UN)
    os.close(fd)


async def _run(*cmd: str) -> str:
    if not shutil.which(cmd[0]):
        return f"{cmd[0]} not found"
    proc = await asyncio.create_subprocess_exec(
        *cmd, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT
    )
    try:
        out, _ = await asyncio.wait_for(proc.communicate(), timeout=15)
    except asyncio.TimeoutError:
        proc.kill()
        return "timed out"
    return out.decode(errors="replace").strip()


@mcp.tool()
async def hailo_status() -> dict:
    """Report Hailo-10H health: firmware, which process holds the device,
    whether hailo-ollama is up, and which LLMs are downloaded or downloadable.
    Call this first if hailo_ask fails."""
    identify = await _run("hailortcli", "fw-control", "identify")
    firmware = next((l.split(":", 1)[1].strip() for l in identify.splitlines() if "Firmware Version" in l), None)
    holders = await _run("fuser", "-v", "/dev/hailo0")

    status: dict = {
        "device_firmware": firmware or identify[-300:],
        "device_holders": holders or "none",
        "hailo_ollama_url": HAILO_URL,
        "default_model": DEFAULT_MODEL,
        "limits": {"max_input_chars": MAX_INPUT_CHARS, "max_output_tokens": MAX_OUTPUT_TOKENS},
    }
    try:
        async with httpx.AsyncClient(timeout=10) as client:
            tags = (await client.get(f"{HAILO_URL}/api/tags")).json()
            library = (await client.get(f"{HAILO_URL}/hailo/v1/list")).json()
        status["hailo_ollama"] = "up"
        status["downloaded_models"] = [m["name"] for m in tags.get("models", [])]
        status["available_models"] = library.get("models", [])
    except (httpx.HTTPError, ValueError) as e:
        status["hailo_ollama"] = f"down or unreachable: {e}"
    return status


@mcp.tool()
async def hailo_ask(
    prompt: str,
    system: str | None = None,
    model: str | None = None,
    max_tokens: int = 256,
    temperature: float | None = None,
) -> dict:
    """Run one short prompt on a small LLM (1.5B to 3B) on the local Hailo-10H.
    Free and private, but small and slow (about 7 tokens/s; first call can take
    70 s to load the model). Good for: summarizing a short text, classifying,
    extracting a field, rewording, quick drafts. Not for: long documents,
    multi-step reasoning, or code you need to be correct.
    Input (system + prompt) is limited to about 6,000 characters; no chat history."""
    system = system or ""
    if not prompt.strip():
        raise ToolError("prompt is empty")
    size = len(prompt) + len(system)
    if size > MAX_INPUT_CHARS:
        raise ToolError(
            f"Input is {size} characters; the Hailo models only fit about {MAX_INPUT_CHARS}. "
            "Shorten or split it."
        )
    max_tokens = max(1, min(int(max_tokens), MAX_OUTPUT_TOKENS))

    messages = ([{"role": "system", "content": system}] if system else []) + [
        {"role": "user", "content": prompt}
    ]
    options: dict = {"num_predict": max_tokens}
    if temperature is not None:
        options["temperature"] = temperature
    body = {"model": model or DEFAULT_MODEL, "messages": messages, "stream": False, "options": options}

    fd = await _acquire_lock()
    started = time.monotonic()
    try:
        async with httpx.AsyncClient(timeout=REQUEST_TIMEOUT_S) as client:
            r = await client.post(f"{HAILO_URL}/api/chat", json=body)
    except httpx.ConnectError:
        raise ToolError(f"hailo-ollama is not reachable at {HAILO_URL}. Is the service running?")
    except httpx.TimeoutException:
        raise ToolError(
            f"hailo-ollama did not answer within {REQUEST_TIMEOUT_S:.0f} s. It may still be generating; "
            "wait before retrying."
        )
    finally:
        _release_lock(fd)

    if r.status_code != 200:
        raise ToolError(f"hailo-ollama returned HTTP {r.status_code}: {r.text[:300]}")
    data = r.json()
    tokens = data.get("eval_count") or 0
    seconds = time.monotonic() - started
    return {
        "text": (data.get("message") or {}).get("content", ""),
        "model": data.get("model", body["model"]),
        "output_tokens": tokens,
        "seconds": round(seconds, 1),
        "truncated": tokens >= max_tokens,
    }


if __name__ == "__main__":
    mcp.run("stdio")
