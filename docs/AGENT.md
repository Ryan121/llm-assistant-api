# `assist` — the agent CLI

An agentic coding session driven by the model this repo deploys. It reads,
searches and edits files, runs commands you approve, and checks its own work.

```bash
make venv                      # installs `assist` into .venv
assist                         # interactive, in any git repository
assist "add retry logic to _forward_json"     # one task, then exit
```

## Why a CLI and not an extension

Because the review surface already exists and is better than anything a
terminal can draw. Edits land in the working tree **uncommitted**, so:

- VS Code's gutter marks and Source Control panel show them live
- per-hunk stage and revert *is* the accept/reject UI
- `git diff`, `git add -p` and any difftool work unchanged

The CLI's job ends at "the edits are on disk and the tree is dirty". It ships
and versions with the gateway, so `make quickstart` gives you a complete
working system with no third-party extension in the dependency chain — which
is the lesson of Continue's shutdown.

## Where to run it

**On the machine holding the repository.** If the GPU box is remote, the
gateway is plain HTTP over the SSH tunnel you already have:

```bash
ssh -N -L 8081:127.0.0.1:8081 ubuntu@gpu-01     # leave running
assist                                          # on your laptop, in your repo
```

Only tokens cross the network; your source never leaves the machine it is on,
and VS Code sees every change locally.

## Configuration

Flags win, then the environment, then `.env` in the current directory.

| Flag | Falls back to | |
| --- | --- | --- |
| `--base-url` | `ASSIST_BASE_URL`, else `http://127.0.0.1:$API_PORT/v1` | Gateway |
| `--api-key` | `ASSIST_API_KEY`, else first entry of `API_KEYS` | Bearer token |
| `--model` | `MODEL_ID` | Model to request |
| `--cwd` | current directory | Workspace root |
| `--context-window` | `0` (off) | Compact the transcript before it overflows |
| `--yes` | — | Approve shell commands automatically. Unattended runs only. |
| `--allow-dirty` | — | Start with uncommitted changes already present |

Set `--context-window` to the engine's `MAX_MODEL_LEN`. Without it the session
runs until the gateway rejects it; with it, the transcript is compacted at 75%
and the run survives.

## The tools it has

| | |
| --- | --- |
| `list_files` | Orient in an unfamiliar tree, optionally by glob |
| `grep` | Regex over file contents, with line numbers |
| `read_file` | Read a text file — **required before editing it** |
| `edit_file` | Replace an exact, unique snippet |
| `write_file` | Create a file, or replace one wholesale |
| `run` | Shell command, **approved by you each time** |

Six tools, deliberately. Every tool costs prompt tokens on every turn of the
loop and adds another way for the model to go wrong.

## How edits are applied

`edit_file` takes `old_string` and `new_string`, and the applier is strict:

- **Exact match only.** No whitespace normalisation, no fuzzy fallback. A
  failed match returns an error *to the model*, which re-reads and retries.
- **Must be unique.** If the anchor appears more than once, the edit is
  refused and the line numbers are named, so the model adds context rather
  than guessing which occurrence you meant.
- **Must have been read this session**, which stops an edit built from a
  hallucinated recollection of the file.

Fuzzy matching trades a loud, recoverable failure for a silent wrong edit, and
a silent wrong edit in a multi-file change is expensive to find. The near-miss
case gets a specific hint, because it is nearly always indentation:

```
✗ old_string was not found in the file. A block matching this text apart from
  whitespace does exist - the difference is almost certainly indentation.
```

The diff you see is computed from the file before and after, never from the
model's description of what it did.

## Safety

| | |
| --- | --- |
| Path escapes | Every path is resolved and must be inside the workspace |
| Undo | Git. A clean tree is required unless you pass `--allow-dirty` |
| Shell | Every command prompts, showing the exact command |
| Commits | The system prompt forbids `git commit`/`push` — this repo's releases are driven by commit messages, so an agent writing them would mint bogus versions |
| Runaway loops | Hard cap of 40 steps per message |

## A session

```
$ assist "make prepare_payload apply the cap to max_completion_tokens too"
  Qwen/Qwen3-Coder-30B-A3B-Instruct via http://127.0.0.1:8081/v1
  workspace /home/ryan/llm-assistant-api
────────────────────────────────────────────────────────────────────────
⏺ grep  prepare_payload
⏺ read_file  src/llm_assistant_api/proxy.py
⏺ edit_file  src/llm_assistant_api/proxy.py
  @@ -117,7 +117,7 @@
  -        for field in ("max_tokens",):
  +        for field in ("max_tokens", "max_completion_tokens"):
⏺ run  make test
  ............................ 161 passed

Applied the cap to both fields; the tests still pass.

  src/llm_assistant_api/proxy.py | 2 +-
  ctx ~12k / 131k
```

Then review it the way you review anything else — `git diff`, or the Source
Control panel.

## For bigger runs, use a worktree

```bash
git worktree add ../repo-agent -b agent/refactor
assist --cwd ../repo-agent "..."
```

The change becomes a branch you review like a pull request, and your main
working tree is never touched.

## Limits

- **No retrieval yet.** `grep` and `list_files` only; there is no semantic
  search over the repo. Enabling `EMBEDDINGS_ENABLED` gives the gateway the
  endpoint for it, but the CLI does not use it yet.
- **No resume.** A session lives as long as the process.
- **It is not Cline.** Cline is more capable and more polished. This exists so
  the whole system is in one repo you control.
