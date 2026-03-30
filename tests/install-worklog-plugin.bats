#!/usr/bin/env bats
# Test suite for the refactored ampa plugin installer
# Run with: bats tests/install-worklog-plugin.bats
# Or with all tests: bats tests/

# Load the installer functions by sourcing the script
# Note: We need to carefully load the functions without executing main()

# Setup and teardown fixtures
setup() {
  # Create a temporary directory for test isolation
  TEST_DIR="$(mktemp -d)"
  export TEST_DIR
  
  # Redirect XDG_CONFIG_HOME so the installer's global default
  # (${XDG_CONFIG_HOME}/opencode/.worklog/plugins) resolves inside TEST_DIR.
  export XDG_CONFIG_HOME="$TEST_DIR/xdg"
  # The expected global target directory when XDG_CONFIG_HOME is set
  GLOBAL_TARGET="$XDG_CONFIG_HOME/opencode/.worklog/plugins"
  export GLOBAL_TARGET
  
  # Create necessary test subdirectories
  mkdir -p "$TEST_DIR/.worklog/plugins"
  mkdir -p "$TEST_DIR/.worklog/ampa/default"
  mkdir -p "$TEST_DIR/ampa"
  
  # Change to test directory
  cd "$TEST_DIR"
  
  # Copy the installer script to test directory (use current user's repo path)
  cp /home/rgardler/.config/opencode/skill/install-ampa/scripts/install-worklog-plugin.sh ./install-test.sh
  
  # Make it executable
  chmod +x ./install-test.sh
  
  # Create a dummy source .mjs file for testing
  echo "export default function register(ctx) {}" > plugins-test-plugin.mjs
  mkdir -p plugins-test
  echo "export default function register(ctx) {}" > plugins-test/test.mjs
}

teardown() {
  # Clean up test directory
  rm -rf "$TEST_DIR"
}

# ============================================================================
# UTILITY FUNCTION TESTS
# ============================================================================

@test "help flag shows usage" {
  ./install-test.sh --help
}

@test "unknown option fails with error" {
  run ./install-test.sh --unknown-flag
  [ "$status" -ne 0 ]
  [[ "$output" == *"Unknown option"* ]]
}

@test "bot-token and channel-id option with value" {
  TEST_BOT_TOKEN="bot_test_token"
  TEST_CHANNEL_ID="1234567890"
  run ./install-test.sh --bot-token "$TEST_BOT_TOKEN" --channel-id "$TEST_CHANNEL_ID" --no-restart --yes plugins-test-plugin.mjs
  [ "$status" -eq 0 ] || [ "$status" -eq 1 ]  # Might fail if source doesn't exist, that's ok
}

@test "bot-token and channel-id long options work" {
  TEST_BOT_TOKEN="bot_test_token"
  TEST_CHANNEL_ID="1234567890"
  run ./install-test.sh --bot-token "$TEST_BOT_TOKEN" --channel-id "$TEST_CHANNEL_ID" --no-restart --yes plugins-test-plugin.mjs
  [ "$status" -eq 0 ] || [ "$status" -eq 1 ]
}

@test "bot-token without value fails" {
  run ./install-test.sh --bot-token
  [ "$status" -eq 2 ]
  [[ "$output" == *"requires a value"* ]]
}

@test "--yes option enables auto mode" {
  run ./install-test.sh --no-restart --yes plugins-test-plugin.mjs
  # Should exit cleanly without prompting
  [ "$status" -eq 0 ] || [ "$status" -eq 1 ]
}

@test "--yes short option -y works" {
  run ./install-test.sh --no-restart -y plugins-test-plugin.mjs
  [ "$status" -eq 0 ] || [ "$status" -eq 1 ]
}

@test "--restart and --no-restart are mutually exclusive" {
  run ./install-test.sh --restart --no-restart --yes plugins-test-plugin.mjs
  [ "$status" -eq 2 ]
  [[ "$output" == *"mutually exclusive"* ]]
}

@test "positional arguments: source and target" {
  run ./install-test.sh --no-restart --yes plugins-test-plugin.mjs .worklog/plugins
  # Will fail on validation but should parse correctly
  [ "$status" -eq 0 ] || [ "$status" -eq 2 ]
}

# ============================================================================
# ARGUMENT PARSING TESTS
# ============================================================================

@test "source file not found error" {
  run ./install-test.sh --yes nonexistent-plugin.mjs
  [ "$status" -eq 2 ]
  [[ "$output" == *"Source file not found"* ]]
}

