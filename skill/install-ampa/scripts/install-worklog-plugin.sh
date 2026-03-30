#!/usr/bin/env sh
# Install a Worklog plugin into the global or per-project plugin directory.
# By default, installs to ${XDG_CONFIG_HOME:-$HOME/.config}/opencode/.worklog/plugins/.
# Use --local to install to the current project's .worklog/plugins/ directory.
# Usage: ./skill/install-ampa/scripts/install-worklog-plugin.sh [--local] <source-file> [target-dir]
# Example: ./skill/install-ampa/scripts/install-worklog-plugin.sh skill/install-ampa/resources/ampa.mjs

set -eu

# ============================================================================
# CONSTANTS
# ============================================================================

# Resolve paths relative to this script's location so the installer can be
# executed from any working directory. SCRIPT_DIR points to
# skill/install-ampa/scripts and the canonical resources live at
# ../resources/ampa.mjs relative to this script.
SCRIPT_DIR="$(cd "$(dirname "$0")" >/dev/null 2>&1 && pwd)"
DEFAULT_SRC="$SCRIPT_DIR/../resources/ampa.mjs"
# Directory to copy AMPA python package from when not present in the project.
# Prefer XDG_CONFIG_HOME if set, otherwise default to $HOME/.config/opencode/ampa
CONFIG_AMPA_DIR="${XDG_CONFIG_HOME:-$HOME/.config}/opencode/ampa"
# Global plugin install directory (default when --local is not specified)
GLOBAL_PLUGINS_DIR="${XDG_CONFIG_HOME:-$HOME/.config}/opencode/.worklog/plugins"
LOCK_DIR="/tmp/ampa_install.lock"
DECISION_LOG="/tmp/ampa_install_decisions.$$"
PID_FILE=".worklog/ampa/default/default.pid"
# Remote AMPA repository to clone from when local/bundled sources are absent.
# Can be overridden by setting AMPA_REMOTE_REPO in the environment.
AMPA_REMOTE_REPO="${AMPA_REMOTE_REPO:-https://github.com/SorraTheOrc/ampa.git}"

# ============================================================================
# UTILITY FUNCTIONS
# ============================================================================

# Print message to stderr
log_error() {
  echo "$@" >&2
}

# Print message to stdout
log_info() {
  echo "$@"
}

# Get current timestamp in ISO format with timezone
get_timestamp() {
  date +"%Y-%m-%dT%H:%M:%S%z"
}

# Create a symlink to the decision log for convenience
create_decision_log_symlink() {
  ln -sf "$DECISION_LOG" /tmp/ampa_install_decisions.log || true
}

# Log a decision to the decision log file
log_decision() {
  local _ts=$(get_timestamp)
  printf "%s %s\n" "$_ts" "$1" >> "$DECISION_LOG" || true
}

# ============================================================================
# LOCKING / CONCURRENCY CONTROL
# ============================================================================

# Acquire an exclusive lock for installation
acquire_lock() {
  if ! mkdir "$LOCK_DIR" 2>/dev/null; then
    log_error "Another ampa install appears to be running (lock $LOCK_DIR). Try again later."
    exit 1
  fi
  # Ensure lock is cleaned up on normal exit and common signals.
  trap 'rmdir "$LOCK_DIR" 2>/dev/null || true' EXIT INT TERM
}

# Explicit release function for tests and explicit cleanup paths
release_lock() {
  if [ -d "$LOCK_DIR" ]; then
    rmdir "$LOCK_DIR" 2>/dev/null || true
  fi
  # Remove traps installed by acquire_lock so callers can control lifecycle
  trap - EXIT INT TERM || true
}

# Wrapper to ensure a unique install environment: acquires the lock and
# records that the caller holds it. Callers may still rely on the EXIT trap
# established by acquire_lock; release_lock() is provided if explicit
# release is desired.
ensure_unique_install() {
  acquire_lock
  LOCK_HELD=1
}

# ============================================================================
# ARGUMENT PARSING
# ============================================================================

# Parse command-line arguments and populate global variables.
# Global variables set: BOT_TOKEN, CHANNEL_ID, SRC, TARGET_DIR, AUTO_YES, FORCE_RESTART, FORCE_NO_RESTART, LOCAL_INSTALL
parse_args() {
  # Initialize output variables with defaults
  BOT_TOKEN=""
  CHANNEL_ID=""
  SRC_ARG=""
  TARGET_ARG=""
  AUTO_YES=0
  FORCE_RESTART=0
  FORCE_NO_RESTART=0
  LOCAL_INSTALL=0

  # Parse options
  while [ "$#" -gt 0 ]; do
    case "$1" in
      --bot-token)
        shift
        if [ "$#" -gt 0 ]; then
          BOT_TOKEN="$1"
          shift
        else
          log_error "--bot-token requires a value"
          exit 2
        fi
        ;;
      --channel-id)
        shift
        if [ "$#" -gt 0 ]; then
          CHANNEL_ID="$1"
          shift
        else
          log_error "--channel-id requires a value"
          exit 2
        fi
        ;;
      # --webhook removed: webhooks were migrated to the Discord bot/notifications
      # model. Operators should provide --bot-token and --channel-id or set
      # per-project .env values. Passing --webhook is no longer supported.
      --yes|-y)
        AUTO_YES=1
        shift
        ;;
      --restart)
        FORCE_RESTART=1
        shift
        ;;
      --no-restart)
        FORCE_NO_RESTART=1
        shift
        ;;
      --local)
        LOCAL_INSTALL=1
        shift
        ;;
  --help|-h)
        echo "Usage: $0 [--bot-token <token>] [--channel-id <id>] [--yes] [--restart|--no-restart] [--local] [source-file] [target-dir]"
        echo ""
        echo "Options:"
        echo "  --bot-token <token>  Set the Discord bot token in .env"
        echo "  --channel-id <id>    Set the Discord channel ID in .env"
        echo "  --yes, -y            Non-interactive mode (auto-accept prompts)"
        echo "  --restart            Force daemon restart after install"
        echo "  --no-restart         Skip daemon restart after install"
        echo "  --local              Install to per-project .worklog/plugins/ instead of global"
        echo ""
        echo "By default, plugins are installed globally to:"
        echo "  \${XDG_CONFIG_HOME:-\$HOME/.config}/opencode/.worklog/plugins/"
        echo "Use --local to install into the current project's .worklog/plugins/ directory."
        exit 0
        ;;
      --pool-size)
        shift
        if [ "$#" -gt 0 ]; then
          POOL_SIZE="$1"
          shift
        else
          log_error "--pool-size requires a numeric value"
          exit 2
        fi
        ;;
      --*)
        log_error "Unknown option: $1"
        exit 2
        ;;
      *)
        # Positional argument
        if [ -z "$SRC_ARG" ]; then
          SRC_ARG="$1"
        elif [ -z "$TARGET_ARG" ]; then
          TARGET_ARG="$1"
        else
          log_error "Ignoring extra argument: $1"
        fi
        shift
        ;;
    esac
  done

  # Validate argument combinations
  if [ "$FORCE_RESTART" -eq 1 ] && [ "$FORCE_NO_RESTART" -eq 1 ]; then
    log_error "--restart and --no-restart are mutually exclusive"
    exit 2
  fi

  # Set final values with defaults
  SRC="${SRC_ARG:-$DEFAULT_SRC}"
  # Pool size: respect explicit flag, then env WL_AMPA_POOL_SIZE, default 3
  POOL_SIZE="${POOL_SIZE:-${WL_AMPA_POOL_SIZE:-3}}"
  if [ -n "$TARGET_ARG" ]; then
    # Explicit target directory takes highest precedence
    TARGET_DIR="$TARGET_ARG"
  elif [ "$LOCAL_INSTALL" -eq 1 ]; then
    # --local flag: install to per-project directory (legacy behaviour)
    TARGET_DIR=".worklog/plugins"
  else
    # Default: global install directory
    TARGET_DIR="$GLOBAL_PLUGINS_DIR"
  fi
}

