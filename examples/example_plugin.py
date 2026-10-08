"""Example plugin showing how to create custom tools.

This plugin adds tools for:
1. Fetching web content
2. Running Python code snippets
3. Managing environment variables

Install by copying to ~/.assist/plugins/ or .assist/plugins/
"""

import os
import subprocess
import urllib.request
from typing import Any

# Tool schemas - same format as core tools
TOOL_SCHEMAS = [
    {
        "type": "function",
        "function": {
            "name": "fetch_url",
            "description": "Fetch content from a URL. Use for API calls or downloading files.",
            "parameters": {
                "type": "object",
                "properties": {
                    "url": {
                        "type": "string",
                        "description": "URL to fetch (http/https only)",
                    },
                    "max_bytes": {
                        "type": "integer",
                        "description": "Max bytes to read",
                        "default": 10000,
                    },
                },
                "required": ["url"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "run_python",
            "description": (
                "Execute a Python code snippet. Use for quick calculations "
                "or data processing."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "code": {
                        "type": "string",
                        "description": "Python code to execute",
                    },
                    "timeout": {
                        "type": "integer",
                        "description": "Timeout in seconds (default 10)",
                        "default": 10,
                    },
                },
                "required": ["code"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "get_env",
            "description": "Get environment variables. Use to check configuration.",
            "parameters": {
                "type": "object",
                "properties": {
                    "names": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": "Variable names to retrieve",
                    },
                    "prefix": {
                        "type": "string",
                        "description": "Filter variables by prefix",
                    },
                },
            },
        },
    },
]

# SECURITY NOTE: This example plugin is for demonstration only.
# In production, you should:
# - Validate all inputs strictly
# - Use proper sandboxing for code execution
# - Restrict URL schemes and domains
# - Never run untrusted code


class PluginToolBox:
    """Toolbox for example plugin."""

    def fetch_url(self, arguments: dict[str, Any]) -> str:
        """Fetch content from a URL."""
        url = arguments.get("url", "")
        max_bytes = arguments.get("max_bytes", 10000)

        if not url.startswith(("http://", "https://")):
            return "Error: URL must start with http:// or https://"

        try:
            with urllib.request.urlopen(url, timeout=30) as response:
                content = response.read(max_bytes)
                return content.decode("utf-8", errors="replace")[:max_bytes]
        except TimeoutError:
            return f"Error: Timeout fetching {url}"
        except Exception as exc:
            return f"Error fetching {url}: {exc}"

    def run_python(self, arguments: dict[str, Any]) -> str:
        """Execute Python code and return output."""
        code = arguments.get("code", "")
        timeout = arguments.get("timeout", 10)

        if not code:
            return "Error: No code provided"

        # Safety: limit to simple expressions and print statements
        # In production, you'd want better sandboxing
        try:
            result = subprocess.run(
                ["python3", "-c", code],
                capture_output=True,
                text=True,
                timeout=timeout,
                check=False,
            )
            output = result.stdout + result.stderr
            if result.returncode != 0:
                return f"Exit code {result.returncode}\n{output}"
            return output or "(no output)"
        except subprocess.TimeoutExpired:
            return f"Error: Code execution timed out after {timeout}s"
        except Exception as exc:
            return f"Error executing code: {exc}"

    def get_env(self, arguments: dict[str, Any]) -> str:
        """Get environment variables."""
        names = arguments.get("names", [])
        prefix = arguments.get("prefix", "")

        results = []

        # Get specific variables
        if names:
            for name in names:
                value = os.environ.get(name, "(not set)")
                results.append(f"{name}={value}")

        # Or filter by prefix
        if prefix:
            for name, value in sorted(os.environ.items()):
                if name.startswith(prefix):
                    results.append(f"{name}={value[:100]}...")

        if not results:
            return "No variables found"

        return "\n".join(results)


def setup() -> None:
    """Called when plugin is loaded."""
    print("Example plugin loaded - fetch_url, run_python, get_env tools available")


def teardown() -> None:
    """Called when agent exits."""
    pass
