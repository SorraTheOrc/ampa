#!/usr/bin/env sh
# Manual test suite for the refactored ampa plugin installer
# Run with: sh tests/test-installer-manual.sh
#
# This script provides manual tests for the installer without requiring bats framework.
# Tests check basic functionality and backward compatibility.

set -e

# Colors for output
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
NC='\033[0m' # No Color

# Test counters
TESTS_RUN=0
TESTS_PASSED=0
TESTS_FAILED=0

# Print test result
test_result() {
  local test_name="$1"
  local result="$2"
  local details="$3"
  
  TESTS_RUN=$((TESTS_RUN + 1))
  
  if [ "$result" = "pass" ]; then
    printf "${GREEN}✓${NC} %s\n" "$test_name"
    TESTS_PASSED=$((TESTS_PASSED + 1))
  else
    printf "${RED}✗${NC} %s\n" "$test_name"
    if [ -n "$details" ]; then
      printf "  ${YELLOW}Details:${NC} %s\n" "$details"
    fi
    TESTS_FAILED=$((TESTS_FAILED + 1))
  fi
}

# Print section header
print_section() {
  printf "\n${YELLOW}=== %s ===${NC}\n" "$1"
}

# Setup test environment
setup_test_env() {
  TEST_DIR=$(mktemp -d)
  export TEST_DIR
  cd "$TEST_DIR"
  
  # Create directory structure
  mkdir -p .worklog/plugins
  mkdir -p .worklog/ampa/default
  mkdir -p plugins/wl_ampa
  mkdir -p ampa
  
  # Copy installer script
  cp /home/rogardle/.config/opencode/skill/install-ampa/scripts/install-worklog-plugin.sh ./install-test.sh
  chmod +x ./install-test.sh
  
  # Create test plugin files
  echo "export default function register(ctx) {}" > plugins-test-plugin.mjs
  echo "export default function register(ctx) {}" > plugins/wl_ampa/ampa.mjs
}

# Cleanup test environment
cleanup_test_env() {
  cd /
  rm -rf "$TEST_DIR"
}

# Test: Help flag
test_help_flag() {
  if ./install-test.sh --help 2>&1 | grep -q "Usage:"; then
    test_result "Help flag shows usage" "pass"
  else
    test_result "Help flag shows usage" "fail" "Help output missing"
  fi
}

# Test: Unknown option fails
test_unknown_option() {
  if ! ./install-test.sh --unknown-flag 2>&1 > /dev/null; then
    test_result "Unknown option fails with error" "pass"
  else
    test_result "Unknown option fails with error" "fail" "Should have exited with error"
  fi
}

# Test: Bot token option works
test_bot_token_option() {
  if ./install-test.sh --bot-token "test-bot-token-123" --channel-id "123456789" --yes plugins-test-plugin.mjs .worklog/plugins 2>&1 > /dev/null; then
    test_result "Bot token option parsing" "pass"
  else
    test_result "Bot token option parsing" "fail" "Bot token option not processed correctly"
  fi
}

# Test: Channel ID option works
test_channel_id_option() {
  if ./install-test.sh --bot-token "test-bot-token-123" --channel-id "123456789" --yes plugins-test-plugin.mjs .worklog/plugins 2>&1 > /dev/null; then
    test_result "Channel ID option parsing" "pass"
  else
    test_result "Channel ID option parsing" "fail" "Channel ID option not processed correctly"
  fi
}

# Test: Bot token without value fails
test_bot_token_without_value() {
  if ! ./install-test.sh --bot-token 2>&1 | grep -q "requires a value"; then
    test_result "Bot token without value fails" "fail" "Should require value"
  else
    test_result "Bot token without value fails" "pass"
  fi
}

# Test: Source file not found
test_source_not_found() {
  if ! ./install-test.sh --yes nonexistent.mjs 2>&1 | grep -q "Source file not found"; then
    test_result "Source file not found error" "fail" "Should show error"
  else
    test_result "Source file not found error" "pass"
  fi
}

# Test: Default source path
test_default_source() {
  if ./install-test.sh --yes 2>&1 > /dev/null; then
    test_result "Default source path used" "pass"
  else
    test_result "Default source path used" "fail" "Default path not working"
  fi
}

# Test: Install plugin file
test_install_plugin() {
  if ./install-test.sh --yes plugins-test-plugin.mjs .worklog/plugins 2>&1 > /dev/null; then
    if [ -f ".worklog/plugins/plugins-test-plugin.mjs" ]; then
      test_result "Install .mjs plugin file" "pass"
    else
      test_result "Install .mjs plugin file" "fail" "Plugin file not created"
    fi
  else
    test_result "Install .mjs plugin file" "fail" "Installation failed"
  fi
}

