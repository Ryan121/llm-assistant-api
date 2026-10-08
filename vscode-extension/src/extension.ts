/**
 * LLM Assistant Agent - VS Code Extension
 *
 * Agentic coding assistant powered by your self-hosted LLM gateway.
 * Provides chat, file editing with approval workflow, and command execution.
 */

import * as vscode from 'vscode';
import { ChatViewProvider } from './chatViewProvider';
import { SessionManager } from './sessionManager';
import { ToolExecutor } from './toolExecutor';

let sessionManager: SessionManager;
let toolExecutor: ToolExecutor;

export function activate(context: vscode.ExtensionContext) {
  console.log('LLM Assistant Agent is now active');

  // Initialize managers
  sessionManager = new SessionManager(context);
  toolExecutor = new ToolExecutor(context);

  // Register the chat view provider
  const chatProvider = new ChatViewProvider(
    context.extensionUri,
    sessionManager,
    toolExecutor
  );

  context.subscriptions.push(
    vscode.window.registerWebviewViewProvider('llm-assistant.chat', chatProvider)
  );

  // Register commands
  context.subscriptions.push(
    vscode.commands.registerCommand('llm-assistant.newChat', () => {
      chatProvider.startNewSession();
    })
  );

  context.subscriptions.push(
    vscode.commands.registerCommand('llm-assistant.configure', () => {
      vscode.commands.executeCommand('workbench.action.openSettings', 'llm-assistant');
    })
  );

  context.subscriptions.push(
    vscode.commands.registerCommand('llm-assistant.showSessions', () => {
      sessionManager.showSessions();
    })
  );

  // Register disposables
  context.subscriptions.push(chatProvider, sessionManager, toolExecutor);
}

export function deactivate() {
  console.log('LLM Assistant Agent is now deactivated');
  if (sessionManager) {
    sessionManager.dispose();
  }
  if (toolExecutor) {
    toolExecutor.dispose();
  }
}