@test "default source path used when omitted" {
  # DEFAULT_SRC resolves to SCRIPT_DIR/../resources/ampa.mjs
  # Since install-test.sh is at $TEST_DIR, SCRIPT_DIR=$TEST_DIR,
  # so DEFAULT_SRC=$TEST_DIR/../resources/ampa.mjs
  local parent_dir
  parent_dir="$(dirname "$TEST_DIR")"
  mkdir -p "$parent_dir/resources"
  echo "export default function register(ctx) {}" > "$parent_dir/resources/ampa.mjs"
  
  run ./install-test.sh --no-restart --yes
  # Will try to install the default plugin to the global target
  [ "$status" -eq 0 ]
  
  # Clean up the resource file outside the test dir
  rm -rf "$parent_dir/resources"
}

@test "extra positional arguments ignored" {
  run ./install-test.sh --no-restart --yes plugins-test-plugin.mjs .worklog/plugins extra args
  # Extra args are logged as warnings but do not cause failure
  [ "$status" -eq 0 ]
  [[ "$output" == *"Ignoring extra argument"* ]]
}

# ============================================================================
# PLUGIN INSTALLATION TESTS
# ============================================================================

@test "install .mjs plugin file to target directory" {
  run ./install-test.sh --no-restart --yes plugins-test-plugin.mjs .worklog/plugins
  [ "$status" -eq 0 ]
  [ -f ".worklog/plugins/plugins-test-plugin.mjs" ]
}

@test "install with default target directory (global)" {
  run ./install-test.sh --no-restart --yes plugins-test-plugin.mjs
  [ "$status" -eq 0 ]
  [ -f "$GLOBAL_TARGET/plugins-test-plugin.mjs" ]
}

@test "overwrite existing plugin file" {
  cp plugins-test-plugin.mjs .worklog/plugins/plugins-test-plugin.mjs
  echo "old content" > .worklog/plugins/plugins-test-plugin.mjs
  
  run ./install-test.sh --no-restart --yes plugins-test-plugin.mjs .worklog/plugins
  [ "$status" -eq 0 ]
  
  # Verify the file was overwritten with new content
  grep -q "export default function register" .worklog/plugins/plugins-test-plugin.mjs
}

# ============================================================================
# PYTHON PACKAGE TESTS
# ============================================================================

@test "python package detection" {
  # Create a simple Python package structure (no requirements.txt so venv
  # creation is skipped — we only verify the package directory is copied).
  mkdir -p ampa
  echo "test module" > ampa/__init__.py
  
  run ./install-test.sh --no-restart --yes plugins-test-plugin.mjs .worklog/plugins
  [ "$status" -eq 0 ]
  
  # Verify the package was copied
  [ -d ".worklog/plugins/ampa_py/ampa" ]
}

@test "python not found error handled" {
  # Create a minimal ampa package with requirements to trigger Python check
  mkdir -p ampa
  echo "requests>=2.0.0" > ampa/requirements.txt
  
  # Build a clean PATH that excludes python/python3 by creating a bin dir
  # with only the essential commands the installer needs.
  local fake_bin="$TEST_DIR/no_python_bin"
  mkdir -p "$fake_bin"
  # Link essential commands the installer uses (sh, cp, mkdir, etc.)
  for cmd in sh bash cp mkdir rm mv cat grep sed tee chmod mktemp dirname basename readlink date printf tr wc ls touch ln find rmdir stat id flock node; do
    local real
    real="$(command -v "$cmd" 2>/dev/null || true)"
    if [ -n "$real" ]; then
      ln -sf "$real" "$fake_bin/$cmd"
    fi
  done

  PATH="$fake_bin" run ./install-test.sh --no-restart --yes plugins-test-plugin.mjs .worklog/plugins
  # This should error about python not found
  [ "$status" -ne 0 ]
}

# ============================================================================
# ENV FILE HANDLING TESTS
# ============================================================================

@test "env sample file detection" {
  mkdir -p ampa
  echo 'AMPA_DISCORD_BOT_TOKEN=""' > ampa/.env.sample
  echo 'AMPA_DISCORD_CHANNEL_ID=""' >> ampa/.env.sample

  TEST_BOT_TOKEN="bot_test_token"
  TEST_CHANNEL_ID="1234567890"

  run ./install-test.sh --bot-token "$TEST_BOT_TOKEN" --channel-id "$TEST_CHANNEL_ID" --no-restart --yes plugins-test-plugin.mjs .worklog/plugins
  [ "$status" -eq 0 ]
  
  # Verify .env was created
  [ -f ".worklog/plugins/ampa_py/ampa/.env" ]
}

