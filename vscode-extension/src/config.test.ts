/**
 * Unit tests for configuration management
 */

import { getConfig, updateConfig, validateConfig, isConfigured } from '../src/config';
import * as vscode from 'vscode';

describe('Config', () => {
  const mockConfig = {
    get: jest.fn(),
    update: jest.fn(),
  };

  beforeEach(() => {
    jest.clearAllMocks();
    (vscode.workspace.getConfiguration as jest.Mock).mockReturnValue(mockConfig);
  });

  describe('getConfig', () => {
    it('should return default values when no config is set', () => {
      mockConfig.get.mockReturnValue(undefined);

      const config = getConfig();

      expect(config.baseUrl).toBe('http://127.0.0.1:8081/v1');
      expect(config.apiKey).toBe('');
      expect(config.model).toBe('');
      expect(config.contextWindow).toBe(131072);
      expect(config.autoApproveEdits).toBe(false);
      expect(config.autoApproveCommands).toBe(false);
    });

    it('should return configured values when set', () => {
      mockConfig.get.mockImplementation((key: string) => {
        const values: Record<string, any> = {
          baseUrl: 'http://custom-host:9000/v1',
          apiKey: 'sk-test-key',
          model: 'custom-model',
          contextWindow: 65536,
          autoApproveEdits: true,
          autoApproveCommands: true,
        };
        return values[key];
      });

      const config = getConfig();

      expect(config.baseUrl).toBe('http://custom-host:9000/v1');
      expect(config.apiKey).toBe('sk-test-key');
      expect(config.model).toBe('custom-model');
      expect(config.contextWindow).toBe(65536);
      expect(config.autoApproveEdits).toBe(true);
      expect(config.autoApproveCommands).toBe(true);
    });
  });

  describe('validateConfig', () => {
    it('should return no errors for valid config', () => {
      const config = {
        baseUrl: 'http://127.0.0.1:8081/v1',
        apiKey: 'sk-test-key',
        model: 'test-model',
        contextWindow: 131072,
        autoApproveEdits: false,
        autoApproveCommands: false,
      };

      const errors = validateConfig(config);

      expect(errors).toEqual([]);
    });

    it('should return error when baseUrl is missing', () => {
      const config = {
        baseUrl: '',
        apiKey: 'sk-test-key',
        model: 'test-model',
        contextWindow: 131072,
        autoApproveEdits: false,
        autoApproveCommands: false,
      };

      const errors = validateConfig(config);

      expect(errors).toContain('Base URL is required');
    });

    it('should return error when baseUrl is invalid', () => {
      const config = {
        baseUrl: 'not-a-url',
        apiKey: 'sk-test-key',
        model: 'test-model',
        contextWindow: 131072,
        autoApproveEdits: false,
        autoApproveCommands: false,
      };

      const errors = validateConfig(config);

      expect(errors).toContain('Base URL must be a valid URL');
    });

    it('should return error when apiKey is missing', () => {
      const config = {
        baseUrl: 'http://127.0.0.1:8081/v1',
        apiKey: '',
        model: 'test-model',
        contextWindow: 131072,
        autoApproveEdits: false,
        autoApproveCommands: false,
      };

      const errors = validateConfig(config);

      expect(errors).toContain('API key is required');
    });

    it('should return error when contextWindow is invalid', () => {
      const config = {
        baseUrl: 'http://127.0.0.1:8081/v1',
        apiKey: 'sk-test-key',
        model: 'test-model',
        contextWindow: -100,
        autoApproveEdits: false,
        autoApproveCommands: false,
      };

      const errors = validateConfig(config);

      expect(errors).toContain('Context window must be positive');
    });
  });

  describe('isConfigured', () => {
    it('should return true when baseUrl and apiKey are set', () => {
      const config = {
        baseUrl: 'http://127.0.0.1:8081/v1',
        apiKey: 'sk-test-key',
        model: '',
        contextWindow: 131072,
        autoApproveEdits: false,
        autoApproveCommands: false,
      };

      expect(isConfigured(config)).toBe(true);
    });

    it('should return false when baseUrl is missing', () => {
      const config = {
        baseUrl: '',
        apiKey: 'sk-test-key',
        model: '',
        contextWindow: 131072,
        autoApproveEdits: false,
        autoApproveCommands: false,
      };

      expect(isConfigured(config)).toBe(false);
    });

    it('should return false when apiKey is missing', () => {
      const config = {
        baseUrl: 'http://127.0.0.1:8081/v1',
        apiKey: '',
        model: '',
        contextWindow: 131072,
        autoApproveEdits: false,
        autoApproveCommands: false,
      };

      expect(isConfigured(config)).toBe(false);
    });
  });
});