# ============================================================================
# UPGRADE/INSTALL DETECTION
# ============================================================================

# Detect if an existing installation is present
detect_existing_installation() {
  local basename="$(basename "$SRC")"
  
  if [ -f "$TARGET_DIR/$basename" ] || [ -d "$TARGET_DIR/ampa_py/ampa" ]; then
    return 0  # Existing installation found
  fi
  return 1  # No existing installation
}

# Prompt user for upgrade vs abort decision
prompt_upgrade_or_abort() {
  if [ "$AUTO_YES" -eq 1 ]; then
    # In non-interactive mode, default to upgrade
    return 0
  fi

  if [ -t 0 ]; then
    printf "Existing ampa installation detected at %s\n" "$TARGET_DIR"
    printf "Choose action: [U]pgrade/Reinstall (default), [A]bort: "
    if ! read -r CHOICE; then CHOICE=""; fi
    case "$(printf "%s" "$CHOICE" | tr '[:upper:]' '[:lower:]')" in
      a)
        echo "Aborting."
        exit 1
        ;;
      *)
        return 0  # Proceed with upgrade
        ;;
    esac
  fi

  return 0  # Default to upgrade
}

# ============================================================================
# LOCAL-TO-GLOBAL MIGRATION
# ============================================================================

# Detect a per-project (local) installation that should be migrated to global.
# Only relevant when installing to the global target (not --local).
# Returns 1 (no local install / same as global) when the local and global
# plugin directories resolve to the same path — there is nothing to migrate.
detect_local_install() {
  if [ -f ".worklog/plugins/ampa.mjs" ] || [ -d ".worklog/plugins/ampa_py" ]; then
    # Resolve both paths to absolute to detect the same-directory case
    local local_abs global_abs
    local_abs="$(cd ".worklog/plugins" 2>/dev/null && pwd)" || true
    global_abs="$(cd "$GLOBAL_PLUGINS_DIR" 2>/dev/null && pwd)" || true
    if [ -n "$local_abs" ] && [ "$local_abs" = "$global_abs" ]; then
      log_decision "DETECT_LOCAL_INSTALL=same_as_global ($local_abs)"
      return 1  # Same directory — not a separate local install
    fi
    return 0  # Local install found
  fi
  return 1  # No local install
}

# Prompt user whether to migrate a local install to global.
# Returns 0 (yes, migrate) or 1 (no, skip).
# Migration is a destructive action so the default is to skip it.
# The user must explicitly opt in by answering 'y' or 'yes'.
prompt_migrate() {
  if [ "$AUTO_YES" -eq 1 ]; then
    return 1  # Auto-yes should not auto-migrate; upgrade in place instead
  fi

  if [ -t 0 ]; then
    printf "Detected existing per-project plugin in .worklog/plugins/.\n"
    printf "Migrate to global install? [y/N]: "
    if ! read -r MIGRATE_ANS; then MIGRATE_ANS=""; fi
    case "$(printf "%s" "$MIGRATE_ANS" | tr '[:upper:]' '[:lower:]')" in
      y|yes)
        return 0  # Yes, migrate (explicit opt-in)
        ;;
      *)
        return 1  # Skip migration (default)
        ;;
    esac
  fi

  return 1  # Non-interactive, non-auto: skip
}

# Merge two pool-state.json files. When the same container name exists in both,
# keep the entry with the more recent claimedAt timestamp.
# Usage: merge_pool_state <local_file> <global_file> <output_file>
merge_pool_state() {
  local local_file="$1"
  local global_file="$2"
  local output_file="$3"

  # If only one file exists, just copy it
  if [ ! -f "$local_file" ] && [ ! -f "$global_file" ]; then
    return 0  # Nothing to merge
  fi
  if [ ! -f "$local_file" ]; then
    cp -f "$global_file" "$output_file"
    return 0
  fi
  if [ ! -f "$global_file" ]; then
    cp -f "$local_file" "$output_file"
    return 0
  fi

  # Both files exist — merge using Node.js
  node -e "
    const fs = require('fs');
    const local = JSON.parse(fs.readFileSync('$local_file', 'utf8'));
    const global = JSON.parse(fs.readFileSync('$global_file', 'utf8'));
    const merged = { ...global };
    for (const [name, entry] of Object.entries(local)) {
      if (!merged[name]) {
        merged[name] = entry;
      } else {
        const localTime = new Date(entry.claimedAt || 0).getTime();
        const globalTime = new Date(merged[name].claimedAt || 0).getTime();
        if (localTime > globalTime) {
          merged[name] = entry;
        }
      }
    }
    fs.writeFileSync('$output_file', JSON.stringify(merged, null, 2), 'utf8');
  " 2>/dev/null

  if [ $? -ne 0 ]; then
    log_error "Warning: pool-state merge failed; keeping global copy"
    if [ -f "$global_file" ]; then
      cp -f "$global_file" "$output_file"
    fi
  fi
}

# Merge two pool-cleanup.json files (arrays). Union of both lists, deduplicated.
# Usage: merge_cleanup_list <local_file> <global_file> <output_file>
merge_cleanup_list() {
  local local_file="$1"
  local global_file="$2"
  local output_file="$3"

  if [ ! -f "$local_file" ] && [ ! -f "$global_file" ]; then
    return 0
  fi
  if [ ! -f "$local_file" ]; then
    cp -f "$global_file" "$output_file"
    return 0
  fi
  if [ ! -f "$global_file" ]; then
    cp -f "$local_file" "$output_file"
    return 0
  fi

  # Both files exist — merge using Node.js
  node -e "
    const fs = require('fs');
    const local = JSON.parse(fs.readFileSync('$local_file', 'utf8'));
    const global = JSON.parse(fs.readFileSync('$global_file', 'utf8'));
    const merged = [...new Set([...(Array.isArray(global) ? global : []), ...(Array.isArray(local) ? local : [])])];
    fs.writeFileSync('$output_file', JSON.stringify(merged, null, 2), 'utf8');
  " 2>/dev/null

  if [ $? -ne 0 ]; then
    log_error "Warning: pool-cleanup merge failed; keeping global copy"
    if [ -f "$global_file" ]; then
      cp -f "$global_file" "$output_file"
    fi
  fi
}

