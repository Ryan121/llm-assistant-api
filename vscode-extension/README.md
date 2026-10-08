# LLM Assistant Agent for VS Code

An agentic coding assistant powered by your self-hosted LLM gateway. This extension brings the power of the `assist` CLI directly into VS Code with a chat interface, edit approval workflows, and command execution.

## Features

- **Chat Interface**: Natural language conversation with your AI coding assistant
- **Tool Integration**: Full support for file operations, search, and command execution
- **Edit Approval**: Review diffs before applying changes to your code
- **Command Approval**: Authorize shell commands before execution
- **Session Management**: Persistent chat sessions across VS Code restarts
- **Streaming Responses**: Real-time response streaming for faster feedback

## Requirements

- VS Code 1.85.0 or later
- A running LLM gateway (see [llm-assistant-agent](https://github.com/your-org/llm-assistant-agent))

## Getting Started

### 1. Configure the Gateway Connection

Run the command palette command: **LLM Assistant: Configure Gateway Connection**

Or manually set these in VS Code settings:

| Setting | Default | Description |
|---------|---------|-------------|
| `llm-assistant.baseUrl` | `http://127.0.0.1:8081/v1` | Gateway API URL |
| `llm-assistant.apiKey` | (required) | API key from your `.env` |
| `llm-assistant.model` | (auto-detect) | Model ID to use |
| `llm-assistant.contextWindow` | `131072` | Maximum context tokens |

### 2. Get Your Configuration Values

On your GPU host, run:

```bash
make vscode-config
```

This prints the base URL, model ID, and API key to use.

### 3. Start Chatting

Click the LLM Assistant icon in the activity bar to open the chat panel. Ask questions, request code changes, or run commands.

## Usage

### Chat

Type your request in the chat input. The assistant can:

- Answer questions about your code
- Make file edits (with approval)
- Run shell commands (with approval)
- Search and read files

### Edit Approval

When the assistant proposes a file edit:

1. A diff view opens showing the proposed changes
2. Click **Accept** to apply the edit
3. Click **Reject** to decline

You can enable auto-approval in settings for trusted workflows.

### Command Execution

When the assistant needs to run a command:

1. A dialog shows the command to be executed
2. Click **Allow** to run it
3. Click **Deny** to skip

Destructive git commands (checkout, reset, clean) are flagged with warnings.

### Sessions

- **New Chat**: Click the refresh icon to start a new session
- **View Sessions**: Run **LLM Assistant: Show Saved Sessions**
- Sessions persist across VS Code restarts

## Available Tools

The assistant has access to these tools:

| Tool | Description |
|------|-------------|
| `list_files` | List files with optional glob pattern |
| `grep` | Search file contents with regex |
| `read_file` | Read file contents (whole or line range) |
| `edit_file` | Replace text in a file |
| `write_file` | Create or overwrite a file |
| `run` | Execute a shell command |

## Settings

```json
{
  "llm-assistant.baseUrl": "http://127.0.0.1:8081/v1",
  "llm-assistant.apiKey": "sk-local-...",
  "llm-assistant.model": "Qwen/Qwen3-Coder-30B-A3B-Instruct",
  "llm-assistant.contextWindow": 131072,
  "llm-assistant.autoApproveEdits": false,
  "llm-assistant.autoApproveCommands": false
}
```

## Development

### Build

```bash
cd vscode-extension
npm install
npm run compile
```

### Debug

1. Open the `vscode-extension` folder in VS Code
2. Press F5 to launch the Extension Development Host
3. The extension will be active in the new window

### Testing

**Unit Tests** (Jest):

```bash
npm run unit-test
npm run unit-test -- --coverage  # With coverage
```

**Integration Tests** (VS Code):

```bash
npm test
```

**Linting**:

```bash
npm run lint
```

### Package

```bash
npm install -g @vscode/vsce
vsce package
```

This creates a `.vsix` file that can be installed manually or published to the marketplace.

## Architecture

```
┌─────────────────────────────────────────┐
│  VS Code Extension                      │
│  ┌─────────────────────────────────┐    │
│  │  Chat View (Webview)            │    │
│  │  - Message list                 │    │
│  │  - Input area                   │    │
│  │  - Tool call visualization      │    │
│  └─────────────────────────────────┘    │
│                                         │
│  ┌─────────────────────────────────┐    │
│  │  Session Manager                │    │
│  │  - Persistent storage           │    │
│  │  - Session lifecycle            │    │
│  └─────────────────────────────────┘    │
│                                         │
│  ┌─────────────────────────────────┐    │
│  │  Tool Executor                  │    │
│  │  - Edit approval workflow       │    │
│  │  - Command approval workflow    │    │
│  │  - File operations              │    │
│  └─────────────────────────────────┘    │
│                                         │
│  ┌─────────────────────────────────┐    │
│  │  API Client                     │    │
│  │  - OpenAI-compatible API        │    │
│  │  - Streaming support            │    │
│  └─────────────────────────────────┘    │
└─────────────────────────────────────────┘
                    │
                    │ HTTP
                    ▼
┌─────────────────────────────────────────┐
│  LLM Gateway (FastAPI)                  │
│  - OpenAI-compatible API                │
│  - Tool call parsing                    │
│  - Rate limiting                        │
└─────────────────────────────────────────┘
```

## Troubleshooting

**"Gateway not configured" warning**

Run **LLM Assistant: Configure Gateway Connection** and set your base URL and API key.

**Connection refused**

Ensure the gateway is running:

```bash
make health
```

If connecting to a remote gateway, verify your SSH tunnel is active.

**401 Unauthorized**

Check that your API key matches the first entry in `API_KEYS` from your `.env`. Restart the gateway after changing keys:

```bash
make restart-api
```

**Tool calls not working**

Verify the gateway is configured with the correct tool call parser for your model:

```bash
make smoke
```

## License

MIT
