/**
 * Jest test setup
 */

// Mock vscode module
jest.mock('vscode', () => {
  return {
    workspace: {
      getConfiguration: jest.fn(),
      workspaceFolders: [],
      findFilesSync: jest.fn(),
      openTextDocument: jest.fn(),
      window: {
        showTextDocument: jest.fn(),
        showInformationMessage: jest.fn(),
        showErrorMessage: jest.fn(),
        showWarningMessage: jest.fn(),
        createTerminal: jest.fn(),
        createOutputChannel: jest.fn(),
      },
    },
    window: {
      showInformationMessage: jest.fn(),
      showErrorMessage: jest.fn(),
      showWarningMessage: jest.fn(),
      createTerminal: jest.fn(),
      createOutputChannel: jest.fn(),
    },
    ViewColumn: {
      Beside: 2,
      One: 1,
    },
    ConfigurationTarget: {
      Global: 1,
      Workspace: 2,
      WorkspaceFolder: 3,
    },
    Uri: {
      parse: jest.fn((path: string) => ({
        fsPath: path,
        toString: () => path,
      })),
      file: jest.fn((path: string) => ({
        fsPath: path,
        toString: () => `file://${path}`,
      })),
    },
  };
});

// Mock fs module
jest.mock('fs', () => {
  const actualFs = jest.requireActual('fs');
  return {
    ...actualFs,
    existsSync: jest.fn(),
    readFileSync: jest.fn(),
    writeFileSync: jest.fn(),
    mkdirSync: jest.fn(),
    chmodSync: jest.fn(),
    unlinkSync: jest.fn(),
  };
});

// Mock child_process
jest.mock('child_process', () => ({
  spawn: jest.fn(),
}));
