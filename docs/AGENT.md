# `assist` — the agent CLI

An agentic coding session driven by the model this repo deploys. It reads,
searches files, proposes edits you approve as diffs, runs commands you
approve, and checks its own work.

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
| `--context-window` | `MAX_MODEL_LEN` | Compact the transcript before it overflows |
| `--temperature` | `0.1` | Sampling temperature |
| `--top-p` | `0.8` | Nucleus sampling cutoff |
| `--max-tokens` | `8192` | Ceiling on one reply; `0` defers to the gateway |
| `--yes` | — | Approve edits and commands automatically. Unattended runs only. |
| `--allow-dirty` | — | Start with uncommitted changes already present |

Compaction is on whenever a window is known, which it is by default from
`MAX_MODEL_LEN` in the environment or `.env`. The transcript is compacted at 75%
of it and the run survives. It used to require the flag, and a flag you have to
remember is a flag that is not set — a session here reached 163k tokens against
a 131k window that way, with every turn past the limit silently truncated by the
engine. `--context-window 0` disables it deliberately.

### Why the temperature is that low

Sampling is sent explicitly on every request. Left unset, vLLM falls back to
the model's own `generation_config.json` — for Qwen3-Coder that is temperature
0.7, tuned for chat, and it is the wrong setting for this loop.

`edit_file` requires `old_string` to be reproduced byte for byte, indentation
included, and the applier refuses anything less than an exact match on purpose
(see *How edits are applied*). So sampling variance here does not produce a
slightly different edit — it produces a **failed** one, costing a re-read and a
retry. At 0.1 the model copies what `read_file` showed it far more reliably.

Not 0.0: greedy decoding leaves nothing to break a repetition loop, and a model
that starts repeating itself burns the whole 40-step budget rather than one
turn.

`--max-tokens` matters for a subtler reason. Unbounded, a long generation can
run into the context limit *mid-tool-call*, and a tool call truncated mid-JSON
is rejected rather than executed — the failure the streaming client guards
against. An explicit ceiling makes that much less likely, and stops a runaway
reply holding a KV-cache slot upstream.

Raise `--temperature` when you want the model to explore a design question;
leave it low when you want it to edit code.

## The tools it has

| | |
| --- | --- |
| `list_files` | Orient in an unfamiliar tree, optionally by glob |
| `grep` | Regex over file contents, with line numbers |
| `read_file` | Read a text file — **required before editing it** |
| `read_document` | Inspect a PDF, CSV/TSV or spreadsheet. Read-only |
| `edit_file` | Replace an exact, unique snippet. **You approve the diff** |
| `write_file` | Create a file, or replace one wholesale. **You approve the diff** |
| `git_diff` | Review its own uncommitted changes. Read-only |
| `run` | Shell command, **approved by you each time** |

Eight tools, deliberately. Every tool costs prompt tokens on every turn of the
loop and adds another way for the model to go wrong.

`git_diff` earns its place because without it the model cannot see its own work
as a whole. It could always shell out through `run`, but that costs an approval
prompt for something read-only, and the end-of-turn `diff --stat` goes to *you*,
never into the transcript. So a multi-file change was written blind, one edit at
a time. The system prompt now asks for a review pass before the final answer,
which is where a leftover import or an edit in the wrong place gets caught.

## Showing it a document to write a parser for

The model is text-only — it cannot be shown a PDF. But what it needs in order
to *write a parser* for one is not the pixels and not really the text: it is
the shape. `read_document` returns that.

```
⏺ read_document  statement.pdf
```
```
statement.pdf - PDF, 1 page

page 1: 29 words
  text columns start at x: 57 (5 rows), 150 (5 rows), 330 (4 rows),
                           399 (2 rows), 471 (5 rows)
    Date Description Debit Credit Balance
    01/09/2026 TESCO STORES 3345 42.10 1203.55
    02/09/2026 TFL TRAVEL CHARGE 8.40 1195.15
```

Thirty lines instead of several thousand tokens of extracted text — and more
useful, because dumped text loses the column geometry, which is exactly what
differs between one bank's layout and the next. Rows appear verbatim, so the
model can match against real strings rather than a paraphrase.

