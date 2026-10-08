/**
 * Tool execution with approval workflows
 */

import * as vscode from 'vscode';
import * as path from 'path';
import * as fs from 'fs';
import { spawn } from 'child_process';
import { ToolExecutionResult, EditApprovalRequest, CommandApprovalRequest } from './types';
import { getConfig } from './config';

/**
 * Executes tools with user approval workflows
 */
export class ToolExecutor implements vscode.Disposable {
  private context: vscode.ExtensionContext;
  private outputChannel: vscode.OutputChannel;

  constructor(context: vscode.ExtensionContext) {
    this.context = context;
    this.outputChannel = vscode.window.createOutputChannel('LLM Assistant Tools');
  }

  /**
   * Execute a tool call with appropriate approval workflow
   */
  async executeTool(
    name: string,
    args: Record<string, any>
  ): Promise<ToolExecutionResult> {
    this.outputChannel.appendLine(`\n--- Tool: ${name} ---`);
    this.outputChannel.appendLine(`Args: ${JSON.stringify(args, null, 2)}`);

    try {
      switch (name) {
        case 'list_files':
          return this.listFiles(args);
        case 'grep':
          return this.grep(args);
        case 'read_file':
          return this.readFile(args);
        case 'edit_file':
          return this.editFile(args);
        case 'write_file':
          return this.writeFile(args);
        case 'read_document':
          return this.readDocument(args);
        case 'git_diff':
          return this.gitDiff(args);
        case 'run':
          return this.runCommand(args);
        default:
          return {
            success: false,
            content: `Unknown tool: ${name}`,
            is_error: true,
          };
      }
    } catch (error) {
      const message = error instanceof Error ? error.message : String(error);
      this.outputChannel.appendLine(`Error: ${message}`);
      return {
        success: false,
        content: message,
        is_error: true,
      };
    }
  }

  /**
   * List files in the workspace
   */
  private listFiles(args: { pattern?: string }): ToolExecutionResult {
    const workspaceFolder = vscode.workspace.workspaceFolders?.[0];
    if (!workspaceFolder) {
      return {
        success: false,
        content: 'No workspace folder open',
        is_error: true,
      };
    }

    const pattern = args.pattern || '**/*';
    const uris = vscode.workspace.findFilesSync(pattern, '**/node_modules/**,**/.git/**');

    const relativePaths = uris.map(uri =>
      path.relative(workspaceFolder.uri.fsPath, uri.fsPath)
    );

    if (relativePaths.length === 0) {
      return { success: true, content: 'No files found' };
    }

    const maxFiles = 200;
    let content = relativePaths.slice(0, maxFiles).join('\n');

    if (relativePaths.length > maxFiles) {
      content += `\n... and ${relativePaths.length - maxFiles} more. Narrow the pattern.`;
    }

    return { success: true, content };
  }

  /**
   * Search file contents with regex
   */
  private grep(args: { pattern: string; glob?: string }): ToolExecutionResult {
    if (!args.pattern) {
      return {
        success: false,
        content: 'grep needs a pattern',
        is_error: true,
      };
    }

    try {
      new RegExp(args.pattern);
    } catch (e) {
      return {
        success: false,
        content: `Invalid regular expression: ${args.pattern}`,
        is_error: true,
      };
    }

    const workspaceFolder = vscode.workspace.workspaceFolders?.[0];
    if (!workspaceFolder) {
      return {
        success: false,
        content: 'No workspace folder open',
        is_error: true,
      };
    }

    const globPattern = args.glob || '**/*';
    const uris = vscode.workspace.findFilesSync(globPattern, '**/node_modules/**,**/.git/**');

    const regex = new RegExp(args.pattern);
    const matches: string[] = [];
    const maxMatches = 100;

    for (const uri of uris) {
      if (matches.length >= maxMatches) break;

      try {
        const content = fs.readFileSync(uri.fsPath, 'utf-8');
        const lines = content.split('\n');

        for (let i = 0; i < lines.length; i++) {
          if (regex.test(lines[i])) {
            const relativePath = path.relative(workspaceFolder.uri.fsPath, uri.fsPath);
            matches.push(`${relativePath}:${i + 1}: ${lines[i].slice(0, 200)}`);

            if (matches.length >= maxMatches) {
              matches.push(`... stopped at ${maxMatches} matches.`);
              break;
            }
          }
        }
      } catch {
        // Skip binary or unreadable files
      }
    }

    return {
      success: true,
      content: matches.length > 0 ? matches.join('\n') : 'No matches found',
    };
  }

