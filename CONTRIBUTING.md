# Contributing

## Setup

```bash
make venv     # .venv with the package + dev extras
make hooks    # commit-msg hook that enforces Conventional Commits
make validate # what CI runs: ruff, mypy --strict, pytest, terraform validate
```

## Commit messages drive releases

The image version is never set by hand. `python-semantic-release` reads the
commits merged into `main` and decides the bump:

| Prefix | Bump | Example |
| --- | --- | --- |
| `fix:` `perf:` `refactor:` | patch | `fix(proxy): return 504 on upstream read timeout` |
| `feat:` | minor | `feat(api): add /v1/completions passthrough` |
| `feat!:` or a `BREAKING CHANGE:` footer | major | `feat(api)!: require API_KEYS to be set` |
| `docs:` `style:` `test:` `build:` `ci:` `chore:` | none | `docs(vscode): document Cline setup` |

Scopes in use: `api`, `proxy`, `config`, `compose`, `terraform`, `ansible`,
`ci`, `docs`, `vscode`.

On merge to `main`, CI bumps `pyproject.toml`, writes `CHANGELOG.md`, tags
`vX.Y.Z`, and publishes `ghcr.io/ryan121/llm-assistant-api` at `X.Y.Z`, `X.Y`,
`X` and `latest`.

## What the tests may depend on

`tests/` must stay hermetic — no GPU, no network, no Docker. The vLLM upstream
is faked with `httpx.MockTransport` (see `tests/conftest.py`), and settings come
from `IsolatedSettings` so a local `.env` cannot change a result. Coverage is
gated at 90%.

## Changing the deployment

- Runtime knobs belong in `.env.example` with a comment explaining the default,
  and must be surfaced through `Settings` if the gateway reads them.
- Terraform owns durable Docker resources only (network, model-cache volume).
  Containers belong to Compose.
- Ansible must keep working with a bare `ansible-core` install for localhost;
  collection-dependent tasks are allowed only on the remote-host path.