Two details earn their place. Ruled tables are reported as tables; a statement
laid out with whitespace has no table at all, and then the **x-positions are
the only signal** a parser has. And each position carries its occupancy: a
`Credit` column with two entries in the sample is still a column the parser
must handle, so it is listed rather than dropped as noise.

`full=true` returns the extracted text instead, for when the content matters
more than the shape. Documents are never marked as read-for-editing — a digest
is not the file's contents, and an edit built from one would be built from a
summary.

| | |
| --- | --- |
| PDF | pages, ruled tables, column positions, sample rows |
| CSV / TSV | delimiter, column names, guessed types, row count, sample rows |
| XLSX / XLSM | every sheet, its dimensions, header and sample rows |

### It needs an optional extra

```bash
pip install 'llm-assistant-api[documents]'
```

An extra rather than a dependency: this package is also the gateway, whose
image `config.py` deliberately keeps small, and `pdfplumber` brings
`pdfminer.six` and Pillow with it. Without it the tool returns an instruction
to install it, as a tool result, so the model tells you rather than failing
mid-turn. CSV and TSV need nothing.

Note this runs on the host, like `read_file` — it is how the *agent* reads the
document. Code the agent then writes to parse PDFs is your project's own
dependency, installed inside the sandbox with `make venv` as usual.

## Checking edits as they land

Every file the agent writes is checked immediately, and anything found comes
back as part of that tool's result:

```
⏺ edit_file  src/llm_assistant_api/proxy.py
  @@ -117,7 +117,7 @@
  -        for field in ("max_tokens",):
  +        for field in ("max_tokens", "max_completion_tokens"):
  ✗ src/llm_assistant_api/proxy.py:12:1: F401 [*] `json` imported but unused
```

The timing is the whole point. A diagnostic attached to the edit gets fixed in
the same turn, with the file still in front of the model. The same finding
surfacing later — from a test run, or from you at review time — costs a re-read
and several more turns, if it is noticed at all.

Two things run:

- **A Python syntax check**, always. It needs nothing installed, it is
  instant, and it catches the worst outcome: a file that no longer parses. When
  it fires, the other checkers are skipped, since they would each report the
  same thing in their own dialect.
- **`ruff`**, if it is on `PATH`. Auto-detected rather than configured, because
  a check you have to switch on is a check that stays off.

| Flag | |
| --- | --- |
| `--check 'COMMAND {path}'` | Run something else instead. Repeatable |
| `--no-check` | Turn it off |

Deliberately only fast, file-scoped checks. A whole-project `mypy` takes
seconds and needs the tree to be consistent, which mid-refactor it is not —
that belongs in `run`, where the model asks for it on purpose. A checker that
exits non-zero without saying anything is treated as not understanding the file
rather than as a problem with it, and one that hangs is dropped after 30s.

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
a silent wrong edit in a multi-file change is expensive to find.

Which makes the failure message load-bearing, and "not found" on its own is
close to useless: for a long anchor it could be a stray space on line two or a
block that was never in the file, and those need opposite corrections. So a
miss is diagnosed. Measured over real sessions, anchors that applied ran to a
median of 24 lines; the ones that failed, 139 — the model reconstructs a long
block from memory and gets most of it right:

```
✗ old_string was not found in the file. It matches the file from line 61 for
  30 lines, then differs:
    you sent:      # Validate user ID (only allow predefined users)
    the file has:  # Verify user is the default user
  Fix that line, or anchor on a shorter run of lines around the change.
  You have written this file since you last read it, so it no longer says what
  you remember. Call read_file before editing it again.
  old_string is 174 lines. edit_file needs a byte-exact copy, and that much
  text reproduced from memory almost never is one. Anchor on the few lines
  that actually change, or use write_file to replace the file whole.
```

Four separate hints, each fired by a condition: near-miss on whitespace only;
the first line that diverges, with what the file has there instead; an anchor
whose first line is nowhere in the file at all, which means it was never
copied from one; and an anchor long enough that byte-exact recall was never
realistic.

That third case is worth its own note. `write_file` marks a file *seen*, so
read-before-edit is satisfied and the model carries on editing from its
recollection of what it wrote — which is a confirmation, not the text. The
workspace tracks files written since they were last read for exactly this, so
the error can say so rather than leave it to be worked out.

The diff you see is computed from the file before and after, never from the
model's description of what it did.

## Safety

