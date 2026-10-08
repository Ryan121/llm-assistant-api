/**
 * Chat view provider - the main UI for the LLM Assistant
 */

import * as vscode from 'vscode';
import { SessionManager } from './sessionManager';
import { ToolExecutor } from './toolExecutor';
import { ApiClient } from './apiClient';
import { ChatMessage, ToolCall, ToolSchema } from './types';
import { getConfig, isConfigured } from './config';

/**
 * Provides the chat webview in the sidebar
 */
export class ChatViewProvider implements vscode.WebviewViewProvider, vscode.Disposable {
  private view?: vscode.WebviewView;
  private sessionManager: SessionManager;
  private toolExecutor: ToolExecutor;
  private apiClient: ApiClient;
  private disposables: vscode.Disposable[] = [];

  constructor(
    private readonly extensionUri: vscode.Uri,
    sessionManager: SessionManager,
    toolExecutor: ToolExecutor
  ) {
    this.sessionManager = sessionManager;
    this.toolExecutor = toolExecutor;
    this.apiClient = new ApiClient();
  }

  /**
   * Called when the webview should be created
   */
  resolveWebviewView(
    webviewView: vscode.WebviewView,
    context: vscode.WebviewViewResolveContext,
    token: vscode.CancellationToken
  ): void {
    this.view = webviewView;

    webviewView.webview.options = {
      enableScripts: true,
      localResourceRoots: [this.extensionUri],
    };

    webviewView.webview.html = this.getHtmlContent(webviewView.webview);

    // Handle messages from the webview
    webviewView.webview.onDidReceiveMessage(
      async (message) => {
        await this.handleMessage(message);
      },
      undefined,
      this.disposables
    );

    // Send initial state
    this.sendState();
  }

  /**
   * Handle messages from the webview
   */
  private async handleMessage(message: any): Promise<void> {
    if (!this.view) return;

    switch (message.type) {
      case 'sendMessage':
        await this.handleUserMessage(message.content);
        break;

      case 'toolApproval':
        await this.handleToolApproval(message.toolCallId, message.approved);
        break;

      case 'newSession':
        this.startNewSession();
        break;

      case 'getConfig':
        this.sendState();
        break;
    }
  }

