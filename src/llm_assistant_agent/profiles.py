"""Configuration profiles for common workflows.

Profiles let users save and reuse common flag combinations. For example:

```bash
assist --profile sandbox-dev "fix the tests"
```

Profiles are stored in:
- ``~/.assist/profiles.yaml`` - user profiles
- ``.assist/profiles.yaml`` in workspace - project-specific profiles
- Paths in ``ASSIST_PROFILES`` environment variable

Example profile::

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
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

__all__ = ["Profile", "ProfileLoader"]


@dataclass
class Profile:
    """A named configuration profile."""

    name: str
    config: dict[str, Any] = field(default_factory=dict)
    source: Path | None = None

    def apply_to_args(self, args: dict[str, Any]) -> dict[str, Any]:
        """Merge profile config into args, profile values taking precedence.

        Handles special cases:
        - ``check`` list is extended, not replaced
        - Boolean flags like ``no-check`` are converted properly
        """
        result = dict(args)

        for key, value in self.config.items():
            # Normalize key names (profile uses kebab-case, args use snake_case)
            arg_key = key.replace("-", "_")

            # Special handling for check list - extend rather than replace
            if arg_key == "check" and isinstance(value, list):
                existing = result.get("check", []) or []
                result["check"] = existing + value
            # Handle no-check as a boolean flag
            elif arg_key == "no_check":
                if value:
                    result["no_check"] = True
            else:
                result[arg_key] = value

        return result


class ProfileLoader:
    """Loads and manages configuration profiles."""

    def __init__(self) -> None:
        self.profiles: dict[str, Profile] = {}

    def discover(self, workspace_root: Path | None = None) -> dict[str, Profile]:
        """Load all available profiles.

        Searches in order (later sources override earlier):
        1. Paths from ASSIST_PROFILES env var (colon-separated)
        2. ~/.assist/profiles.yaml
        3. .assist/profiles.yaml in workspace (if workspace_root provided)
        """
        self.profiles = {}
        search_paths: list[Path] = []

        # 1. Environment variable paths
        env_paths = os.environ.get("ASSIST_PROFILES", "")
        if env_paths:
            for path_str in env_paths.split(":"):
                if path_str.strip():
                    search_paths.append(Path(path_str.strip()).expanduser())

        # 2. User profiles file
        home = Path.home()
        user_profiles = home / ".assist" / "profiles.yaml"
        if user_profiles.is_file():
            search_paths.append(user_profiles)

        # 3. Workspace profiles file
        if workspace_root:
            workspace_profiles = workspace_root / ".assist" / "profiles.yaml"
            if workspace_profiles.is_file():
                search_paths.append(workspace_profiles)

        # Load from each path
        for path in search_paths:
            self._load_from_file(path)

        return self.profiles

    def _load_from_file(self, path: Path) -> None:
        """Load profiles from a YAML file."""
        if not path.is_file():
            return

        try:
            content = path.read_text(encoding="utf-8")
            data = yaml.safe_load(content)

            if not isinstance(data, dict):
                return

            for name, config in data.items():
                if isinstance(config, dict):
                    self.profiles[name] = Profile(
                        name=name,
                        config=config,
                        source=path,
                    )

        except (OSError, yaml.YAMLError):
            # Silently skip broken profile files
            pass

    def get(self, name: str) -> Profile | None:
        """Get a profile by name."""
        return self.profiles.get(name)

    def list_names(self) -> list[str]:
        """List all available profile names."""
        return sorted(self.profiles.keys())

    def apply(self, name: str, args: dict[str, Any]) -> dict[str, Any]:
        """Apply a named profile to args.

        Returns the merged args dict. Raises KeyError if profile not found.
        """
        profile = self.get(name)
        if profile is None:
            raise KeyError(f"Profile {name!r} not found")
        return profile.apply_to_args(args)