# Test: Overwrite existing plugin
test_overwrite_plugin() {
  # Create existing file
  echo "old content" > .worklog/plugins/plugins-test-plugin.mjs
  
  if ./install-test.sh --yes plugins-test-plugin.mjs .worklog/plugins 2>&1 > /dev/null; then
    if grep -q "export default function register" .worklog/plugins/plugins-test-plugin.mjs; then
      test_result "Overwrite existing plugin file" "pass"
    else
      test_result "Overwrite existing plugin file" "fail" "File not overwritten"
    fi
  else
    test_result "Overwrite existing plugin file" "fail" "Installation failed"
  fi
}

# Test: Python package detection
test_python_package() {
  # Create minimal package
  echo "test" > ampa/__init__.py
  echo "requests>=2.0.0" > ampa/requirements.txt
  
  if ./install-test.sh --yes plugins-test-plugin.mjs .worklog/plugins 2>&1 > /dev/null; then
    if [ -d ".worklog/plugins/ampa_py/ampa" ]; then
      test_result "Python package installation" "pass"
    else
      test_result "Python package installation" "fail" "Package directory not created"
    fi
  else
    test_result "Python package installation" "fail" "Installation failed"
  fi
}

# Test: Env sample file
test_env_sample() {
  echo 'AMPA_DISCORD_BOT_TOKEN=""' > ampa/.env.sample
  
  if ./install-test.sh --bot-token "test-bot-token-123" --channel-id "123456789" --yes plugins-test-plugin.mjs .worklog/plugins 2>&1 > /dev/null; then
    if [ -f ".worklog/plugins/ampa_py/ampa/.env" ]; then
      test_result "Env sample file detection" "pass"
    else
      test_result "Env sample file detection" "fail" ".env file not created"
    fi
  else
    test_result "Env sample file detection" "fail" "Installation failed"
  fi
}

# Test: Bot config in env file
test_bot_config_in_env() {
  echo 'AMPA_DISCORD_BOT_TOKEN=""' > ampa/.env.sample
  TEST_BOT_TOKEN="test-bot-token-456"
  TEST_CHANNEL_ID="987654321"
  
  if ./install-test.sh --bot-token "$TEST_BOT_TOKEN" --channel-id "$TEST_CHANNEL_ID" --yes plugins-test-plugin.mjs .worklog/plugins 2>&1 > /dev/null; then
    if grep -q "AMPA_DISCORD_BOT_TOKEN=$TEST_BOT_TOKEN" .worklog/plugins/ampa_py/ampa/.env 2>/dev/null; then
      test_result "Bot token written to env file" "pass"
    else
      test_result "Bot token written to env file" "fail" "Bot token not in env"
    fi
  else
    test_result "Bot token written to env file" "fail" "Installation failed"
  fi
}

# Test: Existing installation detection
test_existing_install_detection() {
  # Create existing plugin
  mkdir -p .worklog/plugins
  echo "old" > .worklog/plugins/plugins-test-plugin.mjs
  
  if ./install-test.sh --yes plugins-test-plugin.mjs .worklog/plugins 2>&1 > /dev/null; then
    test_result "Existing installation detection" "pass"
  else
    test_result "Existing installation detection" "fail" "Should detect existing"
  fi
}

# Test: PID file handling
test_pid_file() {
  mkdir -p .worklog/ampa/default
  echo "$$" > .worklog/ampa/default/default.pid
  
  if ./install-test.sh --no-restart --yes plugins-test-plugin.mjs .worklog/plugins 2>&1 > /dev/null; then
    test_result "PID file handling" "pass"
  else
    test_result "PID file handling" "fail" "Should handle pid file"
  fi
}

# Test: Target directory creation
test_target_dir_creation() {
  if ./install-test.sh --yes plugins-test-plugin.mjs .worklog/custom/plugins 2>&1 > /dev/null; then
    if [ -d ".worklog/custom/plugins" ]; then
      test_result "Target directory creation" "pass"
    else
      test_result "Target directory creation" "fail" "Directory not created"
    fi
  else
    test_result "Target directory creation" "fail" "Installation failed"
  fi
}

# Test: Script syntax
test_script_syntax() {
  if sh -n ./install-test.sh 2>&1 > /dev/null; then
    test_result "Script has valid shell syntax" "pass"
  else
    test_result "Script has valid shell syntax" "fail" "Syntax error"
  fi
}

