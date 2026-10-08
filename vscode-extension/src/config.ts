/**
 * Configuration management for the LLM Assistant Agent extension
 */

import * as vscode from 'vscode';
import { ExtensionConfig } from './types';

const CONFIG_SECTION = 'llm-assistant';

/**
 * Get the current extension configuration from VS Code settings
 */
export function getConfig(): ExtensionConfig {
  const config = vscode.workspace.getConfiguration(CONFIG_SECTION);

  return {
    baseUrl: config.get<string>('baseUrl') || 'http://127.0.0.1:8081/v1',
    apiKey: config.get<string>('apiKey') || '',
    model: config.get<string>('model') || '',
    contextWindow: config.get<number>('contextWindow') || 131072,
    autoApproveEdits: config.get<boolean>('autoApproveEdits') || false,
    autoApproveCommands: config.get<boolean>('autoApproveCommands') || false,
  };
}

/**
 * Update a configuration value
 */
export async function updateConfig(
  key: keyof ExtensionConfig,
  value: any,
  target: vscode.ConfigurationTarget = vscode.ConfigurationTarget.Workspace
): Promise<void> {
  const config = vscode.workspace.getConfiguration(CONFIG_SECTION);
  await config.update(key, value, target);
}

/**
 * Validate the current configuration
 */
export function validateConfig(config: ExtensionConfig): string[] {
  const errors: string[] = [];

  if (!config.baseUrl) {
    errors.push('Base URL is required');
  } else {
    try {
      new URL(config.baseUrl);
    } catch {
      errors.push('Base URL must be a valid URL');
    }
  }

  if (!config.apiKey) {
    errors.push('API key is required');
  }

  if (config.contextWindow <= 0) {
    errors.push('Context window must be positive');
  }

  return errors;
}

/**
 * Check if the configuration is complete
 */
export function isConfigured(config: ExtensionConfig): boolean {
  return !!(config.baseUrl && config.apiKey);
}
