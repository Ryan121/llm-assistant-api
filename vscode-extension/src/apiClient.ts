/**
 * API client for communicating with the LLM gateway
 */

import * as vscode from 'vscode';
import { ChatMessage, ToolCall, ToolSchema, ExtensionConfig } from './types';
import { getConfig } from './config';
import { ApiError, isRetryableError, withErrorHandling } from './errors';

/**
 * Client for the OpenAI-compatible LLM gateway API
 */
export class ApiClient {
  private config: ExtensionConfig;

  constructor(config?: ExtensionConfig) {
    this.config = config || getConfig();
  }

  /**
   * Get the configured base URL
   */
  getBaseUrl(): string {
    return this.config.baseUrl;
  }

  /**
   * Get the configured API key
   */
  getApiKey(): string {
    return this.config.apiKey;
  }

  /**
   * Get the configured model ID
   */
  getModel(): string {
    return this.config.model;
  }

  /**
   * List available models from the gateway
   */
  async listModels(): Promise<string[]> {
    return withErrorHandling(
      (async () => {
        const response = await this.fetch('/models', {
          method: 'GET',
        });

        if (!response.ok) {
          throw new ApiError(
            `Failed to list models: ${response.status} ${response.statusText}`,
            response.status,
            '/models'
          );
        }

        const data = await response.json();
        return data.data?.map((m: any) => m.id) || [];
      })(),
      'List models',
      {
        retryCount: 2,
        retryDelay: 500,
        defaultValue: [],
      }
    ) as Promise<string[]>;
  }

  /**
   * Send a chat completion request to the gateway
   */
  async chatCompletion(
    messages: ChatMessage[],
    tools?: ToolSchema[],
    onStreamChunk?: (chunk: string) => void
  ): Promise<{ content: string; tool_calls?: ToolCall[] }> {
    const model = this.getModel() || (await this.detectModel());

    const payload: any = {
      model,
      messages,
      stream: !!onStreamChunk,
      max_tokens: 4096,
      temperature: 0.7,
    };

    if (tools && tools.length > 0) {
      payload.tools = tools;
      payload.tool_choice = 'auto';
    }

    return withErrorHandling(
      (async () => {
        const response = await this.fetch('/chat/completions', {
          method: 'POST',
          headers: {
            'Content-Type': 'application/json',
          },
          body: JSON.stringify(payload),
        });

        if (!response.ok) {
          const errorText = await response.text().catch(() => 'Unknown error');
          throw new ApiError(
            `API error: ${response.status} ${response.statusText} - ${errorText}`,
            response.status,
            '/chat/completions'
          );
        }

        if (onStreamChunk && response.body) {
          return this.handleStreamingResponse(response.body, onStreamChunk);
        } else {
          const data = await response.json();
          const choice = data.choices?.[0];
          return {
            content: choice?.message?.content || '',
            tool_calls: choice?.message?.tool_calls || [],
          };
        }
      })(),
      'Chat completion',
      {
        retryCount: isRetryableError ? 1 : 0,
        retryDelay: 1000,
      }
    ) as Promise<{ content: string; tool_calls?: ToolCall[] }>;
  }

  /**
   * Handle streaming response from the gateway
   */
  private async handleStreamingResponse(
    body: ReadableStream<Uint8Array>,
    onChunk: (chunk: string) => void
  ): Promise<{ content: string; tool_calls?: ToolCall[] }> {
    const reader = body.getReader();
    const decoder = new TextDecoder();
    let content = '';
    let toolCalls: ToolCall[] = [];

    try {
      while (true) {
        const { done, value } = await reader.read();
        if (done) break;

        const chunk = decoder.decode(value);
        const lines = chunk.split('\n').filter(line => line.trim());

        for (const line of lines) {
          if (line.startsWith('data: ')) {
            const data = line.slice(6);
            if (data === '[DONE]') continue;

            try {
              const parsed = JSON.parse(data);
              const choice = parsed.choices?.[0];

              if (choice?.delta?.content) {
                content += choice.delta.content;
                onChunk(choice.delta.content);
              }

              if (choice?.delta?.tool_calls) {
                // Accumulate tool call chunks
                for (const tc of choice.delta.tool_calls) {
                  if (!toolCalls[tc.index]) {
                    toolCalls[tc.index] = {
                      id: tc.id || '',
                      type: 'function',
                      function: { name: '', arguments: '' },
                    };
                  }
                  if (tc.function?.name) {
                    toolCalls[tc.index].function.name += tc.function.name;
                  }
                  if (tc.function?.arguments) {
                    toolCalls[tc.index].function.arguments += tc.function.arguments;
                  }
                }
              }
            } catch (e) {
              console.warn('Failed to parse SSE chunk:', e);
            }
          }
        }
      }
    } finally {
      reader.releaseLock();
    }

    return { content, tool_calls: toolCalls.length > 0 ? toolCalls : undefined };
  }

  /**
   * Make an authenticated request to the gateway
   */
  private async fetch(path: string, init: RequestInit = {}): Promise<Response> {
    const url = `${this.config.baseUrl.replace(/\/$/, '')}${path}`;
    const headers = new Headers(init.headers);
    headers.set('Authorization', `Bearer ${this.config.apiKey}`);
    headers.set('User-Agent', 'LLM-Assistant-VSCode/0.1.0');

    try {
      const response = await fetch(url, {
        ...init,
        headers,
        signal: AbortSignal.timeout(60000), // 60 second timeout
      });

      // Check for authentication errors
      if (response.status === 401) {
        throw new ApiError(
          'Authentication failed. Please check your API key in settings.',
          401,
          path
        );
      }

      // Check for gateway unavailable
      if (response.status === 503) {
        throw new ApiError(
          'The LLM gateway is unavailable. Please ensure it is running.',
          503,
          path
        );
      }

      return response;
    } catch (error) {
      if (error instanceof ApiError) {
        throw error;
      }

      // Network errors
      if (error instanceof TypeError && error.message.includes('fetch')) {
        throw new ApiError(
          `Cannot connect to gateway at ${url}. Please check that the gateway is running and the base URL is correct.`,
          undefined,
          path
        );
      }

      // Timeout errors
      if (error instanceof Error && error.name === 'AbortError') {
        throw new ApiError(
          'Request timed out. The gateway may be slow or the model is still loading.',
          undefined,
          path
        );
      }

      throw error;
    }
  }

  /**
   * Detect the model ID from the gateway if not configured
   */
  private async detectModel(): Promise<string> {
    const models = await this.listModels();
    if (models.length === 0) {
      throw new Error('No models available from the gateway');
    }
    return models[0];
  }

  /**
   * Update the configuration
   */
  updateConfig(config: Partial<ExtensionConfig>) {
    this.config = { ...this.config, ...config };
  }
}