| | |
| --- | --- |
| Path escapes | Every path is resolved and must be inside the workspace |
| Edits | Every edit prompts, showing the diff it would make, before it lands |
| Undo | Git. A clean tree is required unless you pass `--allow-dirty` |
| Shell | Every command prompts, showing the exact command — and where it will run |
| Shell isolation | `--sandbox` runs commands in a container with only the workspace mounted and no network. Off by default |
| Commits | The system prompt forbids `git commit`/`push` — this repo's releases are driven by commit messages, so an agent writing them would mint bogus versions |
| Runaway loops | Hard cap of 40 steps per message, and a turn is abandoned after the same call fails 4 times running |
| A turn going nowhere | Ctrl-C cancels it and keeps the session |

## Interrupting a turn

Ctrl-C during a turn cancels **that turn**, not the session. The conversation
so far is kept, and you carry on from the next prompt:

```
⏺ run  make test
^C
! interrupted - the conversation is kept, carry on or /exit
›
```

Press it twice within two seconds to leave the session instead — the escape
hatch for a turn that will not stop. At the `›` prompt, where there is no turn
to cancel, Ctrl-C exits as usual.

Two things make this work, and both are worth knowing about if you touch this
code:

**The interrupt goes through the event loop**, not through `KeyboardInterrupt`.
A `SIGINT` arriving while the loop is parked in `epoll` raises inside
`run_forever`, not inside the coroutine, so a `try/except KeyboardInterrupt`
around the `await` never fires and the whole process goes down with the turn.
`loop.add_signal_handler` cancels the task instead. The handler is installed
only for the duration of a turn, which is what leaves Ctrl-C at the prompt
behaving normally.

**Tool calls run in a worker thread.** Without that the loop is blocked inside
`subprocess.run` for as long as the command takes, and cannot process the
cancellation at all — Ctrl-C during a five-minute test run would do nothing.
The approval prompt deliberately stays on the event loop: a prompt blocked on
stdin inside a thread cannot be cancelled, and would carry on eating keystrokes
after the turn that asked for it had gone.

A cancel can land between the assistant message requesting *N* tool calls and
the *N* results answering it. That transcript is not merely untidy — every
`tool_call_id` must have a matching result, so the next request would be
rejected and you would have kept a history you could not use. `Session` closes
the gap on the way out, with a result saying the tool never ran.

## Answering the approval prompt

Two things had to be got right before an approval could be trusted, and both
were found the same way: a session in which seven consecutive commands were
recorded as "declined by the user", `wc -l` among them.

**What was typed before the question is discarded.** A turn is mostly waiting,
and people type into the gap — a stray Enter, the first characters of the next
instruction. Those keystrokes sit in the terminal's buffer and are read by the
very next `input()`. A bare Enter reads as "no" under `[y/N]`, so an approval
nobody saw is refused and the transcript records a decision the user never
made. The buffer is flushed immediately before the question is asked, so an
answer can only be a keypress made while the question was on screen.

**No stdin is not a refusal.** On EOF the prompt used to return `False`, which
is indistinguishable from a considered "no": the model apologises, tries
something else, and every later approval refuses the same way, silently,
forever. It now raises, and the turn stops with

```
error: cannot ask for approval: stdin is closed, so nothing can be approved
```

which is recoverable, unlike a session full of refusals that have to be
reverse-engineered afterwards.

## Getting stuck

The step cap catches a model that keeps busy. It does not catch the loop that
actually happens, which is the same failing call over and over. From one real
session: 77 `edit_file` calls, ten of them sending `old_string` and
`new_string` identical, the last three the same 132-line block in a row — then
"I'm still getting the same error. Let me approach this differently", and
nothing different.

Two things were missing. The first is that the error said only what had
happened, not what it meant:

```
✗ old_string and new_string are identical, so this edit is a no-op.
```

Sending the same text twice means one of two opposite things, so the file has
to be consulted before saying anything. Over one real session there were 16 of
them, and in 14 the text was **already in the file**: the model had made the
change several turns earlier, lost track of it, and proposed it again from its
plan rather than from the file it had just read. The right thing to say there
is that the work is done:

```
✗ old_string and new_string are identical, and the file already reads exactly
  like this - so this change has already been applied. There is nothing to do
  here. Call git_diff if you are unsure what has landed, and move on.
```