  /**
   * Handle a user message
   */
  private async handleUserMessage(content: string): Promise<void> {
    if (!this.view || !content.trim()) return;

    const config = getConfig();

    // Check if configured
    if (!isConfigured(config)) {
      this.view.webview.postMessage({
        type: 'error',
        content: 'Please configure the gateway connection first. Run "LLM Assistant: Configure Gateway Connection"',
      });
      return;
    }

    const session = this.sessionManager.getCurrentSession();

    // Add user message to session
    const userMessage: ChatMessage = { role: 'user', content };
    this.sessionManager.addMessage(session.id, userMessage);

    // Show thinking indicator
    this.view.webview.postMessage({ type: 'thinking', started: true });

    try {
      // Get tool schemas
      const tools: ToolSchema[] = [
        {
          type: 'function',
          function: {
            name: 'list_files',
            description: "List files in the workspace, optionally filtered by a glob such as 'src/**/*.py'",
            parameters: {
              type: 'object',
              properties: {
                pattern: { type: 'string', description: 'Glob pattern relative to the workspace root' },
              },
            },
          },
        },
        {
          type: 'function',
          function: {
            name: 'grep',
            description: 'Search file contents with a regular expression',
            parameters: {
              type: 'object',
              properties: {
                pattern: { type: 'string', description: 'Python regular expression' },
                glob: { type: 'string', description: 'Restrict the search, e.g. *.py' },
              },
              required: ['pattern'],
            },
          },
        },
        {
          type: 'function',
          function: {
            name: 'read_file',
            description: 'Read a UTF-8 text file, whole or a range of lines',
            parameters: {
              type: 'object',
              properties: {
                path: { type: 'string', description: 'Path relative to the workspace' },
                begin: { type: 'integer', description: 'First line to read (1-indexed)' },
                end: { type: 'integer', description: 'Last line to read, inclusive' },
              },
              required: ['path'],
            },
          },
        },
        {
          type: 'function',
          function: {
            name: 'edit_file',
            description: 'Replace an exact snippet in a file',
            parameters: {
              type: 'object',
              properties: {
                path: { type: 'string' },
                old_string: { type: 'string', description: 'Exact text to replace' },
                new_string: { type: 'string', description: 'Replacement text' },
                replace_all: { type: 'boolean', description: 'Replace every occurrence' },
              },
              required: ['path', 'old_string', 'new_string'],
            },
          },
        },
        {
          type: 'function',
          function: {
            name: 'write_file',
            description: 'Create a new file or overwrite one completely',
            parameters: {
              type: 'object',
              properties: {
                path: { type: 'string' },
                content: { type: 'string' },
              },
              required: ['path', 'content'],
            },
          },
        },
        {
          type: 'function',
          function: {
            name: 'run',
            description: 'Run a shell command in the workspace',
            parameters: {
              type: 'object',
              properties: {
                command: { type: 'string' },
                timeout_seconds: { type: 'integer', description: 'Default 120' },
              },
              required: ['command'],
            },
          },
        },
      ];

      // Call the API
      const response = await this.apiClient.chatCompletion(
        [...session.messages, userMessage],
        tools,
        (chunk) => {
          this.view?.webview.postMessage({ type: 'streamChunk', content: chunk });
        }
      );

      // Handle tool calls
      if (response.tool_calls && response.tool_calls.length > 0) {
        // Add assistant message with tool calls
        const assistantMessage: ChatMessage = {
          role: 'assistant',
          content: response.content || null,
          tool_calls: response.tool_calls,
        };
        this.sessionManager.addMessage(session.id, assistantMessage);

        // Execute each tool call
        for (const toolCall of response.tool_calls) {
          await this.executeToolCall(toolCall);
        }

        // Continue the conversation with tool results
        await this.continueWithToolResults(session.id, response.tool_calls);
      } else {
        // No tool calls, just add the response
        const assistantMessage: ChatMessage = {
          role: 'assistant',
          content: response.content,
        };
        this.sessionManager.addMessage(session.id, assistantMessage);

        this.view.webview.postMessage({
          type: 'messageComplete',
          message: assistantMessage,
        });
      }
    } catch (error) {
      const errorMessage = error instanceof Error ? error.message : String(error);
      this.view.webview.postMessage({
        type: 'error',
        content: errorMessage,
      });
    } finally {
      this.view.webview.postMessage({ type: 'thinking', started: false });
    }
  }

  /**
   * Execute a single tool call
   */
  private async executeToolCall(toolCall: ToolCall): Promise<void> {
    if (!this.view) return;

    const { name, arguments: argsStr } = toolCall.function;
    let args: Record<string, any>;

    try {
      args = JSON.parse(argsStr);
    } catch (e) {
      args = {};
    }

    // Show tool execution in UI
    this.view.webview.postMessage({
      type: 'toolStart',
      toolCallId: toolCall.id,
      name,
      args,
    });

    // Execute the tool
    const result = await this.toolExecutor.executeTool(name, args);

    // Show tool result
    this.view.webview.postMessage({
      type: 'toolEnd',
      toolCallId: toolCall.id,
      name,
      result: result.content,
      success: result.success,
      diff: result.diff,
    });

    return result;
  }

  /**
   * Continue conversation with tool results
   */
  private async continueWithToolResults(
    sessionId: string,
    toolCalls: ToolCall[]
  ): Promise<void> {
    if (!this.view) return;

    // Execute all tool calls and collect results
    const toolResults: ChatMessage[] = [];

    for (const toolCall of toolCalls) {
      const { name, arguments: argsStr } = toolCall.function;
      let args: Record<string, any>;

      try {
        args = JSON.parse(argsStr);
      } catch (e) {
        args = {};
      }

      const result = await this.toolExecutor.executeTool(name, args);

      toolResults.push({
        role: 'tool',
        tool_call_id: toolCall.id,
        name,
        content: result.content,
      });
    }

    // Add tool results to session
    for (const result of toolResults) {
      this.sessionManager.addMessage(sessionId, result);
    }

    // Show thinking indicator
    this.view.webview.postMessage({ type: 'thinking', started: true });

    try {
      // Get the updated session
      const session = this.sessionManager.getSession(sessionId);
      if (!session) return;

      // Call API with tool results
      const response = await this.apiClient.chatCompletion(session.messages);

      // Add final response
      const assistantMessage: ChatMessage = {
        role: 'assistant',
        content: response.content,
      };
      this.sessionManager.addMessage(sessionId, assistantMessage);

      this.view.webview.postMessage({
        type: 'messageComplete',
        message: assistantMessage,
      });
    } catch (error) {
      const errorMessage = error instanceof Error ? error.message : String(error);
      this.view.webview.postMessage({
        type: 'error',
        content: `Error continuing conversation: ${errorMessage}`,
      });
    } finally {
      this.view.webview.postMessage({ type: 'thinking', started: false });
    }
  }