# Migrate a per-project installation to the global location.
# Moves pool state files, removes old plugin, preserves per-project config.
migrate_to_global() {
  local local_ampa_dir=".worklog/ampa"
  # Construct global ampa dir from the same XDG base as GLOBAL_PLUGINS_DIR
  local xdg_base="${XDG_CONFIG_HOME:-$HOME/.config}"
  local global_ampa_dir="$xdg_base/opencode/.worklog/ampa"

  log_info "Migrating per-project install to global location..."
  log_decision "MIGRATION_START local_ampa=$local_ampa_dir global_ampa=$global_ampa_dir"

  # Ensure global ampa directory exists
  mkdir -p "$global_ampa_dir"

  # Migrate pool-state.json (merge if both exist)
  if [ -f "$local_ampa_dir/pool-state.json" ]; then
    merge_pool_state "$local_ampa_dir/pool-state.json" "$global_ampa_dir/pool-state.json" "$global_ampa_dir/pool-state.json"
    log_info "Migrated pool-state.json to $global_ampa_dir/"
    rm -f "$local_ampa_dir/pool-state.json"
    log_decision "MIGRATED_POOL_STATE=1"
  else
    log_decision "MIGRATED_POOL_STATE=0 (not found)"
  fi

  # Migrate pool-cleanup.json (merge if both exist)
  if [ -f "$local_ampa_dir/pool-cleanup.json" ]; then
    merge_cleanup_list "$local_ampa_dir/pool-cleanup.json" "$global_ampa_dir/pool-cleanup.json" "$global_ampa_dir/pool-cleanup.json"
    log_info "Migrated pool-cleanup.json to $global_ampa_dir/"
    rm -f "$local_ampa_dir/pool-cleanup.json"
    log_decision "MIGRATED_POOL_CLEANUP=1"
  else
    log_decision "MIGRATED_POOL_CLEANUP=0 (not found)"
  fi

  # Migrate pool-replenish.log (append local to global)
  if [ -f "$local_ampa_dir/pool-replenish.log" ]; then
    cat "$local_ampa_dir/pool-replenish.log" >> "$global_ampa_dir/pool-replenish.log" 2>/dev/null || true
    log_info "Migrated pool-replenish.log to $global_ampa_dir/"
    rm -f "$local_ampa_dir/pool-replenish.log"
    log_decision "MIGRATED_REPLENISH_LOG=1"
  else
    log_decision "MIGRATED_REPLENISH_LOG=0 (not found)"
  fi

  # Remove old plugin file from per-project location
  if [ -f ".worklog/plugins/ampa.mjs" ]; then
    rm -f ".worklog/plugins/ampa.mjs"
    log_info "Removed old .worklog/plugins/ampa.mjs"
    log_decision "REMOVED_LOCAL_PLUGIN=1"
  fi

  # Note: we do NOT remove .worklog/plugins/ampa_py/ here because the
  # new install will deploy to the global target. The old ampa_py contains
  # .env and scheduler_store.json which are per-project config and should
  # be preserved. Cleanup of the old ampa_py is left to the user.
  if [ -d ".worklog/plugins/ampa_py" ]; then
    log_info "Note: .worklog/plugins/ampa_py/ preserved (contains per-project config)."
    log_info "You may remove it manually after verifying your .env and scheduler_store.json"
    log_info "are configured at the project level (.worklog/ampa/)."
    log_decision "OLD_AMPA_PY_PRESERVED=1"
  fi

  log_info "Migration complete."
  log_decision "MIGRATION_COMPLETE=1"
}

# ============================================================================
# ENV FILE HANDLING
# ============================================================================

# Check if a pre-existing .env is bundled in the plugin directory
check_for_bundled_env() {
  if [ -f "$TARGET_DIR/ampa_py/ampa/.env" ]; then
    return 0  # Bundled .env found
  fi
  return 1  # No bundled .env
}

# Detect existing bot token in current install or repo
detect_existing_bot_token() {
  if [ -f "$TARGET_DIR/ampa_py/ampa/.env" ]; then
    awk -F= '/AMPA_DISCORD_BOT_TOKEN/ {gsub(/^[ \t]+|[ \t]+$/,"",$2); print $2}' "$TARGET_DIR/ampa_py/ampa/.env" | tr -d '"' | tr -d "'"
  elif [ -f "ampa/.env" ]; then
    awk -F= '/AMPA_DISCORD_BOT_TOKEN/ {gsub(/^[ \t]+|[ \t]+$/,"",$2); print $2}' "ampa/.env" | tr -d '"' | tr -d "'"
  fi
}

# Detect existing channel ID in current install or repo
detect_existing_channel_id() {
  if [ -f "$TARGET_DIR/ampa_py/ampa/.env" ]; then
    awk -F= '/AMPA_DISCORD_CHANNEL_ID/ {gsub(/^[ \t]+|[ \t]+$/,"",$2); print $2}' "$TARGET_DIR/ampa_py/ampa/.env" | tr -d '"' | tr -d "'"
  elif [ -f "ampa/.env" ]; then
    awk -F= '/AMPA_DISCORD_CHANNEL_ID/ {gsub(/^[ \t]+|[ \t]+$/,"",$2); print $2}' "ampa/.env" | tr -d '"' | tr -d "'"
  fi
}

# Note: webhook-specific helpers were removed. The installer enforces the
# bot-token/channel model; operators should use --bot-token and --channel-id
# or set per-project .env values. This avoids reintroducing legacy webhook
# behaviour which the runtime has migrated away from.