The other two had neither string in the file — both fields held the wanted
result rather than the current text — and get told that instead. An earlier
version of this message advised sending "a new_string that is actually
different", which is the wrong instruction for fourteen cases out of sixteen:
it invites the model to invent a change to code that is already correct.

The second is that nothing counted. Failures are now tallied by tool name
*and* arguments — a model working steadily through several files is not
looping, one resending a byte-identical failing call is. The second such call
is told so plainly, and the fourth ends the turn:

```
! edit_file failed the same way 4 times running - stopping so you can take a look.
```

Better than burning thirty more steps and handing back a session that has to
be read to find out nothing happened.

## Knowing it is still alive

A turn is mostly silence. Prefill over a full context window is tens of
seconds before the first token, and a test suite behind `run` can be minutes —
during which an agent that is working and an agent that has hung look exactly
alike. That resemblance is what makes people reach for Ctrl-C.

So the stretches with no output carry a spinner: `thinking` while waiting on
the model, `running` while a command is out at a worker thread, `checking`
while the linters go over an edit. Past three seconds it starts counting, on
the grounds that "waiting" and "hung" are only distinguishable by a number
that keeps moving.

It draws nothing for the first 0.4s, so the tools that return immediately do
not flash a frame on the way past, and nothing at all when stdout is not a
terminal — redraw sequences belong in a terminal, not in a log file or a
captured test stream.

It also has to come back within a single turn. Qwen3-Coder narrates before it
acts — in a real session, 97 turns out of 130 carried prose *and* tool calls —
so a spinner that merely stopped at the first token would disappear exactly
before the quietest stretch of the turn: the arguments of a whole-file write,
streaming one fragment at a time with nothing printed. The client reports tool
fragments as they arrive, and the spinner resumes on a line of its own below
the prose.

Whoever is about to print stops it first, and `stop()` erases the line there
and then rather than leaving it to the animating task. Both run on the one
event-loop thread, so the task can only take control at an `await` and cannot
be caught halfway through a write. That is what lets the first streamed token
clear the spinner and take the line it was using.

## Approving edits

Every `edit_file` and `write_file` is put to you as the diff it would make,
before it lands. Naming only the file would leave you nothing to answer with
but trust in the model, and misplaced trust is the failure this exists to
catch: an edit that applies cleanly, passes the linter and is quietly wrong.

The preview is built by applying the edit in memory and diffing the result, so
what you approve is what gets written — not the model's description of it.
Three cases are never put to you, because there is nothing to decide: an edit
whose `old_string` does not match, one that changes nothing, and one against a
file the model has not read. All three fail in `invoke` with a message that
says why, and asking first would only be a question about an edit that is not
going to happen.

That last case matters more than it looks. The preview reads the file
directly rather than through `workspace.read`, which records files as *seen*.
Routing it through there would satisfy the read-before-edit rule on the
model's behalf, by way of a prompt the model never had to answer — the rule
would be discharged by the machinery that exists to enforce it.

Approving shows the diff once, not twice: the prompt is where you read it, and
reprinting it on acceptance is the kind of noise that turns approvals into
reflex. Under `--yes` nobody saw a prompt, so the diff is printed after the
edit instead.

## A session

```
$ assist "make prepare_payload apply the cap to max_completion_tokens too"
  Qwen/Qwen3-Coder-30B-A3B-Instruct via http://127.0.0.1:8081/v1
  workspace /home/ryan/llm-assistant-api
────────────────────────────────────────────────────────────────────────
⏺ grep  prepare_payload
⏺ read_file  src/llm_assistant_api/proxy.py
⏺ edit_file  src/llm_assistant_api/proxy.py

? Edit src/llm_assistant_api/proxy.py
    @@ -117,7 +117,7 @@
    -        for field in ("max_tokens",):
    +        for field in ("max_tokens", "max_completion_tokens"):
  Proceed? [y/N] y
⏺ run  make test

? Run a shell command on this machine
    make test
  Proceed? [y/N] y
  ............................ 161 passed

Applied the cap to both fields; the tests still pass.

  src/llm_assistant_api/proxy.py | 2 +-
  ctx ~12k / 131k
```

Then review it the way you review anything else — `git diff`, or the Source
Control panel.