  /**
   * Read a file with optional line range
   */
  private readFile(args: { path: string; begin?: number; end?: number }): ToolExecutionResult {
    const filePath = this.resolvePath(args.path);

    if (!fs.existsSync(filePath)) {
      return {
        success: false,
        content: `File not found: ${args.path}`,
        is_error: true,
      };
    }

    try {
      let content = fs.readFileSync(filePath, 'utf-8');
      const lines = content.split('\n');

      if (args.begin !== undefined || args.end !== undefined) {
        const begin = args.begin || 1;
        const end = args.end || lines.length;

        if (begin > lines.length) {
          return {
            success: false,
            content: `File has only ${lines.length} lines, line ${begin} is past the end`,
            is_error: true,
          };
        }

        const slicedLines = lines.slice(begin - 1, end);
        const numbered = slicedLines
          .map((line, i) => `${(begin + i).toString().padStart(6)}\t${line}`)
          .join('\n');

        content = `${args.path}, lines ${begin}-${Math.min(end, lines.length)} of ${lines.length}:\n${numbered}`;
      } else {
        const numbered = lines
          .map((line, i) => `${(i + 1).toString().padStart(6)}\t${line}`)
          .join('\n');
        content = numbered;
      }

      return { success: true, content, path: args.path };
    } catch (error) {
      return {
        success: false,
        content: `Error reading file: ${error instanceof Error ? error.message : String(error)}`,
        is_error: true,
      };
    }
  }

  /**
   * Edit a file with approval workflow
   */
  private async editFile(args: {
    path: string;
    old_string: string;
    new_string: string;
    replace_all?: boolean;
  }): Promise<ToolExecutionResult> {
    const config = getConfig();

    // Validate old_string is provided
    if (!args.old_string) {
      return {
        success: false,
        content: 'edit_file: old_string is required and must be non-empty',
        is_error: true,
      };
    }

    const filePath = this.resolvePath(args.path);

    // Check if file exists
    let before = '';
    if (fs.existsSync(filePath)) {
      try {
        before = fs.readFileSync(filePath, 'utf-8');
      } catch (error) {
        return {
          success: false,
          content: `Error reading file: ${error instanceof Error ? error.message : String(error)}`,
          is_error: true,
        };
      }
    }

    // Check if old_string exists in file
    if (!before.includes(args.old_string)) {
      return {
        success: false,
        content: `edit_file: old_string was not found in ${args.path}. Use grep to find the current content, then re-read the file.`,
        is_error: true,
      };
    }

    // Apply the edit
    const replaceAll = args.replace_all || false;
    let after: string;

    if (replaceAll) {
      after = before.split(args.old_string).join(args.new_string);
    } else {
      const index = before.indexOf(args.old_string);
      after = before.slice(0, index) + args.new_string + before.slice(index + args.old_string.length);
    }

    // Generate diff
    const diff = this.createDiff(args.path, before, after);

    // Ask for approval unless auto-approved
    if (!config.autoApproveEdits) {
      const approved = await this.requestEditApproval(args.path, diff, before, after);
      if (!approved) {
        return {
          success: false,
          content: 'The user declined this edit.',
          is_error: true,
        };
      }
    }

    // Apply the edit
    try {
      fs.writeFileSync(filePath, after, 'utf-8');
      return {
        success: true,
        content: `Edited ${args.path}`,
        diff,
        path: args.path,
      };
    } catch (error) {
      return {
        success: false,
        content: `Error writing file: ${error instanceof Error ? error.message : String(error)}`,
        is_error: true,
      };
    }
  }

