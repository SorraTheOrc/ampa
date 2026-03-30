#!/usr/bin/env bash
set -euo pipefail

# Lightweight CLI arg parsing: support --verbose to enable logging/tracing
VERBOSE=0
LOGFILE=""
PARAMS=()
while [[ $# -gt 0 ]]; do
  case "$1" in
    --verbose)
      VERBOSE=1
      shift
      ;;
    *)
      PARAMS+=("$1")
      shift
      ;;
  esac
done
set -- "${PARAMS[@]}"

# When verbose mode is enabled, capture stdout/stderr to a temp logfile
# and enable shell tracing. Any failing command will trigger the ERR trap
# which prints a concise failure message and the tail of the log so the
# caller can quickly see what went wrong.
if [[ "$VERBOSE" -eq 1 ]]; then
  LOGFILE="$(mktemp -t tmux.log.XXXXXX)" || LOGFILE="/tmp/tmux.log.$$"
  # Save original stdout/stderr
  exec 3>&1 4>&2
  # Redirect stdout/stderr to tee so we keep a copy in LOGFILE
  exec > >(tee -a "$LOGFILE") 2>&1

  # More informative trace prompt and enable xtrace
  # Use single quotes so parameter expansion is deferred until xtrace runs
  # and provide a safe default for FUNCNAME[0] to avoid `set -u` failures.
  PS4='+ ${BASH_SOURCE}:${LINENO}:${FUNCNAME[0]:-}: '
  set -x

  on_error() {
    local exit_code="$1"
    local line_no="$2"
    local cmd="$3"
    # Print a concise error message to the original stderr (fd4)
    echo "[tmux.sh] ERROR: exit $exit_code at line $line_no" >&4
    echo "[tmux.sh] Command: $cmd" >&4
    echo "[tmux.sh] Full log: $LOGFILE" >&4
    echo "[tmux.sh] Last 200 lines of log:" >&4
    tail -n 200 "$LOGFILE" >&4 || true
    # Restore original fds and exit with the failing command's code
    exec 1>&3 2>&4
    exit "$exit_code"
  }

  trap 'on_error $? $LINENO "$BASH_COMMAND"' ERR
fi

# Creates a tmux session with three panes per window:
# - Left pane: full height, 50% width
# - Right top: ~66% of right column height
# - Right bottom: ~33% of right column height

SESSION="Dev"
DEFAULT_WINDOW="Agents"
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# Prefer a config next to the repository root (parent of scripts/), fall back
# to a config sitting next to this script for backward compatibility.
if [[ -f "$SCRIPT_DIR/../tmux.windows.conf" ]]; then
  CONFIG_FILE="$SCRIPT_DIR/../tmux.windows.conf"
else
  CONFIG_FILE="$SCRIPT_DIR/tmux.windows.conf"
fi

