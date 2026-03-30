# Ampa Plugin Installer Test Suite

This directory contains comprehensive tests for the refactored ampa plugin installer script (`skill/install-ampa/scripts/install-worklog-plugin.sh`).

## Test Files

### `tests/test-installer-manual.sh`

A standalone shell script test suite that doesn't require any external dependencies. This is the primary test file and can be run on any POSIX-compliant shell.

**Features:**
- 22 comprehensive tests covering all major functionality
- Tests argument parsing, plugin installation, environment file handling, Python setup, and more
- Colorized output for easy reading
- Detailed failure information
- Can be run without any test framework dependencies

**Run with:**
```bash
sh tests/test-installer-manual.sh
```

**Expected output:**
```
=== Ampa Plugin Installer Test Suite ===
...
=== Test Summary ===
Tests run:    22
Tests passed: 22
Tests failed: 0

All tests passed!
```

### `tests/install-worklog-plugin.bats`

An optional BATS (Bash Automated Testing System) test suite for users with bats installed. Provides additional test structure and integration with CI/CD systems.

**Requirements:**
- `bats` (Bash Automated Testing System)
- Install with: `npm install -g bats` or `apt-get install bats`

**Run with:**
```bash
bats tests/install-worklog-plugin.bats
```

## Test Coverage

The test suite covers the following areas:

### Argument Parsing
- Help flag (`--help`, `-h`)
- Bot token option (`--bot-token`) and channel ID option (`--channel-id`) with and without values
- Auto-yes option (`--yes`, `-y`)
- Restart options (`--restart`, `--no-restart`)
- Mutually exclusive option validation
- Unknown option error handling
- Positional arguments (source file, target directory)

### Plugin Installation
- Install .mjs plugin file to target directory
- Default target directory (`.worklog/plugins`)
- Overwrite existing plugin files
- Create target directory if it doesn't exist

### Python Package Installation
- Detect Python package in source (`ampa/` directory)
- Copy Python package to `.worklog/plugins/ampa_py/ampa`
- Install Python dependencies via pip
- Handle missing Python executable error
- Create and verify virtual environment

### Environment File Handling
- Detect `.env.sample` and `.env.samplw` files
- Create `.env` from sample template
- Write bot token and channel ID to `.env` file
- Preserve existing `.env` during upgrade
- Handle bot token removal
- Backup and restore `.env` files

### Installation Flow
- Detect existing installations (both .mjs and Python package)
- Prompt for upgrade vs. abort (in interactive mode)
- Handle daemon PID files
- Support `--no-restart` option to prevent daemon restart
- Create decision logs for debugging

### Backward Compatibility
- Single argument: source file only
- Two arguments: source and target directory
- No arguments: use default paths
- Original bot token option behavior
- Original auto-yes behavior

### Script Quality
- Valid shell syntax (passes `sh -n`)
- Executable permissions
- Strict mode (`set -eu`)
- Help text completeness
- Decision log creation

## Individual Test Details

### Argument Parsing Tests (6 tests)

```bash
✓ Help flag shows usage
✓ Unknown option fails with error
✓ Bot token option parsing
✓ Channel ID option parsing
✓ Bot token without value fails
✓ Mutually exclusive options error
```

### Source File Tests (2 tests)

```bash
✓ Source file not found error
✓ Default source path used
```

### Plugin Installation Tests (2 tests)

```bash
✓ Install .mjs plugin file
✓ Overwrite existing plugin file
```

### Python Package Tests (1 test)

```bash
✓ Python package installation
```

### Environment File Tests (2 tests)

```bash
✓ Env sample file detection
✓ Bot token written to env file
```

### Installation Flow Tests (3 tests)

```bash
✓ Existing installation detection
✓ PID file handling
✓ Target directory creation
```

### Script Quality Tests (3 tests)

```bash
✓ Script has valid shell syntax
✓ Script sets strict mode
✓ Help text contains all options
```

### Backward Compatibility Tests (2 tests)

```bash
✓ Backward compat: single argument source
✓ Backward compat: source and target
```

### Logging Tests (1 test)

```bash
✓ Decision log creation
```

## Integration with CI/CD

### GitHub Actions

Add to `.github/workflows/test.yml`:

```yaml
- name: Test Installer
  run: sh tests/test-installer-manual.sh
```

### GitLab CI

Add to `.gitlab-ci.yml`:

```yaml
test:installer:
  script:
    - sh tests/test-installer-manual.sh
```

### Local Development

Run before committing:

```bash
sh tests/test-installer-manual.sh
```

## Troubleshooting

### Tests fail with "python not found"

Some tests require Python to be installed. If Python is not in your PATH:
```bash
export PATH="/usr/bin:$PATH"
sh tests/test-installer-manual.sh
```

### Lock file prevents tests from running

If you get "Another ampa install appears to be running":
```bash
rm -rf /tmp/ampa_install.lock
```

### Tests fail in CI environment

Ensure the test environment has:
- `sh` (POSIX shell)
- `mkdir`, `cp`, `rm`, `grep` commands
- Write permissions to `/tmp`

## Test Structure

Each test follows this pattern:

1. **Setup**: Create temporary test directories and files
2. **Execution**: Run the installer script with specific arguments
3. **Verification**: Check that output and file system match expectations
4. **Cleanup**: Remove temporary test files

The manual test script handles setup/cleanup automatically. BATS tests use fixtures for the same purpose.

## Adding New Tests

To add a new test to `tests/test-installer-manual.sh`:

```bash
# Test: Description of what you're testing
test_new_feature() {
  if ./install-test.sh [arguments] 2>&1 > /dev/null; then
    if [ -f "expected-file" ]; then
      test_result "Test name" "pass"
    else
      test_result "Test name" "fail" "File not created"
    fi
  else
    test_result "Test name" "fail" "Installation failed"
  fi
}
```

Then add a call in the `main()` function:

```bash
print_section "New Test Category"
test_new_feature
```

## Performance

The manual test suite runs in approximately 5-10 seconds depending on system performance.

## See Also

- `skill/install-ampa/scripts/install-worklog-plugin.sh` - The canonical installer script
- `skill/install-ampa/resources/ampa.mjs` - Canonical AMPA plugin source
- `ampa/README.md` - AMPA daemon documentation
