# hailo-mcp

A very small MCP server that lets an agent (Claude Code, Hermes, ZeroClaw, any MCP client) hand short, cheap jobs to the LLM on this Pi's Hailo-10H through `hailo-ollama`.

## Tools

| Tool | What it does |
|---|---|
| `hailo_status` | Firmware, which process holds `/dev/hailo0`, whether `hailo-ollama` is up, downloaded and downloadable models |
| `hailo_ask` | One short prompt (plus optional system prompt) to a Hailo LLM. Returns text, token count, seconds, and whether it hit `max_tokens` |

## Guard rails

* A file lock (`$XDG_RUNTIME_DIR/hailo-mcp.lock`) serializes requests across every process using this server, because `hailo-ollama` allows one connection plus one pending request.
* Input over 6,000 characters (system + prompt) is rejected with a clear error instead of a `hailo-ollama` 500. The model context is about 2K tokens.
* Output is capped at 512 tokens. Request timeout is 240 s (the first call loads the model, about 70 s).

## Run

```bash
python3 -m venv .venv && .venv/bin/pip install "mcp>=2.3" httpx
.venv/bin/python smoke_test.py   # starts the server over stdio and calls every tool
```

## Connect

Claude Code:

```bash
claude mcp add hailo -- /home/homepi/Work/hailo-mcp/.venv/bin/python /home/homepi/Work/hailo-mcp/server.py
```

Hermes (`~/.hermes/config.yaml`):

```yaml
mcp_servers:
  hailo:
    command: /home/homepi/Work/hailo-mcp/.venv/bin/python
    args: ["/home/homepi/Work/hailo-mcp/server.py"]
```

## Settings (environment variables)

| Variable | Default |
|---|---|
| `HAILO_OLLAMA_URL` | `http://localhost:8000` |
| `HAILO_MODEL` | `qwen2.5-instruct:1.5b` |
| `HAILO_MAX_INPUT_CHARS` | `6000` |
| `HAILO_QUEUE_TIMEOUT` | `120` seconds to wait for the lock |