  /**
   * Start a new chat session
   */
  startNewSession(): void {
    this.sessionManager.createSession();
    this.sendState();

    if (this.view) {
      this.view.webview.postMessage({ type: 'newSession' });
    }
  }

  /**
   * Send current state to the webview
   */
  private sendState(): void {
    if (!this.view) return;

    const session = this.sessionManager.getCurrentSession();
    const config = getConfig();

    this.view.webview.postMessage({
      type: 'state',
      session: {
        id: session.id,
        title: session.title,
        messageCount: session.messages.length,
      },
      config: {
        baseUrl: config.baseUrl,
        model: config.model,
        isConfigured: isConfigured(config),
      },
    });
  }

  /**
   * Get the HTML content for the webview
   */
  private getHtmlContent(webview: vscode.Webview): string {
    return `<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="UTF-8">
  <meta name="viewport" content="width=device-width, initial-scale=1.0">
  <title>LLM Assistant Chat</title>
  <style>
    :root {
      --vscode-font-family: var(--vscode-editor-font-family, system-ui);
      --vscode-font-size: var(--vscode-editor-font-size, 13px);
      --vscode-foreground: var(--vscode-foreground, #cccccc);
      --vscode-background: var(--vscode-sideBar-background, #1e1e1e);
      --vscode-input-background: var(--vscode-input-background, #2d2d2d);
      --vscode-input-foreground: var(--vscode-input-foreground, #cccccc);
      --vscode-button-background: var(--vscode-button-background, #0e639c);
      --vscode-button-foreground: var(--vscode-button-foreground, #ffffff);
      --vscode-error-foreground: var(--vscode-errorForeground, #f48771);
      --vscode-success-foreground: var(--vscode-terminal-ansiGreen, #89d185);
    }

    * {
      box-sizing: border-box;
      margin: 0;
      padding: 0;
    }

    body {
      font-family: var(--vscode-font-family);
      font-size: var(--vscode-font-size);
      color: var(--vscode-foreground);
      background: var(--vscode-background);
      height: 100vh;
      display: flex;
      flex-direction: column;
      overflow: hidden;
    }

    .header {
      padding: 8px 12px;
      border-bottom: 1px solid var(--vscode-widget-border, #333);
      display: flex;
      justify-content: space-between;
      align-items: center;
    }

    .header h2 {
      font-size: 14px;
      font-weight: 600;
    }

    .header-actions button {
      background: none;
      border: none;
      color: var(--vscode-foreground);
      cursor: pointer;
      padding: 4px 8px;
      font-size: 12px;
    }

    .header-actions button:hover {
      background: var(--vscode-button-background);
      color: var(--vscode-button-foreground);
    }

    .messages {
      flex: 1;
      overflow-y: auto;
      padding: 12px;
    }

    .message {
      margin-bottom: 16px;
      padding: 8px 12px;
      border-radius: 6px;
      max-width: 90%;
    }

    .message.user {
      background: var(--vscode-button-background);
      color: var(--vscode-button-foreground);
      margin-left: auto;
    }

    .message.assistant {
      background: var(--vscode-input-background);
    }

    .message.tool {
      background: var(--vscode-editor-inactiveSelectionBackground, #2a2d2e);
      font-size: 12px;
      font-family: var(--vscode-editor-font-family, monospace);
    }

    .message.error {
      background: var(--vscode-inputValidation-errorBackground, #5a1d1d);
      color: var(--vscode-error-foreground);
    }

    .message-content {
      white-space: pre-wrap;
      word-wrap: break-word;
    }

    .tool-call {
      margin: 8px 0;
      padding: 8px;
      background: var(--vscode-editor-background, #1e1e1e);
      border-radius: 4px;
      border-left: 3px solid var(--vscode-button-background);
    }

    .tool-call-header {
      font-weight: 600;
      margin-bottom: 4px;
      display: flex;
      justify-content: space-between;
      align-items: center;
    }

    .tool-call-status {
      font-size: 11px;
      padding: 2px 6px;
      border-radius: 3px;
    }

    .tool-call-status.pending {
      background: var(--vscode-editor-inactiveSelectionBackground);
    }

    .tool-call-status.success {
      background: var(--vscode-success-foreground);
      color: #000;
    }

    .tool-call-status.error {
      background: var(--vscode-error-foreground);
      color: #000;
    }

    .tool-args, .tool-result {
      font-size: 11px;
      color: var(--vscode-descriptionForeground, #858585);
      margin-top: 4px;
      white-space: pre-wrap;
      word-wrap: break-word;
    }

    .input-area {
      padding: 12px;
      border-top: 1px solid var(--vscode-widget-border, #333);
    }

    .input-wrapper {
      display: flex;
      gap: 8px;
    }

    textarea {
      flex: 1;
      resize: none;
      border: 1px solid var(--vscode-widget-border, #333);
      border-radius: 4px;
      padding: 8px 12px;
      font-family: var(--vscode-font-family);
      font-size: var(--vscode-font-size);
      background: var(--vscode-input-background);
      color: var(--vscode-input-foreground);
      min-height: 60px;
      max-height: 200px;
    }

    textarea:focus {
      outline: 1px solid var(--vscode-focusBorder, #007fd4);
    }

    button.send-button {
      background: var(--vscode-button-background);
      color: var(--vscode-button-foreground);
      border: none;
      border-radius: 4px;
      padding: 0 16px;
      cursor: pointer;
      font-weight: 600;
    }

    button.send-button:hover {
      opacity: 0.9;
    }

    button.send-button:disabled {
      opacity: 0.5;
      cursor: not-allowed;
    }

    .thinking {
      display: flex;
      align-items: center;
      gap: 8px;
      padding: 8px 12px;
      color: var(--vscode-descriptionForeground, #858585);
    }

    .thinking-spinner {
      width: 12px;
      height: 12px;
      border: 2px solid var(--vscode-descriptionForeground);
      border-top-color: transparent;
      border-radius: 50%;
      animation: spin 1s linear infinite;
    }

    @keyframes spin {
      to { transform: rotate(360deg); }
    }

    .config-warning {
      background: var(--vscode-inputValidation-errorBackground, #5a1d1d);
      color: var(--vscode-error-foreground);
      padding: 12px;
      margin: 12px;
      border-radius: 4px;
      font-size: 12px;
    }

    .config-warning a {
      color: var(--vscode-textLink-foreground, #3794ff);
      cursor: pointer;
    }

    .empty-state {
      text-align: center;
      padding: 40px 20px;
      color: var(--vscode-descriptionForeground, #858585);
    }

    .empty-state h3 {
      margin-bottom: 8px;
    }

    .empty-state p {
      font-size: 12px;
      line-height: 1.5;
    }
  </style>
</head>
<body>
  <div class="header">
    <h2>LLM Assistant</h2>
    <div class="header-actions">
      <button id="newChatBtn" title="New Chat">⟳</button>
    </div>
  </div>

  <div id="messages" class="messages">
    <div class="empty-state">
      <h3>Welcome to LLM Assistant</h3>
      <p>Start a conversation with your AI coding assistant.</p>
      <p style="margin-top: 8px; font-size: 11px;">
        Configure the gateway connection in settings first.
      </p>
    </div>
  </div>

  <div id="thinking" class="thinking" style="display: none;">
    <div class="thinking-spinner"></div>
    <span>Thinking...</span>
  </div>

  <div class="input-area">
    <div class="input-wrapper">
      <textarea
        id="messageInput"
        placeholder="Ask me to help with code..."
        rows="3"
      ></textarea>
      <button id="sendBtn" class="send-button">Send</button>
    </div>
  </div>

  <script>
    const vscode = acquireVsCodeApi();

    const messagesEl = document.getElementById('messages');
    const messageInput = document.getElementById('messageInput');
    const sendBtn = document.getElementById('sendBtn');
    const thinkingEl = document.getElementById('thinking');
    const newChatBtn = document.getElementById('newChatBtn');

    let currentMessageEl = null;

    // Send message
    function sendMessage() {
      const content = messageInput.value.trim();
      if (!content) return;

      vscode.postMessage({ type: 'sendMessage', content });
      messageInput.value = '';
      messageInput.style.height = 'auto';
    }

    sendBtn.addEventListener('click', sendMessage);

    messageInput.addEventListener('keydown', (e) => {
      if (e.key === 'Enter' && !e.shiftKey) {
        e.preventDefault();
        sendMessage();
      }
    });

    // Auto-resize textarea
    messageInput.addEventListener('input', () => {
      messageInput.style.height = 'auto';
      messageInput.style.height = Math.min(messageInput.scrollHeight, 200) + 'px';
    });

    // New chat
    newChatBtn.addEventListener('click', () => {
      vscode.postMessage({ type: 'newSession' });
    });

    // Handle messages from extension
    window.addEventListener('message', (event) => {
      const message = event.data;

      switch (message.type) {
        case 'state':
          updateState(message.session, message.config);
          break;

        case 'newSession':
          messagesEl.innerHTML = '';
          currentMessageEl = null;
          break;

        case 'thinking':
          thinkingEl.style.display = message.started ? 'flex' : 'none';
          break;

        case 'streamChunk':
          if (!currentMessageEl) {
            currentMessageEl = createMessageEl('assistant');
          }
          currentMessageEl.querySelector('.message-content').textContent += message.content;
          scrollToBottom();
          break;

        case 'messageComplete':
          if (!currentMessageEl) {
            currentMessageEl = createMessageEl('assistant', message.message.content);
          }
          currentMessageEl = null;
          break;

        case 'toolStart':
          addToolCallEl(message.toolCallId, message.name, message.args);
          break;

        case 'toolEnd':
          updateToolCallEl(message.toolCallId, message.result, message.success, message.diff);
          break;

        case 'error':
          createMessageEl('error', message.content);
          break;
      }
    });

    function updateState(session, config) {
      if (!config.isConfigured) {
        messagesEl.innerHTML = \`
          <div class="config-warning">
            <strong>Gateway not configured</strong><br>
            Please run "LLM Assistant: Configure Gateway Connection" to set up the connection.
          </div>
        \`;
      } else if (session.messageCount === 0) {
        messagesEl.innerHTML = \`
          <div class="empty-state">
            <h3>New Chat Session</h3>
            <p>Model: \${config.model || 'Auto-detected'}</p>
            <p style="margin-top: 8px;">Ask me to help with code, explain concepts, or review changes.</p>
          </div>
        \`;
      }
    }

    function createMessageEl(role, content = '') {
      const el = document.createElement('div');
      el.className = \`message \${role}\`;
      el.innerHTML = \`<div class="message-content">\${escapeHtml(content)}</div>\`;
      messagesEl.appendChild(el);
      scrollToBottom();
      return el;
    }

    function addToolCallEl(toolCallId, name, args) {
      const el = document.createElement('div');
      el.className = 'message tool';
      el.id = \`tool-\${toolCallId}\`;
      el.innerHTML = \`
        <div class="tool-call">
          <div class="tool-call-header">
            <span>🔧 \${name}</span>
            <span class="tool-call-status pending">Running...</span>
          </div>
          <div class="tool-args">\${escapeHtml(JSON.stringify(args, null, 2))}</div>
        </div>
      \`;
      messagesEl.appendChild(el);
      scrollToBottom();
    }

    function updateToolCallEl(toolCallId, result, success, diff) {
      const el = document.getElementById(\`tool-\${toolCallId}\`);
      if (!el) return;

      const statusEl = el.querySelector('.tool-call-status');
      statusEl.textContent = success ? 'Success' : 'Failed';
      statusEl.className = \`tool-call-status \${success ? 'success' : 'error'}\`;

      const resultEl = document.createElement('div');
      resultEl.className = 'tool-result';
      resultEl.textContent = result.length > 500 ? result.slice(0, 500) + '...' : result;
      el.querySelector('.tool-call').appendChild(resultEl);

      scrollToBottom();
    }

    function escapeHtml(text) {
      const div = document.createElement('div');
      div.textContent = text;
      return div.innerHTML;
    }

    function scrollToBottom() {
      messagesEl.scrollTop = messagesEl.scrollHeight;
    }
  </script>
</body>
</html>`;
  }

  dispose(): void {
    this.disposables.forEach(d => d.dispose());
    this.disposables = [];
  }
}