  /**
   * Write a file with approval workflow
   */
  private async writeFile(args: { path: string; content: string }): Promise<ToolExecutionResult> {
    const config = getConfig();
    const filePath = this.resolvePath(args.path);

    let before = '';
    if (fs.existsSync(filePath)) {
      try {
        before = fs.readFileSync(filePath, 'utf-8');
      } catch {
        before = '';
      }
    }

    const diff = this.createDiff(args.path, before, args.content);
    const verb = before ? 'Overwrite' : 'Create';

    // Ask for approval unless auto-approved
    if (!config.autoApproveEdits && before) {
      const approved = await this.requestEditApproval(args.path, diff, before, args.content);
      if (!approved) {
        return {
          success: false,
          content: 'The user declined this edit.',
          is_error: true,
        };
      }
    }

    // Create directory if needed
    const dir = path.dirname(filePath);
    if (!fs.existsSync(dir)) {
      fs.mkdirSync(dir, { recursive: true });
    }

    try {
      fs.writeFileSync(filePath, args.content, 'utf-8');
      return {
        success: true,
        content: `${verb}d ${args.path}`,
        diff: before ? diff : undefined,
        path: args.path,
      };
    } catch (error) {
      return {
        success: false,
        content: `Error writing file: ${error instanceof Error ? error.message : String(error)}`,
        is_error: true,
      };
    }
  }

  /**
   * Request user approval for an edit
   */
  private async requestEditApproval(
    filePath: string,
    diff: string,
    before: string,
    after: string
  ): Promise<boolean> {
    // Create a temporary diff document
    const doc = await vscode.workspace.openTextDocument({
      content: diff,
      language: 'diff',
    });

    await vscode.window.showTextDocument(doc, {
      viewColumn: vscode.ViewColumn.Beside,
      preview: true,
    });

    const accept = await vscode.window.showInformationMessage(
      `Apply this edit to ${path.basename(filePath)}?`,
      { modal: true },
      'Accept',
      'Reject'
    );

    await vscode.commands.executeCommand('workbench.action.closeActiveEditor');

    return accept === 'Accept';
  }

  /**
   * Run a shell command with approval workflow
   */
  private async runCommand(args: { command: string; timeout_seconds?: number }): Promise<ToolExecutionResult> {
    const config = getConfig();

    if (!args.command) {
      return {
        success: false,
        content: 'run needs a command',
        is_error: true,
      };
    }

    // Check for destructive git commands
    const destructiveGitPattern = /\bgit\s+(?:checkout\b|restore\b|reset\b(?!\s+--soft\b)|clean\b|stash\b)/;
    const isDestructive = destructiveGitPattern.test(args.command);

    // Ask for approval unless auto-approved
    if (!config.autoApproveCommands) {
      const warning = isDestructive
        ? '⚠️ This command may discard uncommitted changes'
        : undefined;

      const description = isDestructive
        ? `Run (DESTRUCTIVE): ${args.command}`
        : `Run: ${args.command}`;

      const approveMsg = warning
        ? `${description}\n\n${warning}`
        : description;

      const approved = await vscode.window.showInformationMessage(
        approveMsg,
        { modal: true },
        'Allow',
        'Deny'
      );

      if (approved !== 'Allow') {
        return {
          success: false,
          content: 'The user declined this command.',
          is_error: true,
        };
      }
    }

    const timeout = (args.timeout_seconds || 120) * 1000;

    try {
      const workspaceFolder = vscode.workspace.workspaceFolders?.[0]?.uri.fsPath;
      const result = await this.executeInTerminal(args.command, workspaceFolder, timeout);

      const output = result.stdout || result.stderr || '(no output)';
      const truncated = output.length > 20000 ? output.slice(0, 20000) + '\n... output truncated.' : output;

      return {
        success: result.exitCode === 0,
        content: `exit ${result.exitCode}\n${truncated}`,
        is_error: result.exitCode !== 0,
      };
    } catch (error) {
      return {
        success: false,
        content: `Error running command: ${error instanceof Error ? error.message : String(error)}`,
        is_error: true,
      };
    }
  }

