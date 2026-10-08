# Advanced Features

This document covers the advanced architecture-level improvements added to the agent.

## 1. Plugin System

Extend the agent with custom tools without modifying core code.

### Creating a Plugin

Create a Python file in `~/.assist/plugins/` or `.assist/plugins/` in your workspace:

```python
# ~/.assist/plugins/github.py
"""GitHub integration plugin."""

import urllib.request
import json

TOOL_SCHEMAS = [{
    "type": "function",
    "function": {
        "name": "fetch_github_issue",
        "description": "Fetch a GitHub issue by number",
        "parameters": {
            "type": "object",
            "properties": {
                "repo": {"type": "string", "description": "Repository in format owner/repo"},
                "issue_number": {"type": "integer", "description": "Issue number"},
            },
            "required": ["repo", "issue_number"],
        },
    },
}]

class PluginToolBox:
    def fetch_github_issue(self, arguments):
        repo = arguments.get("repo", "")
        issue_number = arguments.get("issue_number", 0)
        url = f"https://api.github.com/repos/{repo}/issues/{issue_number}"
        try:
            with urllib.request.urlopen(url, timeout=10) as response:
                data = json.loads(response.read().decode())
                return f"Title: {data['title']}\nState: {data['state']}\nBody: {data['body'][:500]}"
        except Exception as exc:
            return f"Error: {exc}"
```

### Loading Plugins

Plugins are automatically discovered from:
- `~/.assist/plugins/` - user plugins
- `.assist/plugins/` in workspace - project-specific plugins
- Paths in `ASSIST_PLUGINS` environment variable (colon-separated)

```bash
# Use plugins from a custom directory
export ASSIST_PLUGINS=/path/to/plugins:/another/path
assist "fetch issue 123 from owner/repo"
```

### Plugin Lifecycle

Plugins can define optional hooks:

```python
def setup():
    """Called when plugin is loaded."""
    print("GitHub plugin initialized")

def teardown():
    """Called when agent exits."""
    print("Cleaning up GitHub connections")
```

## 2. Configuration Profiles

Save and reuse common flag combinations.

### Creating Profiles

Create `~/.assist/profiles.yaml`:

```yaml
sandbox-dev:
  sandbox: true
  sandbox-network: true
  temperature: 0.1
  max-tokens: 4096
  check:
    - "ruff check {path}"
    - "ruff format {path}"

quick-edit:
  temperature: 0.0
  max-tokens: 2048
  no-check: true

remote-gpu:
  base-url: "http://gpu-server:8081/v1"
  model: "Qwen/Qwen3-Coder-30B-A3B-Instruct"
  context-window: 131072
```

### Using Profiles

```bash
# Use a profile
assist --profile sandbox-dev "fix the tests"

# Profile values can be overridden with flags
assist --profile sandbox-dev --no-sandbox "run locally"
```

### Workspace-Specific Profiles

Create `.assist/profiles.yaml` in your workspace for project-specific configurations:

```yaml
# .assist/profiles.yaml in your repo
frontend:
  sandbox: true
  check:
    - "npm test"
    - "npm run lint"

backend:
  sandbox: true
  sandbox-network: true
  check:
    - "pytest {path}"
```

## 3. Session Sharing

Export and import sessions for collaboration or backup.

### Exporting a Session

```bash
# Export current session to JSON
assist --export-session /path/to/session-export.json

# The exported file contains:
# - Full conversation history
# - File digests (for validation)
# - Model and configuration
```

### Importing a Session

```bash
# Import a session from export
assist --import-session /path/to/session-export.json --resume

# The session is loaded but not automatically saved
# Use --resume to continue from it
```

### Use Cases

- **Collaboration**: Share debugging sessions with teammates
- **Backup**: Preserve important sessions
- **Migration**: Move sessions between machines
- **Templates**: Create session templates for common tasks

## 4. Multi-Repo Support

Work across multiple related repositories simultaneously.

### Using Multi-Workspace

