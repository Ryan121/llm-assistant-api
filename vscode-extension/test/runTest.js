/**
 * Integration test runner for VS Code extension
 */

const path = require('path');
const { runTests } = require('@vscode/test-electron');

async function main() {
  try {
    // Path to the extension source
    const extensionDevelopmentPath = path.resolve(__dirname, '..');

    // Path to the test suite
    const extensionTestsPath = path.resolve(__dirname, './suite/index');

    // Download VS Code, unzip it, and run the integration test
    await runTests({
      extensionDevelopmentPath,
      extensionTestsPath,
      launchArgs: ['--disable-extensions'], // Disable other extensions during tests
    });
  } catch (err) {
    console.error('Failed to run tests:', err);
    process.exit(1);
  }
}

main();