  /**
   * Execute a command in the integrated terminal
   */
  private async executeInTerminal(
    command: string,
    cwd?: string,
    timeout?: number
  ): Promise<{ stdout: string; stderr: string; exitCode: number }> {
    return new Promise((resolve, reject) => {
      const workingDir = cwd || vscode.workspace.workspaceFolders?.[0]?.uri.fsPath || process.cwd();

      // Determine shell based on platform
      const isWindows = process.platform === 'win32';
      const shell = isWindows ? 'cmd.exe' : '/bin/sh';
      const shellArg = isWindows ? '/c' : '-c';

      const proc = spawn(shell, [shellArg, command], {
        cwd: workingDir,
        env: { ...process.env, FORCE_COLOR: '0' }, // Disable color codes for cleaner output
        shell: false,
      });

      let stdout = '';
      let stderr = '';
      let timedOut = false;

      const timeoutId = setTimeout(() => {
        timedOut = true;
        proc.kill('SIGKILL');
      }, timeout || 120000);

      proc.stdout.on('data', (data: Buffer) => {
        stdout += data.toString('utf-8');
      });

      proc.stderr.on('data', (data: Buffer) => {
        stderr += data.toString('utf-8');
      });

      proc.on('error', (err) => {
        clearTimeout(timeoutId);
        reject(err);
      });

      proc.on('close', (code) => {
        clearTimeout(timeoutId);

        if (timedOut) {
          resolve({
            stdout,
            stderr: stderr + '\nCommand timed out',
            exitCode: -1,
          });
        } else {
          resolve({
            stdout,
            stderr,
            exitCode: code ?? 1,
          });
        }
      });
    });
  }

  /**
   * Read a document (PDF, CSV, Excel, etc.)
   */
  private async readDocument(args: { path: string; full?: boolean }): Promise<ToolExecutionResult> {
    const filePath = this.resolvePath(args.path);

    if (!fs.existsSync(filePath)) {
      return {
        success: false,
        content: `File not found: ${args.path}`,
        is_error: true,
      };
    }

    const ext = path.extname(filePath).toLowerCase();

    try {
      switch (ext) {
        case '.csv':
        case '.tsv':
          return this.readCSV(filePath, args.full);
        case '.xlsx':
        case '.xls':
          return this.readExcel(filePath, args.full);
        case '.pdf':
          return this.readPDF(filePath, args.full);
        case '.json':
          return this.readJSON(filePath, args.full);
        default:
          // Try to read as text
          const content = fs.readFileSync(filePath, 'utf-8');
          return {
            success: true,
            content: args.full ? content : `Document structure for ${args.path}:\n${content.slice(0, 2000)}...`,
            path: args.path,
          };
      }
    } catch (error) {
      return {
        success: false,
        content: `Error reading document: ${error instanceof Error ? error.message : String(error)}`,
        is_error: true,
      };
    }
  }

