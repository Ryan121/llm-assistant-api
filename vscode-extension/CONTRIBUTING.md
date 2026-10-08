# Contributing to LLM Assistant VS Code Extension

## Development Setup

1. **Clone and Install**
   ```bash
   cd vscode-extension
   npm install
   ```

2. **Configure TypeScript**
   - The project uses strict TypeScript mode
   - Run `npm run compile` to check for errors
   - Use `npm run watch` for continuous compilation during development

3. **Set Up Debug Configuration**
   - The `.vscode/launch.json` is pre-configured for extension debugging
   - Press F5 to launch the Extension Development Host

## Code Style

### TypeScript Guidelines

- Use strict typing - avoid `any` unless absolutely necessary
- Prefer `const` over `let`, avoid `var`
- Use async/await for asynchronous code
- Export only what's needed from modules

Example:
```typescript
// ✅ Good
export async function fetchData(url: string): Promise<Response> {
  const response = await fetch(url);
  if (!response.ok) {
    throw new ApiError(`Request failed: ${response.status}`, response.status);
  }
  return response;
}

// ❌ Avoid
export async function fetchData(url: any) {
  let response = await fetch(url);
  return response;
}
```

### Error Handling

Always use the error handling utilities:

```typescript
import { ApiError, withErrorHandling, showError } from './errors';

// Wrap API calls with error handling
const result = await withErrorHandling(
  apiClient.chatCompletion(messages),
  'Chat completion',
  { retryCount: 2 }
);

// Show user-friendly errors
if (error instanceof ApiError && error.statusCode === 401) {
  await showError(error, ErrorSeverity.Error, [
    { label: 'Open Settings', action: () => configureGateway() }
  ]);
}
```

### Testing

**Unit Tests** (Jest):

```typescript
// src/myModule.test.ts
import { myFunction } from './myModule';

describe('myFunction', () => {
  it('should return expected value', () => {
    expect(myFunction('input')).toBe('expected');
  });

  it('should handle edge cases', () => {
    expect(() => myFunction(null)).toThrow('Invalid input');
  });
});
```

**Integration Tests** (VS Code):

```typescript
// test/suite/extension.test.ts
import * as assert from 'assert';
import * as vscode from 'vscode';

suite('Extension Tests', () => {
  vscode.window.showInformationMessage('Starting tests...');

  test('Extension should be present', () => {
    const extension = vscode.extensions.getExtension('llm-assistant.llm-assistant');
    assert.ok(extension, 'Extension should be installed');
  });
});
```

Run tests before submitting:
```bash
npm run unit-test
npm test
```

## Architecture Overview

```
src/
├── extension.ts          # Entry point, registers providers
├── types.ts              # TypeScript interfaces
├── config.ts             # Configuration management
├── errors.ts             # Error handling utilities
├── apiClient.ts          # HTTP client for gateway API
├── sessionManager.ts     # Session persistence
├── toolExecutor.ts       # Tool execution with approvals
└── chatViewProvider.ts   # Chat UI webview
```

### Key Components

**ApiClient**: Handles all HTTP communication with the LLM gateway. Features:
- Automatic retry on network errors
- Timeout handling (60s default)
- Streaming response support
- User-friendly error messages

**ToolExecutor**: Executes model tool calls. Features:
- Edit approval workflow (shows diff before applying)
- Command approval workflow (warns on destructive commands)
- Document parsing (CSV, Excel, PDF, JSON)
- Proper error handling and reporting

**SessionManager**: Manages chat sessions. Features:
- Persistent storage in extension state
- Automatic session title generation
- Session listing and deletion
- Maximum 50 sessions (FIFO eviction)

**ChatViewProvider**: Renders the chat UI. Features:
- Streaming response display
- Tool call visualization
- Thinking indicators
- Responsive design

## Common Tasks

### Adding a New Tool

1. Add the tool schema in `chatViewProvider.ts`:
```typescript
const tools: ToolSchema[] = [
  {
    type: 'function',
    function: {
      name: 'my_tool',
      description: 'What the tool does',
      parameters: {
        type: 'object',
        properties: {
          param1: { type: 'string', description: 'Description' },
        },
        required: ['param1'],
      },
    },
  },
];
```

2. Implement the tool in `toolExecutor.ts`:
```typescript
private async myTool(args: { param1: string }): Promise<ToolExecutionResult> {
  // Validate arguments
  if (!args.param1) {
    return { success: false, content: 'param1 is required', is_error: true };
  }

  // Execute the tool
  const result = await doSomething(args.param1);

  return {
    success: true,
    content: `Result: ${result}`,
  };
}
```

3. Add to the tool dispatcher:
```typescript
switch (name) {
  case 'my_tool':
    return this.myTool(args);
```

### Adding a New Setting

1. Add to `package.json` contributes.configuration:
```json
"llm-assistant.newSetting": {
  "type": "string",
  "default": "default-value",
  "description": "What this setting does"
}
```

2. Add to `ExtensionConfig` interface in `types.ts`:
```typescript
export interface ExtensionConfig {
  // ... existing fields
  newSetting: string;
}
```

3. Add to `getConfig()` in `config.ts`:
```typescript
return {
  // ... existing fields
  newSetting: config.get<string>('newSetting') || 'default-value',
};
```

### Debugging Tips

**Logging**: Use `console.log()` for development, but remove or guard in production:
```typescript
if (process.env.DEBUG === 'true') {
  console.log('Debug info:', data);
}
```

**Webview Debugging**:
1. Open the chat panel
2. Right-click → Inspect (opens DevTools for the webview)
3. Use Console and Network tabs to debug

**Extension Logs**:
1. View → Output → LLM Assistant Tools
2. View → Output → Extension Host

## Performance Considerations

- **Lazy Loading**: Use dynamic imports for heavy dependencies (PDF, Excel parsers)
- **Streaming**: Always stream large responses to avoid blocking the UI
- **Caching**: Cache API responses where appropriate (model list, etc.)
- **Debouncing**: Debounce user input for search/filter operations

## Security Best Practices

- **API Keys**: Never log or expose API keys
- **Command Execution**: Always require approval for shell commands
- **File Operations**: Validate file paths to prevent directory traversal
- **Input Sanitization**: Escape HTML in webview messages

## Publishing

1. Update version in `package.json`
2. Update CHANGELOG.md with changes
3. Run all tests: `npm run unit-test && npm test`
4. Package: `vsce package`
5. Publish: `vsce publish`

## Questions?

Open an issue on GitHub for:
- Bug reports
- Feature requests
- Documentation improvements