# Prompt user whether to change bot config during upgrade
prompt_bot_config_change() {
  local existing_token="$1"
  local existing_channel="$2"
  
  if [ -z "$existing_token" ] && [ -z "$existing_channel" ]; then
    # No existing config, allow entering new one
    if [ "$AUTO_YES" -eq 1 ]; then
      BOT_TOKEN=""
      CHANNEL_ID=""
    else
      if [ -t 0 ]; then
        printf "Enter Discord bot token to use for installation (leave empty to skip): "
        if ! read -r NEW_TK; then NEW_TK=""; fi
        BOT_TOKEN="$NEW_TK"
        printf "Enter Discord channel ID to use for installation (leave empty to skip): "
        if ! read -r NEW_CH; then NEW_CH=""; fi
        CHANNEL_ID="$NEW_CH"
      fi
    fi
  else
    # Existing config found
    if [ -t 0 ]; then
      if [ -n "$existing_token" ]; then
        printf "Existing bot token detected: %.8s...\n" "$existing_token"
      fi
      if [ -n "$existing_channel" ]; then
        printf "Existing channel ID detected: %s\n" "$existing_channel"
      fi
      printf "Change Discord bot config? [y/N]: "
      if ! read -r CHW; then CHW=""; fi
      case "$(printf "%s" "$CHW" | tr '[:upper:]' '[:lower:]')" in
        y|yes)
          printf "Enter new bot token (leave empty to reuse existing, or '-' to remove): "
          if ! read -r NEW_TK; then NEW_TK=""; fi
          if [ "$NEW_TK" = "-" ]; then
            BOT_TOKEN=""
            REMOVE_BOT_CONFIG=1
          elif [ -n "$NEW_TK" ]; then
            BOT_TOKEN="$NEW_TK"
          else
            SKIP_BOT_CONFIG_UPDATE=1
          fi
          printf "Enter new channel ID (leave empty to reuse existing): "
          if ! read -r NEW_CH; then NEW_CH=""; fi
          if [ -n "$NEW_CH" ]; then
            CHANNEL_ID="$NEW_CH"
          fi
          ;;
        *)
          SKIP_BOT_CONFIG_UPDATE=1
          ;;
      esac
    fi
  fi
}

# Prompt user for bot config during fresh install
prompt_bot_config_new() {
  if [ -t 0 ]; then
    if [ "$AUTO_YES" -eq 1 ]; then
      BOT_TOKEN=""
      CHANNEL_ID=""
    else
      while true; do
        printf "Enter Discord bot token to use for installation: "
        if ! read -r NEW_TK; then NEW_TK=""; fi
        if [ -n "$NEW_TK" ]; then
          BOT_TOKEN="$NEW_TK"
          break
        else
          printf "Bot token is required for a new installation.\n"
        fi
      done
      while true; do
        printf "Enter Discord channel ID to use for installation: "
        if ! read -r NEW_CH; then NEW_CH=""; fi
        if [ -n "$NEW_CH" ]; then
          CHANNEL_ID="$NEW_CH"
          break
        else
          printf "Channel ID is required for a new installation.\n"
        fi
      done
    fi
  fi
}

# Find env sample file (.env.sample or .env.samplw)
find_env_sample() {
  if [ -f "$TARGET_DIR/ampa_py/ampa/.env.sample" ]; then
    echo "$TARGET_DIR/ampa_py/ampa/.env.sample"
  elif [ -f "$TARGET_DIR/ampa_py/ampa/.env.samplw" ]; then
    echo "$TARGET_DIR/ampa_py/ampa/.env.samplw"
  elif [ -f "ampa/.env.sample" ]; then
    echo "ampa/.env.sample"
  elif [ -f "ampa/.env.samplw" ]; then
    echo "ampa/.env.samplw"
  fi
}

# Back up existing .env file before removal
backup_env_file() {
  local target_env="$1"
  local backup_dir="${2:-.}"  # Use specified directory, or current directory by default
  local backup_filename=$(basename "$target_env")
  local backup_path="$backup_dir/$backup_filename.preinstall.$$"
  
  if [ -f "$target_env" ]; then
    mkdir -p "$backup_dir" 2>/dev/null || true
    if cp -a "$target_env" "$backup_path" 2>/dev/null || cp "$target_env" "$backup_path" 2>/dev/null; then
      echo "$backup_path"
      log_decision "BACKUP_ENV=$backup_path"
      return 0
    fi
  fi
  return 1
}

# Write bot token and channel ID to .env file
write_bot_config_to_env() {
  local env_file="$1"
  local bot_token="$2"
  local channel_id="$3"
  
  if [ -f "$env_file" ]; then
    if command -v awk >/dev/null 2>&1; then
      if [ -n "$bot_token" ]; then
        awk -v t="$bot_token" 'BEGIN{r=0} /^AMPA_DISCORD_BOT_TOKEN=/ {print "AMPA_DISCORD_BOT_TOKEN=" t; r=1; next} {print} END{if(r==0) print "AMPA_DISCORD_BOT_TOKEN=" t}' "$env_file" > "$env_file.tmp" && mv "$env_file.tmp" "$env_file"
        log_info "Updated bot token in $env_file"
      fi
      if [ -n "$channel_id" ]; then
        awk -v c="$channel_id" 'BEGIN{r=0} /^AMPA_DISCORD_CHANNEL_ID=/ {print "AMPA_DISCORD_CHANNEL_ID=" c; r=1; next} {print} END{if(r==0) print "AMPA_DISCORD_CHANNEL_ID=" c}' "$env_file" > "$env_file.tmp" && mv "$env_file.tmp" "$env_file"
        log_info "Updated channel ID in $env_file"
      fi
    else
      if [ -n "$bot_token" ]; then
        echo "AMPA_DISCORD_BOT_TOKEN=$bot_token" >> "$env_file"
        log_info "Appended bot token to $env_file"
      fi
      if [ -n "$channel_id" ]; then
        echo "AMPA_DISCORD_CHANNEL_ID=$channel_id" >> "$env_file"
        log_info "Appended channel ID to $env_file"
      fi
    fi
  else
    # Try to create from sample
    local sample=$(find_env_sample)
    if [ -n "$sample" ] && [ -f "$sample" ]; then
      cp -f "$sample" "$env_file"
      if command -v awk >/dev/null 2>&1; then
        if [ -n "$bot_token" ]; then
          awk -v t="$bot_token" 'BEGIN{r=0} /^AMPA_DISCORD_BOT_TOKEN=/ {print "AMPA_DISCORD_BOT_TOKEN=" t; r=1; next} {print} END{if(r==0) print "AMPA_DISCORD_BOT_TOKEN=" t}' "$env_file" > "$env_file.tmp" && mv "$env_file.tmp" "$env_file"
        fi
        if [ -n "$channel_id" ]; then
          awk -v c="$channel_id" 'BEGIN{r=0} /^AMPA_DISCORD_CHANNEL_ID=/ {print "AMPA_DISCORD_CHANNEL_ID=" c; r=1; next} {print} END{if(r==0) print "AMPA_DISCORD_CHANNEL_ID=" c}' "$env_file" > "$env_file.tmp" && mv "$env_file.tmp" "$env_file"
        fi
        log_info "Copied sample and wrote bot config to $env_file"
      else
        if [ -n "$bot_token" ]; then
          echo "AMPA_DISCORD_BOT_TOKEN=$bot_token" >> "$env_file"
        fi
        if [ -n "$channel_id" ]; then
          echo "AMPA_DISCORD_CHANNEL_ID=$channel_id" >> "$env_file"
        fi
        log_info "Copied sample and appended bot config to $env_file"
      fi
    else
      log_info "No .env or .env.sample available; skipping bot config write"
    fi
  fi
}

