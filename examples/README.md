# Example Plugins

This directory contains example plugins demonstrating the plugin system.

## ⚠️ Security Warning

These examples are for **demonstration purposes only**. They show what's possible but should not be used in production without proper security hardening:

- **Validate all inputs** strictly
- **Sandbox code execution** properly (use containers, seccomp, etc.)
- **Restrict network access** to trusted domains
- **Never run untrusted code** without isolation

## Available Examples

### `example_plugin.py`

Demonstrates three common plugin patterns:

1. **fetch_url** - HTTP client tool
   - Shows network access from plugins
   - Demonstrates parameter validation
   - Error handling patterns

2. **run_python** - Code execution tool
   - Shows subprocess usage
   - Demonstrates timeout handling
   - **Requires sandboxing in production**

3. **get_env** - Environment inspection
   - Shows reading system state
   - Demonstrates array and optional parameters
   - Safe, read-only operation

## Installing Examples

```bash
# Copy to user plugins directory
cp examples/example_plugin.py ~/.assist/plugins/

# Or to workspace plugins directory
cp examples/example_plugin.py .assist/plugins/

# Verify plugin loaded
assist --stats
```

## Creating Your Own Plugin

1. Create a Python file in `~/.assist/plugins/` or `.assist/plugins/`
2. Define `TOOL_SCHEMAS` list
3. Implement `PluginToolBox` class with methods matching tool names
4. (Optional) Add `setup()` and `teardown()` hooks

See `docs/ADVANCED_FEATURES.md` for complete documentation.

## Best Practices

### Do
- Keep plugins focused and small
- Handle errors gracefully
- Document parameters clearly
- Test independently
- Use type hints

### Don't
- Access files outside workspace
- Run untrusted code without sandboxing
- Make network calls without validation
- Store sensitive data in plugins
- Block the event loop

## Example: Safe File Tool

```python
TOOL_SCHEMAS = [{
    "type": "function",
    "function": {
        "name": "count_lines",
        "description": "Count lines in a workspace file",
        "parameters": {
            "type": "object",
            "properties": {
                "path": {"type": "string"},
            },
            "required": ["path"],
        },
    },
}]

class PluginToolBox:
    def count_lines(self, arguments):
        from pathlib import Path
        path = Path(arguments.get("path", ""))
        if not path.is_file():
            return "File not found"
        try:
            lines = path.read_text().splitlines()
            return f"{len(lines)} lines"
        except Exception as exc:
            return f"Error: {exc}"
```

## Testing Plugins

Test your plugin before using it:

```python
# test_plugin.py
from example_plugin import PluginToolBox

toolbox = PluginToolBox()

# Test fetch_url
result = toolbox.fetch_url({"url": "https://example.com", "max_bytes": 100})
print(result)

# Test run_python
result = toolbox.run_python({"code": "print(2+2)", "timeout": 5})
print(result)

# Test get_env
result = toolbox.get_env({"names": ["HOME", "USER"]})
print(result)
```

## Debugging

Enable plugin debugging:

```bash
# Watch for plugin load messages
assist "test" 2>&1 | grep -i plugin

# Check plugin directory
ls -la ~/.assist/plugins/

# Validate plugin syntax
python -m py_compile ~/.assist/plugins/your_plugin.py
```

## Sharing Plugins

Share plugins with your team:

1. Create a git repository for plugins
2. Document installation instructions
3. Include test cases
4. Version your plugins
5. Provide security guidelines

Example structure:
```
my-plugins/
├── README.md
├── github.py
├── docker.py
├── database.py
└── tests/
    ├── test_github.py
    └── test_docker.py
```

## Next Steps

1. Study the example plugin code
2. Create a simple plugin for your workflow
3. Test it thoroughly
4. Share with your team
5. Contribute to a plugin repository

For complete documentation, see `docs/ADVANCED_FEATURES.md`.