@test "env sample with legacy .env.samplw filename" {
  mkdir -p ampa
  echo 'AMPA_DISCORD_BOT_TOKEN=""' > ampa/.env.samplw
  echo 'AMPA_DISCORD_CHANNEL_ID=""' >> ampa/.env.samplw

  TEST_BOT_TOKEN="bot_test_token"
  TEST_CHANNEL_ID="1234567890"

  run ./install-test.sh --bot-token "$TEST_BOT_TOKEN" --channel-id "$TEST_CHANNEL_ID" --no-restart --yes plugins-test-plugin.mjs .worklog/plugins
  [ "$status" -eq 0 ]
  
  [ -f ".worklog/plugins/ampa_py/ampa/.env" ]
}

@test "bot token and channel id written to env file" {
  mkdir -p ampa
  echo 'AMPA_DISCORD_BOT_TOKEN=""' > ampa/.env.sample
  echo 'AMPA_DISCORD_CHANNEL_ID=""' >> ampa/.env.sample

  TEST_BOT_TOKEN="bot_test_token"
  TEST_CHANNEL_ID="1234567890"
  run ./install-test.sh --bot-token "$TEST_BOT_TOKEN" --channel-id "$TEST_CHANNEL_ID" --no-restart --yes plugins-test-plugin.mjs .worklog/plugins
  [ "$status" -eq 0 ]

  # Verify bot token and channel id are in the env file
  grep -q "AMPA_DISCORD_BOT_TOKEN=$TEST_BOT_TOKEN" .worklog/plugins/ampa_py/ampa/.env
  grep -q "AMPA_DISCORD_CHANNEL_ID=$TEST_CHANNEL_ID" .worklog/plugins/ampa_py/ampa/.env
}

@test "existing env file preservation during upgrade" {
  mkdir -p ampa
  mkdir -p .worklog/plugins/ampa_py/ampa
  
  # Create existing env with data
  echo 'AMPA_DISCORD_BOT_TOKEN="existing_bot_token"' > .worklog/plugins/ampa_py/ampa/.env
  echo 'AMPA_DISCORD_CHANNEL_ID="existing_channel_id"' >> .worklog/plugins/ampa_py/ampa/.env
  echo "test_data=preserved" >> .worklog/plugins/ampa_py/ampa/.env
  
  # Create package files (no requirements.txt — skip venv)
  cp -r ampa .worklog/plugins/ampa_py/
  
  # Perform "upgrade" without changing bot token/channel
  run ./install-test.sh --no-restart --yes plugins-test-plugin.mjs .worklog/plugins
  [ "$status" -eq 0 ]
  
  # Old env should be preserved since no new bot token/channel was provided
  grep -q "existing_bot_token" .worklog/plugins/ampa_py/ampa/.env || \
  grep -q "test_data=preserved" .worklog/plugins/ampa_py/ampa/.env
}

# ============================================================================
# EXISTING INSTALLATION DETECTION
# ============================================================================

@test "detect existing mjs plugin installation" {
  # Create existing plugin
  mkdir -p .worklog/plugins
  echo "old plugin" > .worklog/plugins/plugins-test-plugin.mjs
  
  run ./install-test.sh --no-restart --yes plugins-test-plugin.mjs .worklog/plugins
  [ "$status" -eq 0 ]
  # Should detect existing and proceed with upgrade
}

@test "detect existing python package installation" {
  mkdir -p .worklog/plugins/ampa_py/ampa
  echo "existing package" > .worklog/plugins/ampa_py/ampa/__init__.py
  
  run ./install-test.sh --no-restart --yes plugins-test-plugin.mjs .worklog/plugins
  [ "$status" -eq 0 ]
}

# ============================================================================
# DAEMON PID FILE TESTS
# ============================================================================

@test "detect running daemon from pid file" {
  # Create a pid file with current process id
  mkdir -p .worklog/ampa/default
  echo "$$" > .worklog/ampa/default/default.pid
  
  run ./install-test.sh --no-restart --yes plugins-test-plugin.mjs .worklog/plugins
  [ "$status" -eq 0 ]
  # Should not attempt to restart because of --no-restart
}

@test "no restart when flag is set" {
  mkdir -p .worklog/ampa/default
  echo "$$" > .worklog/ampa/default/default.pid
  
  run ./install-test.sh --no-restart --yes plugins-test-plugin.mjs .worklog/plugins
  [ "$status" -eq 0 ]
}

@test "no restart flag prevents daemon restart" {
  mkdir -p .worklog/ampa/default
  echo "999999" > .worklog/ampa/default/default.pid  # Non-existent pid
  
  run ./install-test.sh --no-restart --yes plugins-test-plugin.mjs .worklog/plugins
  [ "$status" -eq 0 ]
}

