/**
 * Error handling utilities for the LLM Assistant Agent extension
 */

import * as vscode from 'vscode';

/**
 * Custom error class for API-related errors
 */
export class ApiError extends Error {
  constructor(
    message: string,
    public readonly statusCode?: number,
    public readonly endpoint?: string
  ) {
    super(message);
    this.name = 'ApiError';
  }
}

/**
 * Custom error class for tool execution errors
 */
export class ToolError extends Error {
  constructor(
    message: string,
    public readonly toolName: string,
    public readonly recoverable: boolean = true
  ) {
    super(message);
    this.name = 'ToolError';
  }
}

/**
 * Custom error class for configuration errors
 */
export class ConfigError extends Error {
  constructor(
    message: string,
    public readonly setting?: string
  ) {
    super(message);
    this.name = 'ConfigError';
  }
}

/**
 * Custom error class for session errors
 */
export class SessionError extends Error {
  constructor(
    message: string,
    public readonly sessionId?: string
  ) {
    super(message);
    this.name = 'SessionError';
  }
}

/**
 * Error severity levels
 */
export enum ErrorSeverity {
  Info = 'info',
  Warning = 'warning',
  Error = 'error',
  Critical = 'critical',
}

/**
 * Display an error to the user with appropriate UI
 */
export async function showError(
  error: unknown,
  severity: ErrorSeverity = ErrorSeverity.Error,
  actions?: Array<{ label: string; action: () => Thenable<void> | void }>
): Promise<void> {
  const message = formatError(error);

  switch (severity) {
    case ErrorSeverity.Info:
      vscode.window.showInformationMessage(message, ...(actions?.map(a => a.label) || []));
      break;
    case ErrorSeverity.Warning:
      vscode.window.showWarningMessage(message, ...(actions?.map(a => a.label) || []));
      break;
    case ErrorSeverity.Error:
    case ErrorSeverity.Critical:
      vscode.window.showErrorMessage(message, ...(actions?.map(a => a.label) || []));
      break;
  }

  // Log to output channel
  logError(error, severity);
}

/**
 * Format an error into a user-friendly message
 */
export function formatError(error: unknown): string {
  if (error instanceof ApiError) {
    if (error.statusCode === 401) {
      return 'Authentication failed. Please check your API key in settings.';
    }
    if (error.statusCode === 404) {
      return `Resource not found: ${error.endpoint || 'unknown endpoint'}`;
    }
    if (error.statusCode === 503) {
      return 'The LLM gateway is unavailable. Please ensure it is running.';
    }
    if (error.statusCode) {
      return `API error (${error.statusCode}): ${error.message}`;
    }
    return error.message;
  }

  if (error instanceof ToolError) {
    return `${error.toolName}: ${error.message}`;
  }

  if (error instanceof ConfigError) {
    if (error.setting) {
      return `Configuration error: ${error.message}. Check setting: ${error.setting}`;
    }
    return `Configuration error: ${error.message}`;
  }

  if (error instanceof SessionError) {
    return `Session error: ${error.message}`;
  }

  if (error instanceof Error) {
    return error.message;
  }

  return String(error);
}

/**
 * Log an error to the output channel
 */
export function logError(error: unknown, severity: ErrorSeverity = ErrorSeverity.Error): void {
  const timestamp = new Date().toISOString();
  const level = severity.toUpperCase();

  if (error instanceof Error) {
    console.error(`[${timestamp}] ${level}: ${error.name}: ${error.message}`);
    if (error.stack) {
      console.error(error.stack);
    }
  } else {
    console.error(`[${timestamp}] ${level}: ${String(error)}`);
  }
}

/**
 * Wrap a promise with error handling
 */
export async function withErrorHandling<T>(
  promise: Promise<T>,
  context: string,
  options?: {
    defaultValue?: T;
    retryCount?: number;
    retryDelay?: number;
    onError?: (error: unknown) => void;
  }
): Promise<T | undefined> {
  const { defaultValue, retryCount = 0, retryDelay = 1000, onError } = options || {};

  let lastError: unknown;

  for (let attempt = 0; attempt <= retryCount; attempt++) {
    try {
      return await promise;
    } catch (error) {
      lastError = error;

      if (onError) {
        onError(error);
      }

      if (attempt < retryCount) {
        console.warn(`${context} failed (attempt ${attempt + 1}/${retryCount + 1}), retrying...`);
        await new Promise(resolve => setTimeout(resolve, retryDelay));
      }
    }
  }

  // All retries failed
  console.error(`${context} failed after ${retryCount + 1} attempts:`, lastError);

  if (defaultValue !== undefined) {
    return defaultValue;
  }

  throw lastError;
}

/**
 * Check if an error is retryable
 */
export function isRetryableError(error: unknown): boolean {
  if (error instanceof ApiError) {
    // Retry on server errors and rate limiting
    return error.statusCode === 429 || (error.statusCode ?? 0) >= 500;
  }

  if (error instanceof Error) {
    const message = error.message.toLowerCase();
    return (
      message.includes('timeout') ||
      message.includes('network') ||
      message.includes('connection') ||
      message.includes('econnrefused') ||
      message.includes('econnreset')
    );
  }

  return false;
}

/**
 * Create a safe async function that won't throw unhandled errors
 */
export function safeAsync<T extends (...args: any[]) => Promise<any>>(
  fn: T,
  errorHandler?: (error: unknown, ...args: Parameters<T>) => void
): T {
  return (async (...args: Parameters<T>): Promise<ReturnType<T> | undefined> => {
    try {
      return await fn(...args);
    } catch (error) {
      if (errorHandler) {
        errorHandler(error, ...args);
      } else {
        showError(error, ErrorSeverity.Error);
      }
      return undefined;
    }
  }) as T;
}