  /**
   * Read a CSV or TSV file
   */
  private async readCSV(filePath: string, full?: boolean): Promise<ToolExecutionResult> {
    // Dynamic import to avoid requiring the module at startup
    const { parse } = await import('csv-parse/sync');

    const content = fs.readFileSync(filePath, 'utf-8');
    const delimiter = filePath.endsWith('.tsv') ? '\t' : ',';

    try {
      const records: any[] = parse(content, {
        columns: true,
        skip_empty_lines: true,
        delimiter,
        relax_column_count: true,
      });

      if (records.length === 0) {
        return {
          success: true,
          content: 'Empty CSV file',
          path: filePath,
        };
      }

      const headers = Object.keys(records[0]);
      const rowCount = records.length;
      const columnCount = headers.length;

      if (full) {
        // Return full content as formatted text
        const headerLine = headers.join(' | ');
        const lines = [headerLine, '-'.repeat(headerLine.length)];
        for (const record of records) {
          lines.push(headers.map(h => String(record[h] ?? '')).join(' | '));
        }
        return {
          success: true,
          content: lines.join('\n'),
          path: filePath,
        };
      } else {
        // Return structure summary with sample rows
        const sampleSize = Math.min(5, rowCount);
        const sampleRows = records.slice(0, sampleSize);

        let summary = `CSV Structure for ${path.basename(filePath)}:\n`;
        summary += `Rows: ${rowCount}, Columns: ${columnCount}\n\n`;
        summary += `Headers: ${headers.join(', ')}\n\n`;
        summary += `Sample rows (first ${sampleSize}):\n`;

        for (let i = 0; i < sampleSize; i++) {
          summary += `\nRow ${i + 1}:\n`;
          for (const header of headers.slice(0, 10)) { // Limit columns in summary
            const value = sampleRows[i][header];
            const displayValue = String(value ?? '').slice(0, 100);
            summary += `  ${header}: ${displayValue}\n`;
          }
          if (headers.length > 10) {
            summary += `  ... and ${headers.length - 10} more columns\n`;
          }
        }

        if (rowCount > sampleSize) {
          summary += `\n... and ${rowCount - sampleSize} more rows. Use full=true to get all data.`;
        }

        return {
          success: true,
          content: summary,
          path: filePath,
        };
      }
    } catch (error) {
      return {
        success: false,
        content: `Error parsing CSV: ${error instanceof Error ? error.message : String(error)}`,
        is_error: true,
      };
    }
  }

  /**
   * Read an Excel file
   */
  private async readExcel(filePath: string, full?: boolean): Promise<ToolExecutionResult> {
    // Dynamic import to avoid requiring the module at startup
    const XLSX = await import('xlsx');

    const workbook = XLSX.readFile(filePath);
    const sheetNames = workbook.SheetNames;

    if (sheetNames.length === 0) {
      return {
        success: false,
        content: 'Excel file has no sheets',
        is_error: true,
      };
    }

    if (full) {
      // Return all sheets as formatted text
      let content = '';
      for (const sheetName of sheetNames) {
        const worksheet = workbook.Sheets[sheetName];
        const json: any[] = XLSX.utils.sheet_to_json(worksheet);
        content += `\n=== Sheet: ${sheetName} ===\n`;
        if (json.length > 0) {
          const headers = Object.keys(json[0]);
          content += headers.join(' | ') + '\n';
          content += '-'.repeat(headers.join(' | ').length) + '\n';
          for (const row of json) {
            content += headers.map(h => String(row[h] ?? '')).join(' | ') + '\n';
          }
        }
      }
      return {
        success: true,
        content: content.trim(),
        path: filePath,
      };
    } else {
      // Return structure summary
      let summary = `Excel Structure for ${path.basename(filePath)}:\n`;
      summary += `Sheets: ${sheetNames.join(', ')}\n\n`;

      for (const sheetName of sheetNames.slice(0, 5)) { // Limit to first 5 sheets
        const worksheet = workbook.Sheets[sheetName];
        const json: any[] = XLSX.utils.sheet_to_json(worksheet, { header: 1 });

        summary += `Sheet "${sheetName}":\n`;
        summary += `  Rows: ${json.length}`;

        if (json.length > 0 && Array.isArray(json[0])) {
          summary += `, Columns: ${json[0].length}`;
        }

        // Show first few rows as sample
        if (json.length > 0) {
          const headers = Array.isArray(json[0]) ? json[0] : Object.keys(json[0]);
          summary += `\n  Headers: ${headers.slice(0, 10).join(', ')}${headers.length > 10 ? '...' : ''}`;

          if (json.length > 1) {
            const sampleRow = Array.isArray(json[1]) ? json[1] : Object.values(json[1]);
            summary += `\n  Sample: ${sampleRow.slice(0, 5).map(v => String(v)).join(', ')}...`;
          }
        }
        summary += '\n\n';
      }

      if (sheetNames.length > 5) {
        summary += `... and ${sheetNames.length - 5} more sheets.\n`;
      }

      summary += '\nUse full=true to get complete data.';

      return {
        success: true,
        content: summary.trim(),
        path: filePath,
      };
    }
  }

