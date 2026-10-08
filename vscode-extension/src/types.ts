/**
 * Type definitions for the LLM Assistant Agent extension
 */

/** Message role in a chat conversation */
export type MessageRole = 'user' | 'assistant' | 'system' | 'tool';

/** A single message in the chat history */
export interface ChatMessage {
  role: MessageRole;
  content: string | null;
  tool_calls?: ToolCall[];
  tool_call_id?: string;
  name?: string;
}

/** A tool call from the model */
export interface ToolCall {
  id: string;
  type: 'function';
  function: {
    name: string;
    arguments: string;
  };
}

/** A tool call result */
export interface ToolResult {
  tool_call_id: string;
  role: 'tool';
  name: string;
  content: string;
}

/** A chat session */
export interface ChatSession {
  id: string;
  workspace: string;
  model: string;
  messages: ChatMessage[];
  createdAt: number;
  updatedAt: number;
  title?: string;
}

/** Tool schema definition */
export interface ToolSchema {
  type: 'function';
  function: {
    name: string;
    description: string;
    parameters: {
      type: 'object';
      properties: Record<string, {
        type: string;
        description?: string;
      }>;
      required?: string[];
    };
  };
}

/** Configuration for the extension */
export interface ExtensionConfig {
  baseUrl: string;
  apiKey: string;
  model: string;
  contextWindow: number;
  autoApproveEdits: boolean;
  autoApproveCommands: boolean;
}

/** Edit approval request */
export interface EditApprovalRequest {
  path: string;
  oldString: string;
  newString: string;
  replaceAll: boolean;
}

/** Command approval request */
export interface CommandApprovalRequest {
  command: string;
  timeout: number;
  description?: string;
}

/** Result of a tool execution */
export interface ToolExecutionResult {
  success: boolean;
  content: string;
  diff?: string;
  path?: string;
  is_error?: boolean;
}