## Sessions outlive the terminal

A transcript is the expensive part of a session — it is what the model has
learned about your repository, bought a tool call at a time — so each turn is
written to `~/.assist/sessions` (override with `ASSIST_HOME`).

```bash
assist --sessions             # what is saved for this workspace
assist --resume               # carry on from the most recent one
assist --resume 20260906-1312-a4f1
assist --no-save "..."        # leave no trace
```

Deleting is explicit, and shows you what is about to go:

```bash
assist --delete 20260906-1312-a4f1 20260906-1104-9b02
assist --delete-all           # every session for this workspace
assist --delete-all --yes     # no prompt, for scripts
```

```
  20260906-131204-a4f1  2026-09-06 14:12    6 turns  port the retry logic
? Permanently delete 1 saved session?
  Proceed? [y/N]
```

A transcript is not recoverable once deleted, and is usually the expensive part
of a session, so it is listed and confirmed first — which also catches a
mistyped id before it quietly matches nothing. Naming an id that does not exist
exits non-zero rather than reporting success for work it did not do.

`--delete-all` is scoped to the current workspace (so it honours `--cwd`).
Sessions recorded elsewhere are work you are not looking at, and deleting them
from the wrong directory would be a surprising way to lose a transcript.

Resuming does **not** restore the read-before-edit permission wholesale, and
that is the part worth understanding. `Workspace` refuses to edit a file that
has not been read in this session, which is what stops an edit built from a
hallucinated recollection of the code. Restoring that set verbatim from a
session recorded last week would launder a stale memory into a fresh
permission: the model could edit a file it has not read and whose contents have
since changed — exactly the case the rule exists to prevent.

So each file's digest is stored with it, and on resume an entry comes back only
if the file still hashes the same:

```
  resumed 20260906-1312-a4f1 - 6 turns, 41 messages
  9 files still match, 2 changed and must be re-read
! the repository has moved on since this session (was 4363bdf1, now b8be4820).
  What it remembers about the code may be out of date.
```

Anything that moved on has to be read again, which costs one cheap tool call.
The `HEAD` warning is advisory — you may well be resuming deliberately after a
rebase — but it is worth seeing before the model acts on what it thinks it
knows.

Sessions are pruned to the most recent 100.

What is saved is the **whole** conversation, not the compacted one. Compaction
trims what gets *sent* so it fits the context window; it is not a decision to
forget what happened. Saving the trimmed list instead makes every compaction a
permanent deletion, and a cumulative one — a session here went from 23 user
turns to 2 that way, one save at a time, with no way back. `Session.messages`
is what the model sees; `Session.history` is what happened, and the store gets
the latter.

## Matching the sandbox to the project

The sandbox image defaults to the same Python as the gateway, which is not
necessarily the one the project targets. A project pinning 2023-era
dependencies has no wheels built for a 2025 interpreter, so pip falls back to
building from source — and `pydantic-core` from source wants a Rust toolchain
that is deliberately not in the image:

```
ERROR: Failed building wheel for pydantic-core
```

That reads like a broken sandbox and is really a version mismatch. Build one
that matches:

```bash
make sandbox-image SANDBOX_PYTHON=3.11
```

Or point `--sandbox-image` at any image you already have. Rust is left out on
purpose — it would roughly double the image for something most projects never
need.

## Running shell commands in a container

Every other tool is contained already: paths are resolved inside the workspace
and refused outside it. `run` is the hole in that — an arbitrary shell command
with your user, your environment and your network. The approval prompt is the
only gate, and `cat .env` looks perfectly reasonable at the end of a long
session.

`--sandbox` points `run` at a container instead. The workspace is bind-mounted;
your home directory, your keys and your network are simply not in there.

```bash
make sandbox-image            # once
assist --sandbox "port the retry logic to the embeddings route"
```

```
$ assist --sandbox
  Qwen/Qwen3-Coder-30B-A3B-Instruct via http://127.0.0.1:8081/v1
  workspace /home/ryan/llm-assistant-api
  sandbox assist-993753791190 created from assist-sandbox:latest

? Run a shell command in container assist-993753791190
    make test
  Proceed? [y/N]
```

The prompt names where the command will run, because approving `rm -rf /` in a
container is a different decision from approving it on your laptop.