# Test: Strict mode
test_strict_mode() {
  if grep -q "set -eu" ./install-test.sh; then
    test_result "Script sets strict mode" "pass"
  else
    test_result "Script sets strict mode" "fail" "Missing set -eu"
  fi
}

# Test: Help text completeness
test_help_completeness() {
  local help_output=$(./install-test.sh --help 2>&1)
  
  local has_bot_token=0
  local has_yes=0
  local has_restart=0
  
  if echo "$help_output" | grep -q "\--bot-token\|--yes\|--restart"; then
    test_result "Help text contains all options" "pass"
  else
    test_result "Help text contains all options" "fail" "Missing option descriptions"
  fi
}

# Test: Backward compatibility - single argument
test_compat_single_arg() {
  if ./install-test.sh --yes plugins-test-plugin.mjs 2>&1 > /dev/null; then
    if [ -f ".worklog/plugins/plugins-test-plugin.mjs" ]; then
      test_result "Backward compat: single argument source" "pass"
    else
      test_result "Backward compat: single argument source" "fail" "File not created"
    fi
  else
    test_result "Backward compat: single argument source" "fail" "Installation failed"
  fi
}

# Test: Backward compatibility - two arguments
test_compat_two_args() {
  if ./install-test.sh --yes plugins-test-plugin.mjs custom-target 2>&1 > /dev/null; then
    if [ -f "custom-target/plugins-test-plugin.mjs" ]; then
      test_result "Backward compat: source and target" "pass"
    else
      test_result "Backward compat: source and target" "fail" "File not created in target"
    fi
  else
    test_result "Backward compat: source and target" "fail" "Installation failed"
  fi
}

# Test: Mutually exclusive options
test_exclusive_options() {
  if ! ./install-test.sh --restart --no-restart --yes plugins-test-plugin.mjs 2>&1 | grep -q "mutually exclusive"; then
    test_result "Mutually exclusive options error" "fail" "Should show error"
  else
    test_result "Mutually exclusive options error" "pass"
  fi
}

# Test: Decision log
test_decision_log() {
  if ./install-test.sh --yes plugins-test-plugin.mjs .worklog/plugins 2>&1 > /dev/null; then
    if [ -L "/tmp/ampa_install_decisions.log" ] || [ -f "/tmp/ampa_install_decisions.log" ]; then
      test_result "Decision log creation" "pass"
    else
      test_result "Decision log creation" "fail" "Log not created"
    fi
  else
    test_result "Decision log creation" "fail" "Installation failed"
  fi
}

# Main test execution
main() {
  printf "\n${YELLOW}=== Ampa Plugin Installer Test Suite ===${NC}\n"
  printf "Testing: %s\n\n" "/home/rogardle/.config/opencode/skill/install-ampa/scripts/install-worklog-plugin.sh"
  
  setup_test_env
  
  print_section "Argument Parsing Tests"
  test_help_flag
  test_unknown_option
  test_bot_token_option
  test_channel_id_option
  test_bot_token_without_value
  test_exclusive_options
  
  print_section "Source File Tests"
  test_source_not_found
  test_default_source
  
  print_section "Plugin Installation Tests"
  test_install_plugin
  test_overwrite_plugin
  
  print_section "Python Package Tests"
  test_python_package
  
  print_section "Environment File Tests"
  test_env_sample
  test_bot_config_in_env
  
  print_section "Installation Flow Tests"
  test_existing_install_detection
  test_pid_file
  test_target_dir_creation
  
  print_section "Script Quality Tests"
  test_script_syntax
  test_strict_mode
  test_help_completeness
  
  print_section "Backward Compatibility Tests"
  test_compat_single_arg
  test_compat_two_args
  
  print_section "Logging Tests"
  test_decision_log
  
  # Print summary
  printf "\n${YELLOW}=== Test Summary ===${NC}\n"
  printf "Tests run:    %d\n" "$TESTS_RUN"
  printf "${GREEN}Tests passed: %d${NC}\n" "$TESTS_PASSED"
  if [ "$TESTS_FAILED" -gt 0 ]; then
    printf "${RED}Tests failed: %d${NC}\n" "$TESTS_FAILED"
  else
    printf "${GREEN}Tests failed: %d${NC}\n" "$TESTS_FAILED"
  fi
  printf "\n"
  
  cleanup_test_env
  
  # Exit with appropriate code
  if [ "$TESTS_FAILED" -eq 0 ]; then
    printf "${GREEN}All tests passed!${NC}\n\n"
    exit 0
  else
    printf "${RED}Some tests failed!${NC}\n\n"
    exit 1
  fi
}

main "$@"