  /**
   * Read a PDF file
   */
  private async readPDF(filePath: string, full?: boolean): Promise<ToolExecutionResult> {
    // Dynamic import to avoid requiring the module at startup
    const pdf = await import('pdf-parse');

    const dataBuffer = fs.readFileSync(filePath);

    try {
      const data = await pdf.default(dataBuffer);

      if (full) {
        return {
          success: true,
          content: data.text,
          path: filePath,
        };
      } else {
        let summary = `PDF Structure for ${path.basename(filePath)}:\n`;
        summary += `Pages: ${data.numpages}\n`;
        summary += `Info: ${JSON.stringify(data.info, null, 2)}\n\n`;
        summary += `Metadata:\n`;
        summary += `  Title: ${data.info?.Title || 'N/A'}\n`;
        summary += `  Author: ${data.info?.Author || 'N/A'}\n`;
        summary += `  Subject: ${data.info?.Subject || 'N/A'}\n`;
        summary += `  Creator: ${data.info?.Creator || 'N/A'}\n\n`;

        // Show first page or first 2000 chars
        const firstPageText = data.text.slice(0, 2000);
        summary += `First page preview:\n${firstPageText}`;

        if (data.text.length > 2000) {
          summary += `\n\n... and ${data.text.length - 2000} more characters. Use full=true to get all text.`;
        }

        return {
          success: true,
          content: summary,
          path: filePath,
        };
      }
    } catch (error) {
      return {
        success: false,
        content: `Error parsing PDF: ${error instanceof Error ? error.message : String(error)}. ` +
          'Note: Encrypted PDFs are not supported.',
        is_error: true,
      };
    }
  }

  /**
   * Read a JSON file
   */
  private readJSON(filePath: string, full?: boolean): ToolExecutionResult {
    const content = fs.readFileSync(filePath, 'utf-8');

    try {
      const data = JSON.parse(content);

      if (full) {
        return {
          success: true,
          content: typeof data === 'string' ? data : JSON.stringify(data, null, 2),
          path: filePath,
        };
      } else {
        let summary = `JSON Structure for ${path.basename(filePath)}:\n`;

        if (Array.isArray(data)) {
          summary += `Type: Array with ${data.length} items\n`;
          if (data.length > 0) {
            summary += `First item type: ${typeof data[0]}\n`;
            if (typeof data[0] === 'object' && data[0] !== null) {
              summary += `Keys: ${Object.keys(data[0]).join(', ')}\n`;
            }
            summary += `\nFirst item preview:\n${JSON.stringify(data[0], null, 2).slice(0, 500)}`;
          }
        } else if (typeof data === 'object' && data !== null) {
          const keys = Object.keys(data);
          summary += `Type: Object with ${keys.length} keys\n`;
          summary += `Keys: ${keys.slice(0, 20).join(', ')}${keys.length > 20 ? '...' : ''}\n\n`;

          // Show sample values
          summary += `Sample values:\n`;
          for (const key of keys.slice(0, 5)) {
            const value = data[key];
            const displayValue = typeof value === 'object'
              ? JSON.stringify(value).slice(0, 100)
              : String(value);
            summary += `  ${key}: ${displayValue}\n`;
          }
        } else {
          summary += `Type: ${typeof data}\n`;
          summary += `Value: ${String(data).slice(0, 500)}`;
        }

        return {
          success: true,
          content: summary,
          path: filePath,
        };
      }
    } catch (error) {
      return {
        success: false,
        content: `Error parsing JSON: ${error instanceof Error ? error.message : String(error)}`,
        is_error: true,
      };
    }
  }