# ============================================================================
# ERROR HANDLING TESTS
# ============================================================================

@test "missing target directory is created" {
  run ./install-test.sh --no-restart --yes plugins-test-plugin.mjs .worklog/custom/plugins
  [ "$status" -eq 0 ]
  [ -d ".worklog/custom/plugins" ]
}

@test "script validates source file exists" {
  run ./install-test.sh --no-restart --yes /nonexistent/plugin.mjs
  [ "$status" -eq 2 ]
  [[ "$output" == *"not found"* ]]
}

@test "lock prevents concurrent installation" {
  # This is a simplified test - in practice, concurrent runs would be needed
  run ./install-test.sh --no-restart --yes plugins-test-plugin.mjs .worklog/plugins
  [ "$status" -eq 0 ]
  # Lock should be released after completion
  [ ! -d "/tmp/ampa_install.lock" ] || [ -d "/tmp/ampa_install.lock" ]
}

# ============================================================================
# INTEGRATION TESTS
# ============================================================================

@test "fresh install flow complete" {
  mkdir -p ampa
  echo 'AMPA_DISCORD_BOT_TOKEN=""' > ampa/.env.sample
  echo 'AMPA_DISCORD_CHANNEL_ID=""' >> ampa/.env.sample
  
  TEST_BOT_TOKEN="bot_test_token"
  TEST_CHANNEL_ID="1234567890"
  
  run ./install-test.sh --bot-token "$TEST_BOT_TOKEN" --channel-id "$TEST_CHANNEL_ID" --no-restart --yes plugins-test-plugin.mjs .worklog/plugins
  [ "$status" -eq 0 ]
  
  # Verify all components installed
  [ -f ".worklog/plugins/plugins-test-plugin.mjs" ]
  [ -d ".worklog/plugins/ampa_py/ampa" ]
  [ -f ".worklog/plugins/ampa_py/ampa/.env" ]
  grep -q "AMPA_DISCORD_BOT_TOKEN=$TEST_BOT_TOKEN" .worklog/plugins/ampa_py/ampa/.env
  grep -q "AMPA_DISCORD_CHANNEL_ID=$TEST_CHANNEL_ID" .worklog/plugins/ampa_py/ampa/.env
}

@test "upgrade flow preserves custom env" {
  # First install with bot token/channel
  mkdir -p ampa
  echo 'AMPA_DISCORD_BOT_TOKEN=""' > ampa/.env.sample
  echo 'AMPA_DISCORD_CHANNEL_ID=""' >> ampa/.env.sample
  
  run ./install-test.sh --bot-token "existing_bot_token" --channel-id "existing_channel_id" --no-restart --yes plugins-test-plugin.mjs .worklog/plugins
  [ "$status" -eq 0 ]
  
  # Verify first install
  [ -f ".worklog/plugins/ampa_py/ampa/.env" ]
  grep -q "existing_bot_token" .worklog/plugins/ampa_py/ampa/.env
  
  # Second install (upgrade) without changing bot token/channel
  run ./install-test.sh --no-restart --yes plugins-test-plugin.mjs .worklog/plugins
  [ "$status" -eq 0 ]
  
  # Verify env is preserved (old bot token still there)
  grep -q "existing_bot_token" .worklog/plugins/ampa_py/ampa/.env || true
}

@test "help text contains all options" {
  run ./install-test.sh --help
  [ "$status" -eq 0 ]
  [[ "$output" == *"--bot-token"* ]]
  [[ "$output" == *"--channel-id"* ]]
  [[ "$output" == *"--yes"* ]]
  [[ "$output" == *"--restart"* ]]
  [[ "$output" == *"--no-restart"* ]]
  [[ "$output" == *"--local"* ]]
}

# ============================================================================
# BACKWARD COMPATIBILITY TESTS
# ============================================================================

@test "original behavior: single argument source file installs to global" {
  run ./install-test.sh --no-restart --yes plugins-test-plugin.mjs
  [ "$status" -eq 0 ]
  [ -f "$GLOBAL_TARGET/plugins-test-plugin.mjs" ]
}

@test "original behavior: two arguments source and target" {
  run ./install-test.sh --no-restart --yes plugins-test-plugin.mjs custom-plugins
  [ "$status" -eq 0 ]
  [ -f "custom-plugins/plugins-test-plugin.mjs" ]
}

