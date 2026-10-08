# Changelog

All notable changes to the LLM Assistant VS Code Extension will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.0.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

### Added
- **Terminal Output Capture**: Full stdout/stderr capture for shell commands using `child_process.spawn`
- **Git Diff Integration**: Real git diff support with `--stat` summary option
- **Document Parsing**:
  - CSV/TSV parsing with structure detection and sample rows
  - Excel (.xlsx, .xls) support with multi-sheet handling
  - PDF text extraction with metadata
  - JSON structure analysis
- **Error Handling**:
  - Custom error classes (`ApiError`, `ToolError`, `ConfigError`, `SessionError`)
  - User-friendly error messages with actionable guidance
  - Automatic retry on network errors and rate limiting
  - Timeout handling (60s default for API calls)
- **Testing Infrastructure**:
  - Jest unit tests for config and error handling
  - VS Code integration test runner
  - Test setup with mocked VS Code API
  - Coverage reporting
- **Development Tools**:
  - Comprehensive CONTRIBUTING.md guide
  - Debug launch configurations for extension, tests, and Jest
  - ESLint configuration
  - TypeScript strict mode

### Changed
- **Improved Error Messages**: API errors now provide actionable guidance (check API key, verify gateway is running)
- **Better Tool Execution**: Shell commands now properly capture output instead of placeholder text
- **Enhanced Document Reading**: Structured output for non-text files instead of raw content

### Fixed
- Terminal command execution now works cross-platform (Windows, macOS, Linux)
- Git diff properly handles repositories with no changes
- PDF parsing handles encrypted files gracefully

## [0.1.0] - 2024-01-01

### Added
- Initial release
- Chat panel with streaming responses
- Session management with persistence
- Tool execution framework:
  - `list_files` - List workspace files
  - `grep` - Search file contents
  - `read_file` - Read file contents
  - `edit_file` - Edit files with approval workflow
  - `write_file` - Create/overwrite files
  - `run` - Execute shell commands
- Edit approval workflow with diff preview
- Command approval workflow with destructive command warnings
- Configuration via VS Code settings
- OpenAI-compatible API client