  /**
   * Get git diff
   */
  private async gitDiff(args: { path?: string; summary?: boolean }): Promise<ToolExecutionResult> {
    const workspaceFolder = vscode.workspace.workspaceFolders?.[0];
    if (!workspaceFolder) {
      return {
        success: false,
        content: 'No workspace folder open',
        is_error: true,
      };
    }

    const workspacePath = workspaceFolder.uri.fsPath;

    // Check if this is a git repository
    const gitDir = path.join(workspacePath, '.git');
    if (!fs.existsSync(gitDir)) {
      return {
        success: false,
        content: 'Not a git repository. Run "git init" to enable diff tracking.',
        is_error: true,
      };
    }

    try {
      const gitArgs = ['diff'];
      if (args.summary) {
        gitArgs.push('--stat');
      }
      if (args.path) {
        gitArgs.push('--', args.path);
      }

      const result = await this.executeInTerminal(`git ${gitArgs.join(' ')}`, workspacePath, 30000);

      if (result.exitCode !== 0) {
        // Check if it's just "no changes"
        if (result.stderr.includes('not a git repository') || result.stderr.includes('outside repository')) {
          return {
            success: false,
            content: 'Error: Not a valid git repository',
            is_error: true,
          };
        }
        // No changes returns empty diff, which is fine
        if (result.stdout.trim() === '' && result.stderr.trim() === '') {
          return {
            success: true,
            content: 'No uncommitted changes.',
          };
        }
      }

      const output = (result.stdout + result.stderr).trim();

      if (!output) {
        return {
          success: true,
          content: 'No uncommitted changes.',
        };
      }

      // Truncate very large diffs
      const maxChars = 20000;
      if (output.length > maxChars) {
        return {
          success: true,
          content: output.slice(0, maxChars) + `\n\n... diff truncated at ${maxChars} characters. ` +
            'Call again with summary=true, or with a single path.',
        };
      }

      return {
        success: true,
        content: output,
      };
    } catch (error) {
      return {
        success: false,
        content: `Error running git diff: ${error instanceof Error ? error.message : String(error)}`,
        is_error: true,
      };
    }
  }

  /**
   * Create a unified diff
   */
  private createDiff(filePath: string, before: string, after: string): string {
    const beforeLines = before.split('\n');
    const afterLines = after.split('\n');

    let diff = `--- a/${filePath}\n+++ b/${filePath}\n`;

    // Simple line-by-line diff (production would use proper diff library)
    const maxLines = Math.max(beforeLines.length, afterLines.length);
    let lineNum = 1;

    for (let i = 0; i < maxLines; i++) {
      const beforeLine = beforeLines[i];
      const afterLine = afterLines[i];

      if (beforeLine !== afterLine) {
        if (beforeLine !== undefined) {
          diff += `- ${beforeLine}\n`;
        }
        if (afterLine !== undefined) {
          diff += `+ ${afterLine}\n`;
        }
      } else {
        diff += `  ${beforeLine}\n`;
      }
      lineNum++;
    }

    return diff;
  }

  /**
   * Resolve a path relative to the workspace
   */
  private resolvePath(filePath: string): string {
    if (path.isAbsolute(filePath)) {
      return filePath;
    }

    const workspaceFolder = vscode.workspace.workspaceFolders?.[0];
    if (workspaceFolder) {
      return path.join(workspaceFolder.uri.fsPath, filePath);
    }

    return filePath;
  }

  dispose(): void {
    this.outputChannel.dispose();
  }
}