| Flag | |
| --- | --- |
| `--sandbox` | Run commands in the container. `ASSIST_SANDBOX=1` to default it on |
| `--no-sandbox` | Run them here, overriding `ASSIST_SANDBOX` |
| `--sandbox-network` | Give it network access. Off by default |
| `--sandbox-image` | Use a different image |
| `--sandbox-wipe` | Throw this workspace's container away and exit |

**It is off by default, and the agent runs perfectly well without it.** Not
everyone has Docker, the container cannot run a macOS build, and an agent you
cannot run is not safer than one you can. Asking for a sandbox and not getting
one *is* treated as an error, though — a missing daemon or unbuilt image stops
the session rather than quietly falling back to running on your machine.

### The container is disposable

One long-lived container per workspace, reused across sessions, because a test
run that reinstalls its dependencies every time will not get used. Which means
it accumulates: caches, virtual environments, whatever got installed at 2am.

Nothing inside it is precious — everything that matters is in the bind-mounted
workspace, on the host — so throw it away whenever it gets large or confusing:

```bash
make sandbox-status   # names, states and sizes
make sandbox-wipe     # every agent container, gone
assist --sandbox-wipe # just this workspace's
```

The next command builds a fresh one. Nothing is lost.

Two details that matter in practice. The container runs as your uid, so files
the agent creates arrive in your repository owned by you rather than by root.
And network is off unless you ask for it — which does mean `pip install` fails
in there, so `--sandbox-network` when the task genuinely needs it.

### Build artefacts are not shared with the host

The workspace is bind-mounted read-write, so anything in it is writable from
inside the container. That is the point — the agent has to be able to edit your
code. But a `.venv` is not code: it is binaries built for whichever platform
built them, and the container is deliberately a *different* platform from the
host.

Sharing it is not merely useless, it is destructive. A `python3 -m venv .venv`
inside the container rewrites the host's `pyvenv.cfg` through the bind mount:

```
 home = /usr/local/bin
 command = /usr/local/bin/python3 -m venv /workspace/.venv
```

Those are container paths. The next thing you run on the host finds its own
virtualenv pointing at a directory that does not exist and refuses to start —
`Fatal Python error: init_fs_encoding`. Nothing outside the workspace was ever
at risk; this is about the part of the *workspace* that cannot be shared across
two platforms.

So these are masked with anonymous volumes — present inside the container, but
container-local, and neither read from nor written to the host:

```
.venv  venv  node_modules  .mypy_cache  .pytest_cache  .ruff_cache  .tox
```

Only the ones your workspace actually has, so a Python project does not grow a
phantom `node_modules` that the model then reads and draws conclusions from.
They live and die with the container, and `--sandbox-wipe` takes them with it.

The practical consequence is that the container starts with no dependencies
installed, and installing them needs the network:

```bash
assist --sandbox --sandbox-network "run make venv, then make test"   # once
assist --sandbox "..."                                               # thereafter
```

The environment it builds persists in the container until you wipe it.

### `--sandbox-network` only applies when the container is created

Docker fixes a container's network mode at creation and cannot add network to
one that already exists. So if a sandbox was already built without it, passing
`--sandbox-network` later cannot take effect, and this is an error rather than
something quietly ignored:

```
error: assist-a43c67ba1ee6 was created without network access, and docker
cannot add it to an existing container. Run 'assist --sandbox-wipe' and try
again; nothing in it is worth keeping.
```

Ignoring the flag would surface as `pip` failing to resolve `pypi.org`, which
reads like a broken network rather than a container built without one — several
wasted turns away from the actual cause.

The reverse is not an error. A container that *has* network, used in a run that
did not ask for it, is reused as it is: rebuilding would throw away the
dependencies installed with that network, which is the entire reason the
workflow turns it on for one run. The startup line always reports what the
container can really reach, not what was requested:

```
  sandbox assist-a43c67ba1ee6 with network (50.4MB (virtual 744MB))
```

If a host `.venv` has already been clobbered, it is repairable rather than lost
— the packages are untouched and only `pyvenv.cfg` was rewritten. Point `home`
back at your real interpreter, delete the stray `lib/pythonX.Y` the container
left behind, and it starts again.

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
- **It is not Cline.** Cline is more capable and more polished. This exists so
  the whole system is in one repo you control.