```python
from llm_assistant_agent.workspace import MultiWorkspace
from pathlib import Path

# Create multi-workspace
multi = MultiWorkspace.open(
    roots=[
        Path("/repo-main"),
        Path("/repo-frontend"),
        Path("/repo-backend"),
    ],
    names=["main", "frontend", "backend"],
)

# Switch active workspace
multi.set_active("frontend")
multi.active.read("package.json")

# Access by index
multi[0].read("README.md")

# List all workspaces
for name, root in multi.list_workspaces():
    print(f"{name}: {root}")
```

### Git Worktree Integration

Perfect for git worktrees:

```bash
# Create worktrees
git worktree add ../repo-feature -b feature-branch

# Use both in one session
multi = MultiWorkspace.open([
    Path("."),
    Path("../repo-feature"),
])
```

## 5. Response Caching

Cache model responses to reduce costs and enable offline operation.

### Enabling Cache

```bash
# Enable for one session
assist --cache "fix the bug"

# Enable by default
export ASSIST_CACHE=1

# Disable explicitly
assist --no-cache "..."
```

### How It Works

- Responses are cached by: model, messages, tools, sampling params
- Cache stored in `~/.assist/cache/`
- Default max age: 7 days
- Default max size: 100 MB
- Oldest entries removed when limit reached

### Cache Statistics

```bash
# View cache stats
assist --stats

# Output shows:
# - Cache entries count
# - Cache size
# - Hit rate percentage
# - Saved sessions count
```

### Offline Mode

With caching enabled, repeated queries use cached responses:

```bash
# First call - hits API
assist "explain this code"

# Second identical call - uses cache (offline capable)
assist "explain this code"
```

### Manual Cache Management

```python
from llm_assistant_agent.cache import ResponseCache

cache = ResponseCache()

# View stats
stats = cache.stats()
print(f"Hit rate: {stats.hit_rate:.1f}%")

# List recent entries
entries = cache.list_entries(limit=10)

# Clear cache
count = cache.clear()
print(f"Cleared {count} entries")
```

## 6. Combined Usage Example

```bash
# Use a profile with plugins and caching
export ASSIST_PLUGINS=~/.assist/plugins
assist --profile sandbox-dev --cache \
  "implement the feature using the github plugin"

# Export the session for sharing
assist --export-session feature-session.json

# Share with teammate, who imports it
assist --import-session feature-session.json --resume

# Check statistics
assist --stats
```

## Configuration Reference

### Environment Variables

| Variable | Description | Example |
|----------|-------------|---------|
| `ASSIST_PLUGINS` | Colon-separated plugin paths | `/path/to/plugins` |
| `ASSIST_PROFILES` | Colon-separated profile file paths | `~/.assist/profiles.yaml` |
| `ASSIST_CACHE` | Enable caching by default | `1` or `0` |
| `ASSIST_HOME` | Base directory for assist data | `~/.assist` |

### File Locations

| Location | Purpose |
|----------|---------|
| `~/.assist/plugins/` | User plugins |
| `~/.assist/profiles.yaml` | User profiles |
| `~/.assist/cache/` | Response cache |
| `~/.assist/sessions/` | Saved sessions |
| `.assist/plugins/` | Workspace plugins |
| `.assist/profiles.yaml` | Workspace profiles |

## Best Practices

### Plugins
- Keep plugins focused and small
- Handle errors gracefully
- Document tool parameters clearly
- Test plugins independently

### Profiles
- Create profiles for common workflows
- Use workspace-specific profiles for project needs
- Version control `.assist/profiles.yaml` in team projects

### Sessions
- Export important sessions before major changes
- Share sessions when reporting bugs
- Use imports to continue work across machines

### Caching
- Enable for development/testing
- Disable for production runs needing fresh responses
- Monitor hit rate to optimize workflows
- Clear cache when model is updated

### Multi-Workspace
- Use for monorepos or related projects
- Keep workspace count small (<5)
- Name workspaces clearly
- Switch contexts explicitly
