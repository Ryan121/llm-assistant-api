# Connecting VS Code

The gateway speaks the OpenAI API, so every extension that accepts a custom
base URL works. Get your exact values with:

```bash
make vscode-config
```

That prints a ready-to-paste block for each extension below, filled in with
this deployment's port, model id and API key.

| | Base URL | Model | Key |
| --- | --- | --- | --- |
| Local box | `http://127.0.0.1:8080/v1` | value of `MODEL_ID` | first entry in `API_KEYS` |
| Remote GPU box | `http://<host>:8080/v1` | same | same |

For a remote host, generate the config with the right hostname baked in:

```bash
GATEWAY_HOST=gpu-01.internal make vscode-config
```

---

## Continue — recommended

Best all-round fit: one config covers chat, inline edit, agent mode and
autocomplete.

```bash
code --install-extension Continue.continue
make vscode-install     # writes ~/.continue/config.yaml, backing up any existing one
```

Then reload the window. The generated config looks like this:

```yaml
name: llm-assistant
version: 0.1.0
schema: v1
models:
  - name: Qwen3-Coder-30B-A3B-Instruct
    provider: openai
    model: Qwen/Qwen3-Coder-30B-A3B-Instruct
    apiBase: http://127.0.0.1:8080/v1
    apiKey: sk-local-...
    roles: [chat, edit, apply]
    defaultCompletionOptions:
      contextLength: 131072
      maxTokens: 8192
    capabilities:
      - tool_use
context:
  - provider: file
  - provider: code
  - provider: diff
  - provider: terminal
  - provider: problems
  - provider: codebase
```

`capabilities: [tool_use]` is what unlocks agent mode. Without it Continue
treats the model as chat-only.

If `AUTOCOMPLETE_ENABLED=true`, the generated config gains a second entry with
`roles: [autocomplete]` and `useLegacyCompletionsEndpoint: true` — inline
completion needs `/v1/completions`, not `/v1/chat/completions`.

## Cline / Roo Code / Kilo Code

The strongest agentic experience, if you mainly want "describe a change, let it
edit files and run commands".

1. Install the extension, open its settings.
2. **API Provider** → `OpenAI Compatible`
3. **Base URL** → `http://127.0.0.1:8080/v1`
4. **API Key** → your `API_KEYS` value
5. **Model ID** → your `MODEL_ID` value
6. Leave image support **off** (these are text-only coding models) and make
   sure function/tool calling is **on**.

## GitHub Copilot Chat (bring your own key)

Copilot Chat can be pointed at a local model while keeping the familiar UI:

**Chat panel** → model picker → **Manage Models** → **OpenAI Compatible** →
base URL `http://127.0.0.1:8080/v1`, then paste your key and pick the model.

Note this replaces the *chat* model only; Copilot's inline completions still
come from GitHub's own service.

## Zed, Aider, or anything using the OpenAI SDK

```bash
export OPENAI_BASE_URL=http://127.0.0.1:8080/v1
export OPENAI_API_KEY=sk-local-...      # from .env

aider --model openai/Qwen/Qwen3-Coder-30B-A3B-Instruct
```

Because `MODEL_ALIAS_FALLBACK=true`, a client that insists on sending
`gpt-4o` still gets served by your model rather than a 404. Set it to `false`
if you would rather catch misconfigured clients loudly.

---

## Troubleshooting

**"Connection refused" / nothing happens.**

```bash
make health     # is the gateway up?
make ps         # are both containers running?
```

**401 in the extension.** The key must be the *first* comma-separated entry of
`API_KEYS`, verbatim. Re-run `make vscode-config` and copy from there.

**Chat works, agent mode does nothing.** The tool-call parser does not match
the model family. Check it:

```bash
make smoke      # fails explicitly if no tool_calls come back
```

Then fix `VLLM_TOOL_ARGS` per the table in
[MODEL_SELECTION.md](MODEL_SELECTION.md#match-the-tool-call-parser-to-the-model-family)
and `make restart`.

**First request takes minutes, then works.** The model is still loading.

```bash
make wait       # blocks with progress until /readyz goes green
```

**Requests fail once the context gets long.** The editor is sending more tokens
than `MAX_MODEL_LEN`. Either raise it (and accept fewer concurrent requests) or
lower `contextLength` in the extension.

**Timeouts on big agent runs.** Raise `REQUEST_TIMEOUT_SECONDS` in `.env`,
then `make restart-api`. It only rebuilds the gateway, so the model stays
loaded.

**Everything is slow with several people using it.** Check the concurrency
vLLM actually achieved and trade context length for slots:

```bash
make logs-vllm | grep -i "Maximum concurrency"
```