# Remove bot config from .env file
remove_bot_config_from_env() {
  local env_file="$1"
  
  if [ -f "$env_file" ]; then
    if command -v awk >/dev/null 2>&1; then
      awk '!/^AMPA_DISCORD_BOT_TOKEN=/ && !/^AMPA_DISCORD_CHANNEL_ID=/' "$env_file" > "$env_file.tmp" && mv "$env_file.tmp" "$env_file"
    else
      grep -v '^AMPA_DISCORD_BOT_TOKEN=' "$env_file" | grep -v '^AMPA_DISCORD_CHANNEL_ID=' > "$env_file.tmp" && mv "$env_file.tmp" "$env_file" || true
    fi
    log_info "Removed AMPA_DISCORD_BOT_TOKEN and AMPA_DISCORD_CHANNEL_ID from $env_file"
  fi
}

# Restore .env file from backup
restore_env_file() {
  local backup_path="$1"
  local target_env="$2"
  
  if [ -n "$backup_path" ] && [ -f "$backup_path" ]; then
    if mv "$backup_path" "$target_env" 2>/dev/null; then
      log_decision "RESTORED_ENV=$backup_path"
      return 0
    elif cp -p "$backup_path" "$target_env" 2>/dev/null; then
      log_decision "RESTORED_ENV_COPY=$backup_path"
      return 0
    else
      log_decision "RESTORE_FAILED=$backup_path"
      return 1
    fi
  fi
  return 1
}

# ============================================================================
# PYTHON VENV SETUP
# ============================================================================

# Find python executable (prefer python3, fall back to python)
check_python_executable() {
  if command -v python3 >/dev/null 2>&1; then
    echo "python3"
  elif command -v python >/dev/null 2>&1; then
    echo "python"
  fi
}

# Create virtual environment
create_venv() {
  local venv_dir="$1"
  local py_bin="$2"
  
  if [ -d "$venv_dir" ]; then
    log_info "Virtual environment already exists at $venv_dir"
    return 0
  fi

  log_info "Creating virtualenv at $venv_dir"
  if ! "$py_bin" -m venv "$venv_dir" 2>&1 | tee /tmp/ampa_install_venv.log; then
    log_error "Error: failed to create venv with $py_bin -m venv. See /tmp/ampa_install_venv.log"
    return 1
  fi

  return 0
}

# Verify virtual environment is properly set up
verify_venv() {
  local venv_dir="$1"
  
  if [ ! -x "$venv_dir/bin/python" ]; then
    log_error "Error: virtualenv python not found at $venv_dir/bin/python"
    return 1
  fi

  return 0
}

# Install Python dependencies via pip
install_python_deps() {
  local venv_dir="$1"
  local req_file="$2"
  
  if [ ! -f "$req_file" ]; then
    log_info "No requirements.txt found; skipping pip install"
    return 0
  fi

  log_info "Upgrading pip and installing requirements into venv (logs: /tmp/ampa_install_pip.log)"
  "$venv_dir/bin/python" -m pip install --upgrade pip setuptools wheel 2>&1 | tee /tmp/ampa_install_pip.log || true

  if "$venv_dir/bin/python" -m pip install -r "$req_file" 2>&1 | tee -a /tmp/ampa_install_pip.log; then
    log_info "Installed Python dependencies into $venv_dir"
    return 0
  else
    log_error "Error: pip install failed. See /tmp/ampa_install_pip.log for details."
    log_error "You can try to re-run:"
    log_error "  $venv_dir/bin/python -m pip install -r $req_file"
    return 1
  fi
}

# ============================================================================
# PLUGIN INSTALLATION
# ============================================================================

# Validate that source file exists
validate_source_file() {
  if [ ! -f "$SRC" ]; then
    log_error "Source file not found: $SRC"
    log_error "If you intended to install the canonical plugin, run without arguments to use $DEFAULT_SRC"
    exit 2
  fi
}

# Install the Worklog .mjs plugin file
install_worklog_plugin() {
  local basename="$(basename "$SRC")"
  
  if ! mkdir -p "$TARGET_DIR" 2>/dev/null; then
    log_error "Error: cannot create plugin directory: $TARGET_DIR"
    exit 1
  fi
  cp -f "$SRC" "$TARGET_DIR/$basename"
  log_info "Installed Worklog plugin $SRC to $TARGET_DIR/$basename"
}

# Copy Python package into plugin directory
copy_python_package() {
   # Optional first arg: source dir to copy python package from. Defaults to "ampa"
   local src_dir="${1:-ampa}"
   local py_target_dir="$TARGET_DIR/ampa_py"
   local env_backup=""
   local store_backup=""
   
   # Record pre-removal state
   log_decision "PRE_REMOVE_ls=$(ls -la \"$py_target_dir\" 2>/dev/null || true)"
   
   # Backup existing .env if present
   if [ -f "$py_target_dir/ampa/.env" ]; then
     env_backup=$(backup_env_file "$py_target_dir/ampa/.env")
   fi

    # Backup existing scheduler_store.json if present
    # Store backup OUTSIDE the ampa directory so it survives the rm -rf
    if [ -f "$py_target_dir/ampa/scheduler_store.json" ]; then
      store_backup=$(backup_env_file "$py_target_dir/ampa/scheduler_store.json" "$py_target_dir")
      log_decision "BACKUP_SCHEDULER_STORE=$store_backup"
    fi


   # Remove old bundle and copy new one
    mkdir -p "$py_target_dir"
    rm -rf "$py_target_dir/ampa"
    if [ -d "$src_dir" ]; then
      cp -R "$src_dir" "$py_target_dir/ampa"
    else
      # Fall back to bundled installer resources in the repo
      local bundled="$SCRIPT_DIR/../resources/ampa_py/ampa"
      if [ -d "$bundled" ]; then
        cp -R "$bundled" "$py_target_dir/ampa"
        log_decision "COPIED_FROM_BUNDLED_RESOURCES=$bundled"
      else
        # As a last resort, attempt to clone the remote AMPA repository and
        # copy from the freshly-cloned repo. This allows migration to a new
        # remote repo without bundling the package in this repository.
        log_decision "COPY_SRC_MISSING=$src_dir;ATTEMPT_REMOTE_CLONE=${AMPA_REMOTE_REPO}"
        if command -v git >/dev/null 2>&1; then
          tmp_clone_dir="$(mktemp -d 2>/dev/null || echo "/tmp/ampa_clone_$$")"
          if git clone --depth 1 "$AMPA_REMOTE_REPO" "$tmp_clone_dir" >/dev/null 2>&1; then
            if [ -d "$tmp_clone_dir/ampa" ]; then
              cp -R "$tmp_clone_dir/ampa" "$py_target_dir/ampa"
              log_decision "COPIED_FROM_REMOTE=${AMPA_REMOTE_REPO}"
            else
              # Some repos may have the package at repo root; use root as src
              cp -R "$tmp_clone_dir" "$py_target_dir/ampa"
              log_decision "COPIED_FROM_REMOTE_ROOT=${AMPA_REMOTE_REPO}"
            fi
            # cleanup
            rm -rf "$tmp_clone_dir" || true
          else
            log_decision "REMOTE_CLONE_FAILED=${AMPA_REMOTE_REPO}"
            log_error "AMPA source directory not found: $src_dir and remote clone failed"
            return 1
          fi
        else
          log_decision "COPY_SRC_MISSING=${src_dir};GIT_NOT_AVAILABLE"
          log_error "AMPA source directory not found: $src_dir (and git not available for remote clone)"
          return 1
        fi
      fi
    fi
   
    # Record post-copy state
    log_decision "POST_COPY_ls=$(ls -la \"$py_target_dir/ampa\" 2>/dev/null || true)"

    # Ensure scheduler_store.json exists for fresh installs
    if [ ! -f "$py_target_dir/ampa/scheduler_store.json" ]; then
      if [ -f "$py_target_dir/ampa/scheduler_store_example.json" ]; then
        cp -p "$py_target_dir/ampa/scheduler_store_example.json" "$py_target_dir/ampa/scheduler_store.json" 2>/dev/null || \
          cp "$py_target_dir/ampa/scheduler_store_example.json" "$py_target_dir/ampa/scheduler_store.json" 2>/dev/null || true
        log_info "Initialized scheduler_store.json from scheduler_store_example.json"
      else
        printf '{"commands": {}, "state": {}, "last_global_start_ts": null}\n' > "$py_target_dir/ampa/scheduler_store.json"
        log_info "Initialized empty scheduler_store.json"
      fi
    fi

   # Restore .env if we backed it up
   if [ -n "$env_backup" ]; then
     restore_env_file "$env_backup" "$py_target_dir/ampa/.env"
   fi

   # Restore scheduler_store.json if we backed it up
   if [ -n "$store_backup" ]; then
     restore_env_file "$store_backup" "$py_target_dir/ampa/scheduler_store.json"
     log_info "Preserved existing scheduler_store.json during upgrade"
   fi

   log_info "Installed Python ampa package to $py_target_dir/ampa"
}