normalize_dir() {
  local dir="$1"

  # Trim leading/trailing whitespace
  dir="${dir#"${dir%%[![:space:]]*}"}"
  dir="${dir%"${dir##*[![:space:]]}"}"

  # Strip matching surrounding quotes if present
  if [[ "$dir" == \"*\" && "$dir" == *\" ]]; then
    dir="${dir:1:-1}"
  elif [[ "$dir" == \'*\' && "$dir" == *\' ]]; then
    dir="${dir:1:-1}"
  fi

  # Expand leading ~ to HOME
  if [[ "$dir" == "~" || "$dir" == "~/"* ]]; then
    dir="${HOME}${dir:1}"
  fi

  printf '%s' "$dir"
}

create_three_pane_layout() {
  local target_window="$1"
  local left_cmd="$2"
  local top_right_cmd="$3"
  local pane_dir="$4"

  local left_pane
  local right_pane
  local bottom_right_pane

  # Wait for the target window to exist and report a pane id. New windows
  # may take a short moment to be created by tmux, especially when using
  # -c; poll briefly before proceeding.
  local tries=0
  local pane_check=""
  while ! pane_check="$(tmux display-message -p -t "$target_window" '#{pane_id}' 2>/dev/null || true)" || [[ -z "$pane_check" ]]; do
    sleep 0.01
    tries=$((tries + 1))
    if [[ $tries -gt 200 ]]; then
      # If the window never appears, abort with a helpful message
      echo "[tmux.sh] WARNING: timed out waiting for window $target_window to appear" >&2
      break
    fi
  done
  tmux select-window -t "$target_window" 2>/dev/null || true
  left_pane="${pane_check:-$(tmux display-message -p -t "$target_window" '#{pane_id}' 2>/dev/null || true)}"

  # Ensure pane synchronization is disabled so send-keys targets only the
  # intended pane and doesn't echo commands into all panes.
  tmux set-window-option -t "$target_window" synchronize-panes off 2>/dev/null || true

  # Create the splits; we'll explicitly send a `cd` to each pane so they end
  # up in the desired working directory regardless of tmux version.
  right_pane="$(tmux split-window -h -p 50 -P -F '#{pane_id}' -t "$target_window")"
  bottom_right_pane="$(tmux split-window -v -p 33 -P -F '#{pane_id}' -t "$right_pane")"

  if [[ -n "$pane_dir" ]]; then
    # Wait for each pane to be ready (has a pane_pid) before sending keys.
    local escaped_dir
    printf -v escaped_dir '%q' "$pane_dir"
    for p in "$left_pane" "$right_pane" "$bottom_right_pane"; do
      local tries=0
      while ! tmux display-message -p -t "$p" '#{pane_pid}' >/dev/null 2>&1; do
        sleep 0.01
        tries=$((tries + 1))
        if [[ $tries -gt 200 ]]; then
          break
        fi
      done
      tmux send-keys -t "$p" "cd $escaped_dir" C-m
    done
  fi

  if [[ -n "$left_cmd" ]]; then
    tmux send-keys -t "$left_pane" "$left_cmd" C-m
  fi

  if [[ -n "$top_right_cmd" ]]; then
    tmux send-keys -t "$right_pane" "$top_right_cmd" C-m
  fi

  tmux select-pane -t "$left_pane"
}

# If the session already exists, attach to it
if tmux has-session -t "$SESSION" 2>/dev/null; then
  tmux attach-session -t "$SESSION"
  exit 0
fi

windows=()
dirs=()
left_cmds=()
top_cmds=()
created_with_c=()

if [[ -f "$CONFIG_FILE" ]]; then
  while IFS='|' read -r name dir left_cmd top_cmd; do
    if [[ -z "$name" || "$name" == \#* ]]; then
      continue
    fi

    dir="$(normalize_dir "${dir:-}")"

    windows+=("$name")
    dirs+=("$dir")
    left_cmds+=("$left_cmd")
    top_cmds+=("$top_cmd")

    if [[ "$VERBOSE" -eq 1 ]]; then
      if [[ -n "${dir:-}" && ! -d "$dir" ]]; then
        echo "[tmux.sh] WARNING: directory does not exist: $dir"
      fi
      echo "[tmux.sh] Parsed window: name='$name' dir='${dir:-}' left='${left_cmd:-}' top='${top_cmd:-}'"
    fi
  done < "$CONFIG_FILE"
fi

# If no explicit config file exists, check for the example file and use it
if [[ ${#windows[@]} -eq 0 && -f "$SCRIPT_DIR/tmux.windows.conf.example" ]]; then
  CONFIG_FILE="$SCRIPT_DIR/tmux.windows.conf.example"
  if [[ "$VERBOSE" -eq 1 ]]; then
    echo "[tmux.sh] No config provided; falling back to example: $CONFIG_FILE"
  fi
  while IFS='|' read -r name dir left_cmd top_cmd; do
    if [[ -z "$name" || "$name" == \#* ]]; then
      continue
    fi
    dir="$(normalize_dir "${dir:-}")"
    windows+=("$name")
    dirs+=("$dir")
    left_cmds+=("$left_cmd")
    top_cmds+=("$top_cmd")
    if [[ "$VERBOSE" -eq 1 ]]; then
      if [[ -n "${dir:-}" && ! -d "$dir" ]]; then
        echo "[tmux.sh] WARNING: directory does not exist: $dir"
      fi
      echo "[tmux.sh] Parsed example window: name='$name' dir='${dir:-}' left='${left_cmd:-}' top='${top_cmd:-}'"
    fi
  done < "$CONFIG_FILE"
fi

# Verbose feedback about config parsing
if [[ "$VERBOSE" -eq 1 ]]; then
  if [[ -f "$CONFIG_FILE" ]]; then
    echo "[tmux.sh] Using config file: $CONFIG_FILE"
    echo "[tmux.sh] Parsed ${#windows[@]} windows"
  else
    echo "[tmux.sh] No config file found at: $CONFIG_FILE"
  fi
fi

if [[ ${#windows[@]} -eq 0 ]]; then
  windows=("$DEFAULT_WINDOW")
  dirs=("$HOME/.config/opencode")
  left_cmds=("opencode -c")
  top_cmds=("wl tui")
fi

# Ensure the first window (Agents) uses the user's opencode config dir.
# If the first window is the default "Agents" window and no explicit dir
# was provided in the config, make it work in $HOME/.config/opencode.
if [[ "${windows[0]:-}" == "$DEFAULT_WINDOW" ]]; then
  dirs[0]="${dirs[0]:-$HOME/.config/opencode}"
fi

# Start a new detached session with a single pane (use -c to set starting dir
# when supported). If dirs[0] is set, try to pass it to tmux; fall back to
# default behaviour otherwise.
if [[ -n "${dirs[0]:-}" ]]; then
  if tmux new-session -d -s "$SESSION" -n "${windows[0]}" -c "${dirs[0]}" 2>/dev/null; then
    created_with_c[0]=1
  else
    tmux new-session -d -s "$SESSION" -n "${windows[0]}"
    created_with_c[0]=0
  fi
else
  tmux new-session -d -s "$SESSION" -n "${windows[0]}"
  created_with_c[0]=0
fi


# Tmux appearance configuration
tmux set -g base-index 1
tmux setw -g window-status-current-style fg=black,bg=green
tmux set -g window-style bg=colour235
tmux set -g window-active-style bg=colour234

# Default pane border colors
tmux set -g pane-border-style fg=colour238
tmux set -g pane-active-border-style fg=colour45
# Ensure tmux copies to the system clipboard when using copy-mode/yank.
# Make this conditional: older tmux versions do not support `set-clipboard` and
# will error. Detect support before setting so the script remains compatible.
if tmux show-options -g | grep -q '^set-clipboard'; then
  tmux set -g set-clipboard on
else
  # Fallback: enable mouse-based copy selection which works with terminal
  # integrations and avoids failures on older tmux releases.
  tmux set -g mouse on || true
fi

set_window_colors() {
  local target="$1"
  local border="$2"
  local active="$3"

  tmux set -w -t "$target" pane-border-style "fg=$border"
  tmux set -w -t "$target" pane-active-border-style "fg=$active"
}

apply_window_colors() {
  local index="$1"
  local target="$2"

  case "$index" in
    1) set_window_colors "$target" colour28 colour46 ;;
    2) set_window_colors "$target" colour160 colour196 ;;
    3) set_window_colors "$target" colour26 colour39 ;;
    4) set_window_colors "$target" colour94 colour130 ;;
    5) set_window_colors "$target" colour61 colour98 ;;
    6) set_window_colors "$target" colour22 colour34 ;;
    7) set_window_colors "$target" colour52 colour88 ;;
    8) set_window_colors "$target" colour17 colour25 ;;
    9) set_window_colors "$target" colour178 colour214 ;;
    10) set_window_colors "$target" colour24 colour37 ;;
    *) set_window_colors "$target" colour238 colour45 ;;
  esac
}

create_three_pane_layout "$SESSION:${windows[0]}" "${left_cmds[0]}" "${top_cmds[0]}" "${dirs[0]}" "${created_with_c[0]:-0}"
apply_window_colors 1 "$SESSION:${windows[0]}"

for i in "${!windows[@]}"; do
  if [[ $i -eq 0 ]]; then
    continue
  fi

  # Create the window; try to set its starting directory if provided
  if [[ -n "${dirs[$i]:-}" ]]; then
    if tmux new-window -t "$SESSION" -n "${windows[$i]}" -c "${dirs[$i]}" 2>/dev/null; then
      created_with_c[$i]=1
    else
      tmux new-window -t "$SESSION" -n "${windows[$i]}"
      created_with_c[$i]=0
    fi
  else
    tmux new-window -t "$SESSION" -n "${windows[$i]}"
    created_with_c[$i]=0
  fi
  create_three_pane_layout "$SESSION:${windows[$i]}" "${left_cmds[$i]}" "${top_cmds[$i]}" "${dirs[$i]}" "${created_with_c[$i]:-0}"
  apply_window_colors $((i + 1)) "$SESSION:${windows[$i]}"
done

# Attach to the session
tmux attach-session -t "$SESSION"
