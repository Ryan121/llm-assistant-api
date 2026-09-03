# Connecting VS Code

**Use Continue.** It is the documented path here, and the rest of this page
explains why the alternatives are worse for a self-hosted model.

The gateway speaks the OpenAI API, so anything that accepts a custom base URL
will work — but Continue is the only option that needs no third-party account,
covers chat *and* autocomplete, and can be configured for you from `.env`.

## Step 0 — get your values

On the GPU host:

```bash
make vscode-config
```

That prints everything below, filled in with this deployment's port, model id
and API key.

| | Base URL | Model | Key |
| --- | --- | --- | --- |
| Same machine as VS Code | `http://127.0.0.1:8081/v1` | value of `MODEL_ID` | first entry in `API_KEYS` |
| Remote GPU box, tunnelled | `http://127.0.0.1:8081/v1` | same | same |
| Remote GPU box, direct | `http://<host>:8081/v1` | same | same |

For the direct case, bake the hostname in:

```bash
GATEWAY_HOST=gpu-01.internal make vscode-config
```

## Step 1 — reach the gateway

Skip this if VS Code runs on the GPU box.

The gateway speaks plain HTTP; there is no TLS in front of it. So either keep
it on a trusted LAN, or — recommended — tunnel over SSH and treat it as local:

```bash
ssh -N -L 8081:127.0.0.1:8081 ubuntu@gpu-01
```

Leave that running. The editor then uses `http://127.0.0.1:8081/v1`, and the
API key stops being the only thing between your GPUs and the network.

Confirm it before touching the editor — this one command separates "the stack
is broken" from "my config is wrong":

```bash
curl -s -H 'Authorization: Bearer sk-local-...' http://127.0.0.1:8081/v1/models
# {"object":"list","data":[{"id":"Qwen/Qwen3-Coder-30B-A3B-Instruct",...}]}
```

---

## Continue — the recommended path

One config covers chat, inline edit, apply, agent mode and autocomplete.

```bash
code --install-extension Continue.continue
make vscode-install     # writes ~/.continue/config.yaml, backing up any existing one
```

Reload the window and you are done. `make vscode-install` needs the repo
checked out on the machine running VS Code; if it is not, create
`~/.continue/config.yaml` by hand from the `make vscode-config` output:

```yaml
name: llm-assistant
version: 0.1.0
schema: v1
models:
  - name: Qwen3-Coder-30B-A3B-Instruct
    provider: openai                            # wire format, not the vendor
    model: Qwen/Qwen3-Coder-30B-A3B-Instruct    # your MODEL_ID
    apiBase: http://127.0.0.1:8081/v1           # the /v1 matters
    apiKey: sk-local-...                        # first entry of API_KEYS
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

Two fields matter more than they look:

- **`capabilities: [tool_use]`** is what unlocks agent mode. Without it
  Continue treats the model as chat-only and silently never calls a tool.
- **`provider: openai`** is correct even though this is not OpenAI. It selects
  the request format, and the gateway speaks it.

If `AUTOCOMPLETE_ENABLED=true`, the generated config gains a second entry with
`roles: [autocomplete]` and `useLegacyCompletionsEndpoint: true` — inline
completion needs `/v1/completions`, not `/v1/chat/completions`.

## Cline / Roo Code / Kilo Code

Worth adding *alongside* Continue if you want heavier autonomy — "describe a
change, let it edit files and run commands". No account needed either.

1. Install the extension, open its settings.
2. **API Provider** → `OpenAI Compatible`
3. **Base URL** → `http://127.0.0.1:8081/v1`
4. **API Key** → your `API_KEYS` value
5. **Model ID** → your `MODEL_ID` value
6. Leave image support **off** (these are text-only coding models) and make
   sure function/tool calling is **on**.

## The built-in VS Code chat — only if you already have Copilot

Reach for this only when you are already paying for Copilot and want the native
UI. Otherwise it is strictly more setup for strictly less function.

**The chat view, the model picker and the whole "bring your own key" flow are
contributed by the GitHub Copilot Chat extension — none of it ships with VS
Code.** If you cannot find a model picker, that is why, and there is nothing to
configure yet. You would need to:

```bash
code --list-extensions | grep -i copilot     # expect github.copilot-chat
code --install-extension GitHub.copilot
code --install-extension GitHub.copilot-chat
```

then sign in via **Accounts** (bottom-left) → *Sign in to use GitHub Copilot*.
A free-tier entitlement is enough, but without one the chat view stays in its
sign-in state and no picker appears. You also need a recent VS Code — custom
OpenAI-compatible endpoints are a fairly new addition (**Code → About** to
check).

With all that in place:

1. Open **Chat** (`Ctrl/Cmd+Alt+I`).
2. Click the **model picker** at the bottom of the chat input box.
3. **Manage Models…** → **OpenAI Compatible**.
4. **Base URL** → `http://127.0.0.1:8081/v1`
5. **API key** → your `API_KEYS` value.
6. Tick your model in the list, then select it in the picker.

VS Code fills step 6 by calling `GET /v1/models` on the gateway, so the entry
you see is whatever `MODEL_ID` is set to.

Then the limits:

- **Chat only.** This replaces the chat model; ghost-text inline completions
  still come from GitHub's service. Fully local autocomplete needs Continue.
- **Still tied to GitHub.** The weights are yours, but the UI depends on a
  signed-in account and a live service.
- **The provider label moves between releases** — sometimes "OpenAI
  Compatible", sometimes "OpenAI" with an editable base URL. If your build
  offers no base-URL field at all, it cannot target a custom endpoint.

> Not to be confused with the **Claude Code** panel, if you have that
> installed. It talks to Anthropic's API and has no OpenAI-compatible endpoint
> setting, so it is not the view to configure here.

## Zed, Aider, or anything using the OpenAI SDK

```bash
export OPENAI_BASE_URL=http://127.0.0.1:8081/v1
export OPENAI_API_KEY=sk-local-...      # from .env

aider --model openai/Qwen/Qwen3-Coder-30B-A3B-Instruct
```

Because `MODEL_ALIAS_FALLBACK=true`, a client that insists on sending `gpt-4o`
still gets served by your model rather than a 404. Set it to `false` if you
would rather catch misconfigured clients loudly.

---

## Troubleshooting

**No model picker in the chat view.** That is Copilot Chat, not VS Code — see
the section above. Use Continue instead.

**"Connection refused" / nothing happens.**

```bash
make health     # is the gateway up?
make ps         # are both containers running?
```

If VS Code is on another machine, check the SSH tunnel is still open.

**401 in the extension.** Three places can disagree — `.env`, the running
container, and the editor's config. Don't guess which:

```bash
make check-auth
```

That prints a non-secret fingerprint of the key on each side and then makes a
live request, so it tells you exactly which one is wrong.

The single most common cause: **the gateway reads `API_KEYS` once at startup**,
so editing `.env` without recreating the container leaves it on the old value.
`make restart-api` fixes it and leaves the model loaded.

Otherwise the key must be the *first* comma-separated entry of `API_KEYS`,
verbatim — re-run `make vscode-config`, then **reload the VS Code window**,
because Continue caches its config.

**Chat works, agent mode does nothing.** The tool-call parser does not match
the model family. Check it:

```bash
make smoke      # fails explicitly if no tool_calls come back
```

Then fix `VLLM_TOOL_ARGS` per the table in
[MODEL_SELECTION.md](MODEL_SELECTION.md#match-the-tool-call-parser-to-the-model-family)
and `make restart`. In Continue, also confirm `capabilities: [tool_use]` is
present on the model entry.

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