# Set up Python package (venv and dependencies)
setup_python_package() {
  local py_target_dir="$TARGET_DIR/ampa_py"
  local req_file="$py_target_dir/ampa/requirements.txt"
  
  if [ ! -f "$req_file" ]; then
    log_info "No requirements.txt; skipping Python setup"
    return 0
  fi

  # Check for Python
  local py_bin
  py_bin=$(check_python_executable)
  if [ -z "$py_bin" ]; then
    log_error "Error: no python executable found in PATH; cannot create venv."
    exit 1
  fi

  # Create venv and install deps
  local venv_dir="$py_target_dir/venv"
  
  if ! create_venv "$venv_dir" "$py_bin"; then
    exit 1
  fi

  if ! verify_venv "$venv_dir"; then
    exit 1
  fi

  if ! install_python_deps "$venv_dir" "$req_file"; then
    exit 1
  fi

  return 0
}

# Ensure project scheduler store exists at <project>/.worklog/ampa/scheduler_store.json
ensure_project_scheduler_store() {
  local project_ampa_dir=".worklog/ampa"
  local project_store="$project_ampa_dir/scheduler_store.json"
  local plugin_store="$TARGET_DIR/ampa_py/ampa/scheduler_store.json"
  local plugin_store_example="$TARGET_DIR/ampa_py/ampa/scheduler_store_example.json"

  mkdir -p "$project_ampa_dir"

  if [ -f "$project_store" ]; then
    return 0
  fi

  if [ -f "$plugin_store" ]; then
    cp -p "$plugin_store" "$project_store" 2>/dev/null || cp "$plugin_store" "$project_store"
    log_info "Initialized $project_store from plugin scheduler_store.json"
    return 0
  fi

  if [ -f "$plugin_store_example" ]; then
    cp -p "$plugin_store_example" "$project_store" 2>/dev/null || cp "$plugin_store_example" "$project_store"
    log_info "Initialized $project_store from scheduler_store_example.json"
    return 0
  fi

  printf '{"commands": {}, "state": {}, "last_global_start_ts": null}\n' > "$project_store"
  log_info "Initialized empty $project_store"
}

# ============================================================================
# DAEMON RESTART HANDLING
# ============================================================================

# Detect if a daemon is currently running
detect_running_daemon() {
  # Only consider a daemon running if the pidfile exists, the pid is alive,
  # and the process command line indicates it is the AMPA daemon for this
  # project (to avoid confusing unrelated processes that reused the same PID).
  if [ -f "$PID_FILE" ]; then
    local pid_val
    pid_val=$(sed -n '1p' "$PID_FILE" 2>/dev/null || true)
    if [ -z "$pid_val" ]; then
      return 1
    fi
    if ! kill -0 "$pid_val" 2>/dev/null; then
      # stale pidfile
      return 1
    fi

    # Attempt to verify the process belongs to this project's AMPA by
    # inspecting its command line. Prefer /proc (Linux), fall back to ps.
    local expected_path
    # resolve expected python package path; TARGET_DIR may be relative
    if [ -d "$TARGET_DIR/ampa_py" ]; then
      expected_path="$(cd "$TARGET_DIR/ampa_py" >/dev/null 2>&1 && pwd || true)"
    else
      expected_path="$(pwd)/$TARGET_DIR/ampa_py"
    fi

    # read cmdline
    local cmdline
    if [ -r "/proc/$pid_val/cmdline" ]; then
      cmdline=$(tr '\0' ' ' < "/proc/$pid_val/cmdline" 2>/dev/null || true)
    else
      cmdline=$(ps -p "$pid_val" -o args= 2>/dev/null || true)
    fi

    if [ -n "$cmdline" ] && [ -n "$expected_path" ] && echo "$cmdline" | grep -F -- "$expected_path" >/dev/null 2>&1; then
      log_decision "DETECT_RUNNING=pid=$pid_val cmdline_matches=$expected_path"
      echo "$pid_val"
      return 0
    fi

    # If we couldn't validate, log the discovered cmdline for diagnostics
    log_decision "DETECT_RUNNING=pid=$pid_val cmdline_unverified: ${cmdline:-(empty)}"
    return 1
  fi
  return 1
}

# Prompt user whether to restart daemon
prompt_restart_daemon() {
  local running_pid="$1"
  
  if [ "$FORCE_RESTART" -eq 1 ]; then
    return 0  # Yes, restart
  fi

  if [ "$FORCE_NO_RESTART" -eq 1 ]; then
    return 1  # No, don't restart
  fi

  # Non-interactive default behaviour: when --yes/ AUTO_YES is provided,
  # treat the answer as consent to restart. Otherwise prompt interactively
  # if a tty is available; default to no restart in non-interactive shells.
  if [ "$AUTO_YES" -eq 1 ]; then
    return 0
  fi

  if [ -t 0 ]; then
    printf "Detected running daemon pid=%s. Restart automatically when installation completes? [Y/n]: " "$running_pid"
    if ! read -r RESTART_ANS; then RESTART_ANS=""; fi
    case "$(printf "%s" "$RESTART_ANS" | tr '[:upper:]' '[:lower:]')" in
      n|no)
        return 1  # No, don't restart
        ;;
      *)
        return 0  # Yes, restart (default)
        ;;
    esac
  fi

  return 1  # Default to no restart
}

