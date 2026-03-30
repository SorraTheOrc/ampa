#!/usr/bin/env node

/**
 * Installer Test Runner
 * 
 * Runs the manual test suite and reports results in a parseable format.
 * Can output JSON for CI/CD integration.
 * 
 * Usage:
 *   node tests/run-installer-tests.js [--json] [--verbose]
 */

import { execSync } from 'child_process';
import path from 'path';
import { fileURLToPath } from 'url';

const __dirname = path.dirname(fileURLToPath(import.meta.url));
const TEST_SCRIPT = path.join(__dirname, 'test-installer-manual.sh');

const args = process.argv.slice(2);
const outputJson = args.includes('--json');
const verbose = args.includes('--verbose');

// Colors for output
const colors = {
  reset: '\x1b[0m',
  red: '\x1b[31m',
  green: '\x1b[32m',
  yellow: '\x1b[33m',
};

function log(message, color = 'reset') {
  if (outputJson) return;
  console.log(`${colors[color]}${message}${colors.reset}`);
}

function runTests() {
  try {
    log('Running installer test suite...', 'yellow');
    
    let result;
    try {
      result = execSync(`sh ${TEST_SCRIPT}`, {
        encoding: 'utf-8',
        stdio: verbose ? 'inherit' : 'pipe',
      });
    } catch (e) {
      // Test script might exit with code 1 if tests failed, but output is still valid
      result = e.stdout || e.stderr || e.message;
    }

    // Parse test results from output
    const lines = (result || '').split('\n');
    const summary = {
      testsRun: 0,
      testsPassed: 0,
      testsFailed: 0,
      tests: [],
    };

    let currentSection = 'General';
    
    for (const line of lines) {
      // Parse test counts
      const runMatch = line.match(/Tests run:\s*(\d+)/);
      const passMatch = line.match(/Tests passed:\s*(\d+)/);
      const failMatch = line.match(/Tests failed:\s*(\d+)/);

      if (runMatch) summary.testsRun = parseInt(runMatch[1]);
      if (passMatch) summary.testsPassed = parseInt(passMatch[1]);
      if (failMatch) summary.testsFailed = parseInt(failMatch[1]);

      // Parse section headers
      const sectionMatch = line.match(/=== (.+?) ===/);
      if (sectionMatch) {
        currentSection = sectionMatch[1];
      }

      // Parse test results
      const testPassMatch = line.match(/✓ (.+)/);
      const testFailMatch = line.match(/✗ (.+)/);

      if (testPassMatch) {
        summary.tests.push({
          name: testPassMatch[1],
          status: 'pass',
          section: currentSection,
        });
      }

      if (testFailMatch) {
        summary.tests.push({
          name: testFailMatch[1],
          status: 'fail',
          section: currentSection,
        });
      }
    }

    return {
      success: summary.testsFailed === 0,
      summary,
      output: result,
    };
  } catch (error) {
    return {
      success: false,
      error: error.message,
      output: error.stdout ? error.stdout.toString() : '',
    };
  }
}

function formatJsonOutput(result) {
  return JSON.stringify(result, null, 2);
}

function formatTextOutput(result) {
  if (result.error) {
    return `Error running tests: ${result.error}`;
  }

  const { summary } = result;
  const header = `
╔════════════════════════════════════════╗
║  Ampa Plugin Installer Test Results    ║
╚════════════════════════════════════════╝`;

  const stats = `
Tests Run:     ${summary.testsRun}
Tests Passed:  ${colors.green}${summary.testsPassed}${colors.reset}
Tests Failed:  ${summary.testsFailed > 0 ? colors.red : colors.green}${summary.testsFailed}${colors.reset}`;

  const testsBySection = {};
  for (const test of summary.tests) {
    if (!testsBySection[test.section]) {
      testsBySection[test.section] = [];
    }
    testsBySection[test.section].push(test);
  }

  let testDetails = '';
  for (const [section, tests] of Object.entries(testsBySection)) {
    testDetails += `\n${colors.yellow}${section}:${colors.reset}\n`;
    for (const test of tests) {
      const symbol = test.status === 'pass' ? `${colors.green}✓${colors.reset}` : `${colors.red}✗${colors.reset}`;
      testDetails += `  ${symbol} ${test.name}\n`;
    }
  }

  const status = result.success
    ? `${colors.green}SUCCESS: All tests passed!${colors.reset}`
    : `${colors.red}FAILURE: ${summary.testsFailed} test(s) failed${colors.reset}`;

  return `${header}${stats}${testDetails}\n${status}\n`;
}

function main() {
  const result = runTests();

  if (outputJson) {
    console.log(formatJsonOutput(result));
  } else {
    console.log(formatTextOutput(result));
  }

  process.exit(result.success ? 0 : 1);
}

main();
