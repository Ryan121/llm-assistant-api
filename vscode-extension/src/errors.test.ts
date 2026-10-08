/**
 * Unit tests for error handling
 */

import {
  ApiError,
  ToolError,
  ConfigError,
  SessionError,
  ErrorSeverity,
  formatError,
  isRetryableError,
  withErrorHandling,
} from './errors';

describe('Errors', () => {
  describe('ApiError', () => {
    it('should create an ApiError with status code and endpoint', () => {
      const error = new ApiError('Not found', 404, '/models');

      expect(error.name).toBe('ApiError');
      expect(error.message).toBe('Not found');
      expect(error.statusCode).toBe(404);
      expect(error.endpoint).toBe('/models');
    });

    it('should create an ApiError without optional fields', () => {
      const error = new ApiError('Something went wrong');

      expect(error.name).toBe('ApiError');
      expect(error.message).toBe('Something went wrong');
      expect(error.statusCode).toBeUndefined();
      expect(error.endpoint).toBeUndefined();
    });
  });

  describe('ToolError', () => {
    it('should create a ToolError with tool name', () => {
      const error = new ToolError('File not found', 'read_file');

      expect(error.name).toBe('ToolError');
      expect(error.message).toBe('File not found');
      expect(error.toolName).toBe('read_file');
      expect(error.recoverable).toBe(true);
    });

    it('should create a non-recoverable ToolError', () => {
      const error = new ToolError('Permission denied', 'edit_file', false);

      expect(error.name).toBe('ToolError');
      expect(error.message).toBe('Permission denied');
      expect(error.toolName).toBe('edit_file');
      expect(error.recoverable).toBe(false);
    });
  });

  describe('ConfigError', () => {
    it('should create a ConfigError with setting name', () => {
      const error = new ConfigError('Invalid value', 'llm-assistant.apiKey');

      expect(error.name).toBe('ConfigError');
      expect(error.message).toBe('Invalid value');
      expect(error.setting).toBe('llm-assistant.apiKey');
    });
  });

  describe('SessionError', () => {
    it('should create a SessionError with session ID', () => {
      const error = new SessionError('Session not found', 'session_123');

      expect(error.name).toBe('SessionError');
      expect(error.message).toBe('Session not found');
      expect(error.sessionId).toBe('session_123');
    });
  });

  describe('formatError', () => {
    it('should format ApiError with 401 status', () => {
      const error = new ApiError('Invalid token', 401);
      expect(formatError(error)).toBe('Authentication failed. Please check your API key in settings.');
    });

    it('should format ApiError with 404 status', () => {
      const error = new ApiError('Not found', 404, '/models');
      expect(formatError(error)).toBe('Resource not found: /models');
    });

    it('should format ApiError with 503 status', () => {
      const error = new ApiError('Service unavailable', 503);
      expect(formatError(error)).toBe('The LLM gateway is unavailable. Please ensure it is running.');
    });

    it('should format ApiError with generic status', () => {
      const error = new ApiError('Bad request', 400);
      expect(formatError(error)).toBe('API error (400): Bad request');
    });

    it('should format ToolError', () => {
      const error = new ToolError('File not found', 'read_file');
      expect(formatError(error)).toBe('read_file: File not found');
    });

    it('should format ConfigError with setting', () => {
      const error = new ConfigError('Missing value', 'llm-assistant.apiKey');
      expect(formatError(error)).toBe('Configuration error: Missing value. Check setting: llm-assistant.apiKey');
    });

    it('should format ConfigError without setting', () => {
      const error = new ConfigError('Invalid configuration');
      expect(formatError(error)).toBe('Configuration error: Invalid configuration');
    });

    it('should format SessionError', () => {
      const error = new SessionError('Session expired');
      expect(formatError(error)).toBe('Session error: Session expired');
    });

    it('should format regular Error', () => {
      const error = new Error('Something went wrong');
      expect(formatError(error)).toBe('Something went wrong');
    });

    it('should format unknown error types', () => {
      expect(formatError('string error')).toBe('string error');
      expect(formatError(123)).toBe('123');
      expect(formatError(null)).toBe('null');
    });
  });

  describe('isRetryableError', () => {
    it('should return true for 429 status', () => {
      const error = new ApiError('Rate limited', 429);
      expect(isRetryableError(error)).toBe(true);
    });

    it('should return true for 5xx status', () => {
      const error = new ApiError('Server error', 500);
      expect(isRetryableError(error)).toBe(true);
    });

    it('should return true for network errors', () => {
      const error = new Error('Connection refused');
      expect(isRetryableError(error)).toBe(true);
    });

    it('should return true for timeout errors', () => {
      const error = new Error('Request timeout');
      expect(isRetryableError(error)).toBe(true);
    });

    it('should return false for 4xx errors (except 429)', () => {
      const error = new ApiError('Bad request', 400);
      expect(isRetryableError(error)).toBe(false);
    });

    it('should return false for non-retryable errors', () => {
      const error = new Error('Invalid argument');
      expect(isRetryableError(error)).toBe(false);
    });
  });

  describe('withErrorHandling', () => {
    it('should return successful result', async () => {
      const promise = Promise.resolve('success');

      const result = await withErrorHandling(promise, 'Test operation');

      expect(result).toBe('success');
    });

    it('should throw error when no default value and no retries', async () => {
      const promise = Promise.reject(new Error('Failed'));

      await expect(withErrorHandling(promise, 'Test operation')).rejects.toThrow('Failed');
    });

    it('should return default value on failure', async () => {
      const promise = Promise.reject(new Error('Failed'));

      const result = await withErrorHandling(promise, 'Test operation', {
        defaultValue: 'default',
      });

      expect(result).toBe('default');
    });

    it('should retry on failure', async () => {
      let attempts = 0;
      const promise = new Promise<string>((resolve, reject) => {
        attempts++;
        if (attempts < 3) {
          reject(new Error('Temporary failure'));
        } else {
          resolve('success');
        }
      });

      const result = await withErrorHandling(promise, 'Test operation', {
        retryCount: 3,
        retryDelay: 10,
      });

      expect(result).toBe('success');
      expect(attempts).toBe(3);
    });

    it('should call onError callback', async () => {
      const onError = jest.fn();
      const promise = Promise.reject(new Error('Failed'));

      await withErrorHandling(promise, 'Test operation', {
        defaultValue: 'default',
        onError,
      });

      expect(onError).toHaveBeenCalledTimes(1);
      expect(onError).toHaveBeenCalledWith(expect.any(Error));
    });
  });
});
