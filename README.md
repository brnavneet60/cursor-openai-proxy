# Cursor OpenAI Proxy

**OpenAI-compatible `/v1/chat/completions` server backed by the official [Cursor Agent CLI](https://cursor.com/docs/cli/overview).**

Use your Cursor subscription from any client that speaks the OpenAI Chat Completions API — LangChain, LiteLLM, the OpenAI SDK, [kagent](https://kagent.dev), [agentgateway](https://agentgateway.dev), custom agents — without paying separately for OpenAI or Anthropic API keys.

> **Suggested GitHub repo name:** `cursor-openai-proxy`  
> (Also fine: `cursor-chat-proxy`, `cursor-llm-compat`. Avoid names that imply reverse‑engineering Cursor’s private HTTP APIs.)

---

## Motivation

Cursor already gives you access to strong coding models under a monthly plan. Teams building agent platforms often need a **standard OpenAI HTTP surface** so tools and gateways can plug in without custom SDKs.

Cursor’s supported automation paths are the **CLI** and **Agent SDK** — not a public OpenAI-shaped chat endpoint ([forum clarification](https://forum.cursor.com/t/using-cursor-frontier-models-like-composer-2-5-in-external-harnesses-e-g-codex/164676)). This project fills that gap **legitimately**:

| Approach | This project |
|----------|----------------|
| Reverse‑engineer Cursor private client APIs | **No** |
| Wrap the official `agent` CLI (`--print`) | **Yes** |
| Expose OpenAI Chat Completions + optional SSE | **Yes** |
| Ship Docker + Helm for Kubernetes | **Yes** |

**Opinion / fit:** Ideal for demos, local platforms (kind / Rancher Desktop), and agent meshes where you want “subscription as a model backend.” Not a replacement for dedicated inference (vLLM) or production multi-tenant LLM APIs.

---

## How it works

```text
OpenAI client / agent platform
        │  POST /v1/chat/completions
        ▼
 cursor-openai-proxy (FastAPI)
        │  agent -p --mode=ask --output-format json
        ▼
 Cursor Agent CLI  ──►  Cursor cloud (your plan)
```

- Default mode is **`ask`** (read-only Q&A).
- Default **`chat_only`** runs each request in an empty temp workspace (no project file access).
- Auth: `config.yaml` / `CURSOR_API_KEY` / optional `Authorization: Bearer`.

---

## Quick start (local)

### 1. Install Cursor CLI

```bash
curl https://cursor.com/install -fsS | bash
agent login
# or: export CURSOR_API_KEY=...   # https://cursor.com/dashboard/integrations
agent --list-models
```

### 2. Configure

```bash
cp config.example.yaml config.yaml
# Edit cursor_api_key (never commit config.yaml)
```

### 3. Run

```bash
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
python server.py
# → http://127.0.0.1:8765
```

### 4. Call it

```bash
curl -sS http://127.0.0.1:8765/v1/chat/completions \
  -H 'Content-Type: application/json' \
  -d '{"model":"auto","messages":[{"role":"user","content":"Say hi in one sentence"}]}'
```

```python
from openai import OpenAI

client = OpenAI(base_url="http://127.0.0.1:8765/v1", api_key="not-needed")
print(client.chat.completions.create(
    model="auto",
    messages=[{"role": "user", "content": "Say hi"}],
).choices[0].message.content)
```

---

## API

| Method | Path | Description |
|--------|------|-------------|
| `GET` | `/health` | Liveness + whether CLI / API key are configured |
| `GET` | `/v1/models` | Best-effort model list from `agent --list-models` |
| `POST` | `/v1/chat/completions` | OpenAI chat completions (`stream: true` supported) |

### Optional headers

| Header | Values | Purpose |
|--------|--------|---------|
| `X-Cursor-Mode` | `ask` \| `plan` \| `agent` | Override default CLI mode |
| `X-Cursor-Workspace` | absolute path | Run against a real repo (disables temp chat-only dir) |
| `Authorization` | `Bearer <key>` | Per-request Cursor API key override |

---

## Configuration

Priority for most settings: **environment variable → `config.yaml` → default**.

| Setting | Env | Default |
|---------|-----|---------|
| API key | `CURSOR_API_KEY` | from `cursor_api_key` in config |
| Bind host | `HOST` | `127.0.0.1` (use `0.0.0.0` in containers) |
| Port | `PORT` | `8765` |
| Mode | `CURSOR_BRIDGE_MODE` | `ask` |
| Chat-only temp workspace | `CURSOR_BRIDGE_CHAT_ONLY` | `true` |
| Backend (`sdk` \| `cli`) | `CURSOR_BRIDGE_BACKEND` | `sdk` |
| Agent binary | `CURSOR_AGENT_BIN` | `agent` |
| Agent timeout (seconds) | `CURSOR_BRIDGE_AGENT_TIMEOUT` | `120` |
| Max concurrent agent runs | `CURSOR_BRIDGE_MAX_CONCURRENT` | `1` |
| Reuse Cursor workers (warm, cli) | `CURSOR_BRIDGE_REUSE_WORKERS` | `true` |
| Force kill workers every request | `CURSOR_BRIDGE_FORCE_KILL_WORKERS` | `false` |
| Config file path | `CURSOR_BRIDGE_CONFIG` | `./config.yaml` |

See [`config.example.yaml`](config.example.yaml).

---

## Docker

```bash
docker build -t cursor-openai-proxy:0.2.0 .
docker run --rm -p 8765:8765 \
  -e CURSOR_API_KEY \
  -e HOST=0.0.0.0 \
  cursor-openai-proxy:0.2.0
```

The image installs the Cursor CLI via the [official installer](https://cursor.com/docs/cli/installation) (`curl | bash`). Review that script for production supply-chain hardening.

### Publish to GHCR (GitHub Actions)

On push to `main` / `master` (and on `v*` tags), [`.github/workflows/ghcr.yml`](.github/workflows/ghcr.yml) builds a multi-arch image and pushes to:

```text
ghcr.io/<GITHUB_OWNER>/cursor-openai-proxy:latest
ghcr.io/<GITHUB_OWNER>/cursor-openai-proxy:<tag>
```

No extra secrets are required — the workflow uses `GITHUB_TOKEN` with `packages: write`.

After the first successful run:

1. GitHub → **Packages** → `cursor-openai-proxy` → **Package settings** → set visibility (Public if you want anonymous pulls).
2. Confirm the package is linked to this repository.

**Helm:**

```bash
helm upgrade --install cursor-bridge ./chart \
  --namespace cursor-bridge \
  --set image.repository=ghcr.io/<GITHUB_OWNER>/cursor-openai-proxy \
  --set image.tag=latest \
  --set cursor.existingSecret=cursor-api \
  --set cursor.existingSecretKey=cursor-api-key
```

Manual `make ghcr-push` is optional for local publishes; CI is the intended path.

Official docs: [Working with the Container registry](https://docs.github.com/en/packages/working-with-a-github-packages-registry/working-with-the-container-registry).

---

## Kubernetes (Helm)

```bash
export CURSOR_API_KEY=cursor_...   # Dashboard → Integrations

kubectl create namespace cursor-bridge --dry-run=client -o yaml | kubectl apply -f -

# kind / Rancher Desktop: load the local image first
kind load docker-image cursor-openai-proxy:0.2.0   # if using kind

helm upgrade --install cursor-bridge ./chart \
  --namespace cursor-bridge \
  --set image.repository=cursor-openai-proxy \
  --set image.tag=0.2.0 \
  --set cursor.existingSecret=cursor-api \   # preferred
  --set cursor.existingSecretKey=cursor-api-key
```

Create the secret separately (recommended — avoids storing the key in Helm release values):

```bash
kubectl -n cursor-bridge create secret generic cursor-api \
  --from-literal=cursor-api-key="$CURSOR_API_KEY"
```

Or one-shot (key lands in Helm values history):

```bash
helm upgrade --install cursor-bridge ./chart \
  --namespace cursor-bridge \
  --set image.repository=cursor-openai-proxy \
  --set image.tag=0.2.0 \
  --set-string cursor.apiKey="$CURSOR_API_KEY"
```

**Makefile:** `make docker-build kind-load helm-install`

In-cluster OpenAI base URL:

```text
http://cursor-bridge-cursor-openai-bridge.cursor-bridge.svc.cluster.local:8765/v1
```

(Helm release name `cursor-bridge` + chart name produces that Service name.)

---

## Using with agent platforms

### agentgateway

Point an `AgentgatewayBackend` (`openai` provider) at the Service DNS, then expose a path (e.g. `/cursor`). Clients / ModelConfigs use:

```text
http://agentgateway-proxy.<ns>.svc:8090/cursor
```

(OpenAI SDK will call `{baseUrl}/chat/completions`.)

### kagent

```yaml
apiVersion: kagent.dev/v1alpha2
kind: ModelConfig
metadata:
  name: cursor-via-gateway
  namespace: kagent
spec:
  provider: OpenAI
  model: auto
  apiKeySecret: kagent-cursor-bridge   # can be a dummy "not-needed" key
  apiKeySecretKey: API_KEY
  openAI:
    baseUrl: http://agentgateway-proxy.agentgateway-system.svc.cluster.local:8090/cursor
```

---

## Limitations & policy

**Fact**

- Usage counts against your **Cursor plan** quotas.
- Requires **egress** to Cursor cloud (not offline inference).
- Latency is higher than a raw LLM API (CLI + agent harness).
- Not a full OpenAI API (no embeddings, images, Assistants API, etc.).
- Concurrent runs are limited (default **1**) to avoid wedged Cursor `worker-server` processes.

**Opinion**

- Prefer this for **agent/platform demos** and personal automation.
- Prefer **vLLM / Ollama / provider APIs** when you need raw inference, SLAs, or multi-tenant billing.
- Do **not** use unofficial proxies that hit Cursor’s private client endpoints — that conflicts with Cursor’s terms ([discussion](https://forum.cursor.com/t/using-cursor-frontier-models-like-composer-2-5-in-external-harnesses-e-g-codex/164676)).

---

## Security

- Never commit `config.yaml` or real API keys (gitignored).
- Prefer Kubernetes Secrets + `cursor.existingSecret` over `--set-string cursor.apiKey`.
- Keep `chat_only: true` unless you intentionally pass `X-Cursor-Workspace`.
- Rotate keys if they appear in shell history or chat logs.

---

## Project layout

```text
.
├── server.py              # FastAPI OpenAI-compatible server
├── config.example.yaml    # Template (copy → config.yaml)
├── requirements.txt
├── Dockerfile
├── Makefile
├── chart/                 # Helm chart
└── README.md
```

---

## License

MIT — see [LICENSE](LICENSE).

Cursor® is a trademark of Anysphere. This project is **not** affiliated with or endorsed by Anysphere / Cursor.
