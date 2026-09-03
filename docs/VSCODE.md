# Connecting an editor

The gateway speaks the OpenAI API, so anything that takes a custom base URL
works. What changed in 2026 is which of those things still has a maintainer:

| | |
| --- | --- |
| **Continue** | Acquired by Cursor, June 2026. Final release `v2.0.0-vscode`, repo read-only. Still installs and runs offline against this gateway; no longer maintained. |
| **Roo Code** | Archived, May 2026. |
| **Cline** | Active. The recommended extension here. |
| **Kilo Code** | Active, MIT, Cline descendant. Good hedge. |

No single extension now covers everything Continue did, so split the roles:

| Job | Use |
| --- | --- |
| Agentic sessions | [`assist`](AGENT.md), the CLI in this repo — or Cline |
| Chat, inline edit | Cline |
| Inline autocomplete | Twinny or Tabby against `/v1/completions` |

The autocomplete gap is real: nothing in the Cline family does inline
completion, which makes the `AUTOCOMPLETE_ENABLED` upstream more useful than
it was, not less.

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

## Cline — the recommended extension

```bash
code --install-extension saoudrizwan.claude-dev
```

Then in its settings:

| | |
| --- | --- |
| API Provider | `OpenAI Compatible` |
| Base URL | `http://127.0.0.1:8081/v1` |
| API Key | first entry of `API_KEYS` |
| Model ID | your `MODEL_ID` |
| Context window | your `MAX_MODEL_LEN` |

Turn image support **off** — this is a text-only coding model — and make sure
function/tool calling is **on**, or agent mode silently never calls a tool.

Cline sends a large fixed system prompt, which would normally be expensive.
This deployment runs `--enable-prefix-caching`, so after the first request that
prefix is nearly free. Qwen3-Coder was also trained against Cline's agent
format, which is the same reason `--tool-call-parser qwen3_coder` is the right
setting here.

**Kilo Code** is configured identically and is worth preferring if you want MIT
licensing or per-mode model selection.

## `assist` — the agent CLI in this repo

No extension, no third-party dependency, and it ships with the gateway:

```bash
make venv
assist            # in any git repository
```

Edits land uncommitted in the working tree, so you review them in VS Code's
Source Control panel. Full documentation: **[docs/AGENT.md](AGENT.md)**.

## Inline autocomplete

Nothing in the Cline family does inline completion. Enable the small FIM model
and point a dedicated extension at it:

```bash
# .env
AUTOCOMPLETE_ENABLED=true
make up PROFILES="--profile autocomplete"
```

| | |
| --- | --- |
| Base URL | `http://127.0.0.1:8081/v1` |
| Model | your `AUTOCOMPLETE_MODEL_ID` |
| Endpoint | `/completions` — **not** `/chat/completions` |

**Twinny** is the closest drop-in. **Tabby** is more capable but wants to be
its own server, so it sits beside this stack rather than behind the gateway.

Autocomplete traffic gets its own short timeout
(`AUTOCOMPLETE_TIMEOUT_SECONDS`, default 5s) so a keystroke you have already
typed past cannot hold a KV-cache slot for fifteen minutes. If you also set
`--scheduling-policy priority` in `VLLM_EXTRA_ARGS` and
`PRIORITY_ROUTING_ENABLED=true`, completions jump the queue ahead of long agent
runs.

## Continue — still works, no longer maintained

One config covers chat, inline edit, apply, agent mode and autocomplete, which
is still unmatched. It is Apache 2.0 and runs entirely offline against this
gateway — it never needed Continue's cloud for a local model. Use it if that
single-config convenience outweighs an unmaintained extension.

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
the section above. Use Cline instead.

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
because extensions cache their configuration.

**Chat works, agent mode does nothing.** The tool-call parser does not match
the model family. Check it:

```bash
make smoke      # fails explicitly if no tool_calls come back
```

Then fix `VLLM_TOOL_ARGS` per the table in
[MODEL_SELECTION.md](MODEL_SELECTION.md#match-the-tool-call-parser-to-the-model-family)
and `make restart`. In Cline, confirm function/tool calling is enabled for the
provider; in Continue, that `capabilities: [tool_use]` is on the model entry.

**First request takes minutes, then works.** The model is still loading.

```bash
make wait       # blocks with progress until /readyz goes green
```

**Requests fail once the context gets long.** The editor is sending more tokens
than `MAX_MODEL_LEN`. Either raise it (and accept fewer concurrent requests) or
lower the context window configured in the extension.

Set `CONTEXT_GUARD_TOKENS` to the same value as `MAX_MODEL_LEN` and the gateway
rejects those requests itself, with a message naming the budget and the code
`context_length_exceeded` — which agent clients recognise and respond to by
compacting their transcript, instead of dying on an opaque engine error.

**Timeouts on big agent runs.** Raise `REQUEST_TIMEOUT_SECONDS` in `.env`,
then `make restart-api`. It only rebuilds the gateway, so the model stays
loaded.

**Everything is slow with several people using it.** Check the concurrency
vLLM actually achieved and trade context length for slots:

```bash
make logs-vllm | grep -i "Maximum concurrency"
```