# Stop the running daemon
stop_daemon() {
  log_info "Stopping running daemon before upgrade..."

  # Prefer to use `wl ampa stop` when the `wl` CLI actually exposes the
  # ampa command. Some environments may have a `wl` installed that does not
  # include the ampa plugin (or the installed plugin may be buggy). In that
  # case avoid invoking `wl ampa` (which could load a broken plugin) and fall
  # back to printing PID/command diagnostics and attempting a safe kill.
  if command -v wl >/dev/null 2>&1 && wl --help 2>&1 | grep -E '\bampa\b' >/dev/null 2>&1; then
    # Run the stop command and capture output so the operator sees what happened
    local _out
    if _out=$(wl ampa stop --name default 2>&1); then
      log_info "wl ampa stop output:"
      # Show each line to preserve formatting
      printf "%s\n" "$_out"
    else
      log_info "wl ampa stop returned non-zero (output):"
      printf "%s\n" "$_out"
    fi
  else
    log_info "Skipping 'wl ampa stop' because 'wl' does not expose the ampa command on PATH."
    # Provide diagnostics: show pidfile and process info if available so the
    # operator can act manually. Do not attempt to load the plugin.
    if [ -f "$PID_FILE" ]; then
      local _pidfile_pid
      _pidfile_pid=$(sed -n '1p' "$PID_FILE" 2>/dev/null || true)
      log_info "Observed pidfile $PID_FILE pid=${_pidfile_pid:-(none)}"
      if [ -n "$_pidfile_pid" ]; then
        if [ -r "/proc/$_pidfile_pid/cmdline" ]; then
          log_info "Process cmdline:"
          tr '\0' ' ' < "/proc/$_pidfile_pid/cmdline" 2>/dev/null || true
          printf "\n"
        else
          log_info "ps output for pid=$_pidfile_pid:"
          ps -p "$_pidfile_pid" -o pid,cmd= 2>/dev/null || true
        fi
        log_info "To stop the process manually: kill $_pidfile_pid (or use SIGTERM then SIGKILL if needed)."
      fi
    else
      log_info "No pidfile $PID_FILE present; nothing to stop via installer fallback."
    fi
  fi

  # Wait for the pid file to be removed or the process to exit. This provides
  # observable feedback during upgrades so the operator knows the service was
  # actually stopped before the installer proceeds.
  local _wait=0
  local _pid=""
  while [ -f "$PID_FILE" ] && [ "$_wait" -lt 20 ]; do
    _pid=$(sed -n '1p' "$PID_FILE" 2>/dev/null || true)
    if [ -z "$_pid" ] || ! kill -0 "$_pid" 2>/dev/null; then
      rm -f "$PID_FILE" 2>/dev/null || true
      break
    fi
    _wait=$((_wait + 1))
    sleep 0.5
  done

  if [ -f "$PID_FILE" ]; then
    log_error "Timed out waiting for daemon to stop; pidfile $PID_FILE still exists."
  else
    if [ -n "$_pid" ]; then
      log_info "Daemon stopped (pid=$_pid)"
    else
      log_info "Daemon stop completed (pid file removed)"
    fi
  fi
}

# Verify the daemon started successfully by checking PID file and process liveness
verify_daemon_started() {
  if [ -f "$PID_FILE" ]; then
    local newpid
    newpid=$(sed -n '1p' "$PID_FILE" 2>/dev/null || true)
    if [ -n "$newpid" ] && kill -0 "$newpid" 2>/dev/null; then
      log_info "Daemon running pid=$newpid"
      log_decision "VERIFY_DAEMON=ok PID=$newpid"
      return 0
    else
      log_error "PID file exists but no running process detected. See /tmp/ampa_install_start.log"
      log_decision "VERIFY_DAEMON=pid-not-running"
      return 1
    fi
  else
    log_error "No PID file created after start. See /tmp/ampa_install_start.log"
    log_decision "VERIFY_DAEMON=no-pid-file"
    return 1
  fi
}

# Start the daemon
start_daemon() {
  log_info "Attempting to start daemon..."
  log_decision "Attempting start: TARGET=$TARGET_DIR"

  if ! wl ampa start --name default > /tmp/ampa_install_start.log 2>&1; then
    log_error "Failed to start daemon; see /tmp/ampa_install_start.log"
    log_decision "START=failed"
    return 1
  fi

  verify_daemon_started
}

# Restart the daemon by stopping then starting it
restart_daemon() {
  log_info "Restarting daemon..."
  log_decision "RESTART=begin TARGET=$TARGET_DIR"

  stop_daemon

  start_daemon
  local rc=$?
  if [ "$rc" -eq 0 ]; then
    log_decision "RESTART=ok"
  else
    log_error "Daemon restart failed: start did not succeed after stop."
    log_decision "RESTART=failed"
  fi
  return "$rc"
}

# ============================================================================
# MAIN FLOW
# ============================================================================

