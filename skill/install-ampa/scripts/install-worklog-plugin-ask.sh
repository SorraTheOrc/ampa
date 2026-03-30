#!/usr/bin/env sh
# Interactive wrapper around install-worklog-plugin.sh to request Discord bot config
set -eu

SCRIPT_DIR=$(dirname "$0")
INSTALL_SH="$SCRIPT_DIR/install-worklog-plugin.sh"

if [ ! -x "$INSTALL_SH" ] && [ ! -f "$INSTALL_SH" ]; then
  echo "installer not found: $INSTALL_SH" >&2
  exit 2
fi

BOT_TOKEN=""
CHANNEL_ID=""
AUTO_YES=0
EXTRA_ARGS=

while [ "$#" -gt 0 ]; do
  case "$1" in
    --yes|-y)
      AUTO_YES=1
      shift
      ;;
    --bot-token)
      shift
      BOT_TOKEN="$1"
      shift
      ;;
    --channel-id)
      shift
      CHANNEL_ID="$1"
      shift
      ;;
    --help|-h)
      echo "Usage: $0 [--bot-token <token>] [--channel-id <id>] [--yes] [-- <installer-args>]"
      exit 0
      ;;
    --)
      shift
      EXTRA_ARGS="$*"
      break
      ;;
    *)
      # pass-through positional arguments to installer
      EXTRA_ARGS="$EXTRA_ARGS $1"
      shift
      ;;
  esac
done

if [ -z "$BOT_TOKEN" ] && [ "$AUTO_YES" -ne 1 ]; then
  printf "Enter Discord bot token to use for AMPA notifications (leave empty to skip): \n> "
  if ! read -r BOT_TOKEN; then BOT_TOKEN=""; fi
  BOT_TOKEN=$(printf "%s" "$BOT_TOKEN" | sed 's/^\s*//;s/\s*$//')
fi

if [ -z "$CHANNEL_ID" ] && [ -n "$BOT_TOKEN" ] && [ "$AUTO_YES" -ne 1 ]; then
  printf "Enter Discord channel ID for AMPA notifications: \n> "
  if ! read -r CHANNEL_ID; then CHANNEL_ID=""; fi
  CHANNEL_ID=$(printf "%s" "$CHANNEL_ID" | sed 's/^\s*//;s/\s*$//')
fi

CMD="$INSTALL_SH"
if [ -n "$BOT_TOKEN" ]; then
  CMD="$CMD --bot-token $BOT_TOKEN"
fi
if [ -n "$CHANNEL_ID" ]; then
  CMD="$CMD --channel-id $CHANNEL_ID"
fi

if [ -n "$EXTRA_ARGS" ]; then
  CMD="$CMD $EXTRA_ARGS"
fi

echo "Running installer..."
sh -c "$CMD"
