/**
 * Session management for the LLM Assistant Agent extension
 */

import * as vscode from 'vscode';
import { ChatSession, ChatMessage } from './types';

const SESSIONS_KEY = 'llm-assistant.sessions';
const MAX_SESSIONS = 50;

/**
 * Manages chat sessions persistence and retrieval
 */
export class SessionManager implements vscode.Disposable {
  private context: vscode.ExtensionContext;
  private currentSessionId: string | null = null;

  constructor(context: vscode.ExtensionContext) {
    this.context = context;
  }

  /**
   * Create a new chat session
   */
  createSession(workspace?: string, model?: string): ChatSession {
    const id = this.generateId();
    const now = Date.now();

    const session: ChatSession = {
      id,
      workspace: workspace || vscode.workspace.workspaceFolders?.[0]?.uri.fsPath || '',
      model: model || '',
      messages: [],
      createdAt: now,
      updatedAt: now,
    };

    this.saveSession(session);
    this.currentSessionId = id;

    return session;
  }

  /**
   * Get the current session or create one if none exists
   */
  getCurrentSession(): ChatSession {
    if (this.currentSessionId) {
      const session = this.getSession(this.currentSessionId);
      if (session) {
        return session;
      }
    }

    // Get or create latest session
    const sessions = this.listSessions();
    if (sessions.length > 0) {
      this.currentSessionId = sessions[0].id;
      return sessions[0];
    }

    return this.createSession();
  }

  /**
   * Get a session by ID
   */
  getSession(id: string): ChatSession | undefined {
    const sessions = this.getSessionsFromStorage();
    return sessions.find(s => s.id === id);
  }

  /**
   * Save a session
   */
  saveSession(session: ChatSession): void {
    const sessions = this.getSessionsFromStorage();
    const index = sessions.findIndex(s => s.id === session.id);

    session.updatedAt = Date.now();

    if (index >= 0) {
      sessions[index] = session;
    } else {
      sessions.unshift(session);
      // Trim old sessions
      while (sessions.length > MAX_SESSIONS) {
        sessions.pop();
      }
    }

    this.context.globalState.update(SESSIONS_KEY, sessions);
  }

  /**
   * Add a message to a session
   */
  addMessage(sessionId: string, message: ChatMessage): void {
    const session = this.getSession(sessionId);
    if (session) {
      session.messages.push(message);
      this.saveSession(session);

      // Auto-generate title from first user message
      if (!session.title && message.role === 'user') {
        session.title = message.content?.slice(0, 50) || 'New Chat';
        if (message.content && message.content.length > 50) {
          session.title += '...';
        }
        this.saveSession(session);
      }
    }
  }

  /**
   * List all sessions
   */
  listSessions(): ChatSession[] {
    return this.getSessionsFromStorage();
  }

  /**
   * Delete a session
   */
  deleteSession(id: string): boolean {
    const sessions = this.getSessionsFromStorage();
    const index = sessions.findIndex(s => s.id === id);

    if (index >= 0) {
      sessions.splice(index, 1);
      this.context.globalState.update(SESSIONS_KEY, sessions);

      if (this.currentSessionId === id) {
        this.currentSessionId = sessions.length > 0 ? sessions[0].id : null;
      }

      return true;
    }

    return false;
  }

  /**
   * Delete all sessions
   */
  deleteAllSessions(): void {
    this.context.globalState.update(SESSIONS_KEY, []);
    this.currentSessionId = null;
  }

  /**
   * Show sessions in a quick pick
   */
  async showSessions(): Promise<ChatSession | undefined> {
    const sessions = this.listSessions();

    if (sessions.length === 0) {
      vscode.window.showInformationMessage('No saved sessions');
      return undefined;
    }

    const items = sessions.map(session => ({
      label: session.title || 'Untitled Chat',
      description: new Date(session.updatedAt).toLocaleString(),
      detail: `${session.messages.length} messages`,
      session,
    }));

    const selected = await vscode.window.showQuickPick(items, {
      placeHolder: 'Select a session',
      canPickMany: false,
    });

    if (selected) {
      this.currentSessionId = selected.session.id;
      return selected.session;
    }

    return undefined;
  }

  /**
   * Set the current session
   */
  setCurrentSession(sessionId: string): void {
    this.currentSessionId = sessionId;
  }

  /**
   * Get the current session ID
   */
  getCurrentSessionId(): string | null {
    return this.currentSessionId;
  }

  /**
   * Clear the current session
   */
  clearCurrentSession(): void {
    this.currentSessionId = null;
  }

  /**
   * Get sessions from storage
   */
  private getSessionsFromStorage(): ChatSession[] {
    return this.context.globalState.get<ChatSession[]>(SESSIONS_KEY, []);
  }

  /**
   * Generate a unique session ID
   */
  private generateId(): string {
    return `session_${Date.now()}_${Math.random().toString(36).substr(2, 9)}`;
  }

  dispose(): void {
    // Cleanup if needed
  }
}