main() {
  # Setup
  local _ts=$(get_timestamp)
  acquire_lock
  create_decision_log_symlink
  
  # Parse arguments
  parse_args "$@"

  # Validate source
  validate_source_file

  # Migrate per-project install to global (only when installing globally)
  if [ "$LOCAL_INSTALL" -eq 0 ] && [ -z "$TARGET_ARG" ]; then
    if detect_local_install; then
      if prompt_migrate; then
        migrate_to_global
      else
        # User declined migration — upgrade the existing local install in
        # place instead of installing a second copy into the global dir.
        TARGET_DIR=".worklog/plugins"
        log_info "Upgrading existing local install at $TARGET_DIR"
        log_info ""
        log_info "To migrate to a global install later, re-run the installer and"
        log_info "answer 'y' when prompted, or run:"
        log_info "  $SCRIPT_DIR/install-worklog-plugin.sh"
        log_info ""
        log_decision "MIGRATION_SKIPPED=user_declined TARGET_DIR=$TARGET_DIR"
      fi
    fi
  fi

  # Check for existing installation
  local existing_install=0
  if detect_existing_installation; then
    existing_install=1
    if ! prompt_upgrade_or_abort; then
      exit 0
    fi
    log_decision "ACTION_PROCEED=1 EXISTING_INST=1"
  else
    log_decision "ACTION_PROCEED=1 EXISTING_INST=0"
  fi

  # Handle bot config configuration
  REMOVE_BOT_CONFIG=0
  SKIP_BOT_CONFIG_UPDATE=0
  local preserve_existing_env=0
  
  if check_for_bundled_env; then
    preserve_existing_env=1
    log_decision "PRESERVE_EXISTING_ENV_DETECTED=1 PATH=$TARGET_DIR/ampa_py/ampa/.env"
  fi

  if [ -z "$BOT_TOKEN" ] && [ -z "$CHANNEL_ID" ]; then
    local existing_token
    local existing_channel
    existing_token=$(detect_existing_bot_token)
    existing_channel=$(detect_existing_channel_id)
    
    if [ "$existing_install" -eq 1 ]; then
      prompt_bot_config_change "$existing_token" "$existing_channel"
    else
      prompt_bot_config_new
    fi
  fi

  # Detect running daemon and decide on restart after install
  local do_restart=0
  local running_pid
  running_pid=$(detect_running_daemon) || true
  if [ -n "$running_pid" ] && prompt_restart_daemon "$running_pid"; then
    do_restart=1
    # Stop daemon before install; restart_daemon() will be called after install
    stop_daemon
  fi

  # Mask bot token for logging
  local tk_mask="(empty)"
  if [ -n "$BOT_TOKEN" ]; then
    tk_mask="$(printf "%.8s" "$BOT_TOKEN")..."
  fi
  log_decision "SRC=$SRC TARGET=$TARGET_DIR BOT_TOKEN=$tk_mask CHANNEL_ID=${CHANNEL_ID:-(empty)}"

  # Install plugin
  install_worklog_plugin

   # Install Python package: prefer project-local `ampa/`, fall back to
   # user's config directory (e.g. ~/.config/opencode/ampa). Missing AMPA is a
   # critical error for installations that expect the daemon; report and exit.
    if [ -d "ampa" ]; then
      if ! copy_python_package "ampa"; then
        log_error "Critical: failed to copy AMPA from project 'ampa' directory"
        exit 2
      fi
      setup_python_package
    elif [ -d "$CONFIG_AMPA_DIR" ]; then
      log_info "Copying AMPA package from $CONFIG_AMPA_DIR"
      if ! copy_python_package "$CONFIG_AMPA_DIR"; then
        log_error "Critical: failed to copy AMPA from $CONFIG_AMPA_DIR"
        exit 2
      fi
      setup_python_package
    elif [ -d "$SCRIPT_DIR/../resources/ampa_py/ampa" ]; then
      log_info "Copying AMPA package from bundled installer resources"
      if ! copy_python_package "$SCRIPT_DIR/../resources/ampa_py/ampa"; then
        log_error "Critical: failed to copy AMPA from bundled resources"
        exit 2
      fi
      setup_python_package
   else
     log_error "Critical: AMPA Python package not found in project (ampa/) or $CONFIG_AMPA_DIR"
     log_error "Install cannot proceed without AMPA; aborting."
     exit 2
   fi

   # Ensure scheduler store exists in the project runtime location.
   ensure_project_scheduler_store

   # Handle .env file configuration
   if [ "$SKIP_BOT_CONFIG_UPDATE" -eq 1 ] || [ "$preserve_existing_env" -eq 1 ]; then
     log_info "Preserving existing .env (user requested no bot config update or pre-existing .env)"
   else
     if [ "$REMOVE_BOT_CONFIG" -eq 1 ]; then
       local env_file="$TARGET_DIR/ampa_py/ampa/.env"
       remove_bot_config_from_env "$env_file"
     elif [ -n "$BOT_TOKEN" ] || [ -n "$CHANNEL_ID" ]; then
       local env_file="$TARGET_DIR/ampa_py/ampa/.env"
       write_bot_config_to_env "$env_file" "$BOT_TOKEN" "$CHANNEL_ID"
     else
       log_info "No bot config provided; skipping .env creation/update"
     fi
   fi

  # Start or restart daemon after install/upgrade completes, but only if a
  # Python ampa package was installed into the plugin directory. If no python
  # bundle is present there's nothing sensible to start and calling
  # `wl ampa start` will fail with "No command resolved".
  # Respect --no-restart flag.
  if [ "$FORCE_NO_RESTART" -eq 0 ] && [ -f "$TARGET_DIR/$(basename "$SRC")" ]; then
    if [ -d "$TARGET_DIR/ampa_py/ampa" ]; then
      if [ "$do_restart" -eq 1 ]; then
        log_info "Restarting daemon after upgrade..."
        # stop_daemon was already called before install; just start
        start_daemon
      else
        log_info "Starting daemon after installation..."
        start_daemon
      fi
    else
      log_info "No Python ampa package installed at $TARGET_DIR/ampa_py/ampa; skipping daemon start."
    fi
  fi

  # ==========================================================================
  # Post-install: warm the AMPA container pool when possible
  # ==========================================================================
  # Decide whether to attempt warm-pool. Respect non-interactive installs
  # (--yes / AUTO_YES) and environment hints such as CI=true. Default behaviour
  # (per intake) is to warm the pool when prerequisites exist, including in
  # non-interactive installs unless CI=true is set.
  if [ -f "$TARGET_DIR/$(basename "$SRC")" ]; then
    # Only attempt warm-pool when the Python package exists (we need the wl ampa command)
    # Skip warm-pool in CI when CI=true is set
    if [ "${CI:-}" = "true" ]; then
      log_info "CI environment detected; skipping post-install warm-pool. To run manually: wl ampa warm-pool --size $POOL_SIZE"
      log_decision "WARM_POOL=skipped_ci"
    else
      # Check for required host tooling: podman and distrobox
      if command -v podman >/dev/null 2>&1 && command -v distrobox >/dev/null 2>&1; then
        # Run warm-pool in non-interactive mode. We capture output and show
        # a short progress summary. Failures are non-fatal: record decision and
        # continue.
        log_info "Detected podman and distrobox on PATH; attempting to pre-warm container pool (size=$POOL_SIZE)."
        log_decision "WARM_POOL=begin SIZE=$POOL_SIZE"
        if wl ampa warm-pool --non-interactive --size "$POOL_SIZE" 2>/tmp/ampa_warm_pool.err >/tmp/ampa_warm_pool.out; then
          # Print succinct progress summary
          awk 'NR<=20{print}' /tmp/ampa_warm_pool.out || true
          log_info "warm-pool completed: see /tmp/ampa_warm_pool.out for details"
          log_decision "WARM_POOL=ok"
        else
          # Non-fatal: print one-line actionable error and continue
          log_error "warm-pool: encountered an error (non-fatal). Run: wl ampa warm-pool --size $POOL_SIZE  (see /tmp/ampa_warm_pool.err)"
          log_decision "WARM_POOL=failed"
        fi
      else
        # Missing prerequisites: print one-line actionable message
        log_info "Podman or Distrobox not found; to pre-warm the container pool install them and run: wl ampa warm-pool --size $POOL_SIZE"
        log_decision "WARM_POOL=prereqs_missing"
      fi
    fi
  fi


  log_info "Installation complete."
}

# Note: the installer no longer copies itself into the target plugin dir.
# Keeping installer in the repo (skill/install-ampa/scripts/install-worklog-plugin.sh) is preferred
# to avoid writing executable files into .worklog/ which is usually gitignored.

# Run main function with all arguments
main "$@"