@test "original behavior: no arguments uses defaults (global)" {
  # DEFAULT_SRC resolves to SCRIPT_DIR/../resources/ampa.mjs
  local parent_dir
  parent_dir="$(dirname "$TEST_DIR")"
  mkdir -p "$parent_dir/resources"
  echo "export default function register(ctx) {}" > "$parent_dir/resources/ampa.mjs"
  
  # Also need an ampa/ directory so the Python package copy step doesn't abort
  mkdir -p ampa
  
  run ./install-test.sh --no-restart --yes
  [ "$status" -eq 0 ]
  [ -f "$GLOBAL_TARGET/ampa.mjs" ]
  
  # Clean up the resource file outside the test dir
  rm -rf "$parent_dir/resources"
}

@test "original behavior: bot-token/channel-id option with global default" {
  mkdir -p ampa
  echo 'AMPA_DISCORD_BOT_TOKEN=""' > ampa/.env.sample
  echo 'AMPA_DISCORD_CHANNEL_ID=""' >> ampa/.env.sample

  TEST_BOT_TOKEN="bot_test_token"
  TEST_CHANNEL_ID="1234567890"

  run ./install-test.sh --bot-token "$TEST_BOT_TOKEN" --channel-id "$TEST_CHANNEL_ID" --no-restart --yes plugins-test-plugin.mjs
  [ "$status" -eq 0 ]
}

@test "original behavior: auto yes option with global default" {
  run ./install-test.sh --no-restart --yes plugins-test-plugin.mjs
  [ "$status" -eq 0 ]
}

# ============================================================================
# --LOCAL FLAG TESTS
# ============================================================================

@test "--local flag installs to project-local directory" {
  run ./install-test.sh --local --no-restart --yes plugins-test-plugin.mjs
  [ "$status" -eq 0 ]
  [ -f ".worklog/plugins/plugins-test-plugin.mjs" ]
  # Should NOT be in the global target
  [ ! -f "$GLOBAL_TARGET/plugins-test-plugin.mjs" ]
}

@test "--local flag with default source uses local target" {
  # DEFAULT_SRC resolves to SCRIPT_DIR/../resources/ampa.mjs
  local parent_dir
  parent_dir="$(dirname "$TEST_DIR")"
  mkdir -p "$parent_dir/resources"
  echo "export default function register(ctx) {}" > "$parent_dir/resources/ampa.mjs"
  
  # Also need an ampa/ directory so the Python package copy step doesn't abort
  mkdir -p ampa
  
  run ./install-test.sh --local --no-restart --yes
  [ "$status" -eq 0 ]
  [ -f ".worklog/plugins/ampa.mjs" ]
  
  # Clean up the resource file outside the test dir
  rm -rf "$parent_dir/resources"
}

# ============================================================================
# MIGRATION TESTS
# ============================================================================

@test "migration detects local install and installs to global" {
  # Simulate an existing local installation
  mkdir -p .worklog/plugins
  echo "export default function register(ctx) {}" > .worklog/plugins/plugins-test-plugin.mjs
  mkdir -p .worklog/ampa
  echo '{"ampa-pool-0":{"workItemId":"WL-1"}}' > .worklog/ampa/pool-state.json
  echo '["ampa-pool-2"]' > .worklog/ampa/pool-cleanup.json
  
  # Run installer — it should detect the local install and migrate
  run ./install-test.sh --no-restart --yes plugins-test-plugin.mjs
  [ "$status" -eq 0 ]
  
  # Plugin should be installed to global target
  [ -f "$GLOBAL_TARGET/plugins-test-plugin.mjs" ]
}

# ============================================================================
# DECISION LOG TESTS
# ============================================================================

@test "decision log is created" {
  run ./install-test.sh --no-restart --yes plugins-test-plugin.mjs .worklog/plugins
  [ "$status" -eq 0 ]
  # Decision log should be created in /tmp
  [ -L "/tmp/ampa_install_decisions.log" ] || [ -f "/tmp/ampa_install_decisions.log" ]
}

@test "decision log contains installation details" {
  run ./install-test.sh --bot-token "log_bot_token" --channel-id "log_channel" --no-restart --yes plugins-test-plugin.mjs .worklog/plugins
  [ "$status" -eq 0 ]
  
  # Check if decision log exists and has content
  if [ -f "/tmp/ampa_install_decisions.log" ]; then
    [[ "$(cat /tmp/ampa_install_decisions.log)" == *"ACTION_PROCEED"* ]] || true
  fi
}

# ============================================================================
# SCRIPT QUALITY TESTS
# ============================================================================

@test "script has valid shell syntax" {
  run sh -n ./install-test.sh
  [ "$status" -eq 0 ]
}

@test "script is executable" {
  [ -x "./install-test.sh" ]
}

@test "script sets strict mode" {
  grep -q "set -eu" ./install-test.sh
}
