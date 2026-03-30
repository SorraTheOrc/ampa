"""Public notification API for AMPA.

This module provides a single entry point — :func:`notify` — that all AMPA
modules should call to send Discord messages.  Internally it routes messages
to the Discord bot via a Unix domain socket.

If the socket is unreachable the message is dead-lettered to a local file so
nothing is silently lost.

State tracking
--------------
Each successful send records ``last_message_ts`` and ``last_message_type`` in a
state file.  The daemon's heartbeat suppression logic reads this state to avoid
sending redundant heartbeats when a non-heartbeat message was already sent
recently.
"""

from __future__ import annotations

import datetime
import json
import logging
import os
import socket
import tempfile
import time
from typing import Any, Dict, List, Optional

LOG = logging.getLogger("ampa.notifications")

# Default Unix socket path — must match the bot's default.
DEFAULT_SOCKET_PATH = "/tmp/ampa_bot.sock"

# Socket connect + send timeout in seconds.
SOCKET_TIMEOUT = 10

# Retry / backoff defaults — overridable via environment variables.
DEFAULT_MAX_RETRIES = 10
DEFAULT_BACKOFF_BASE_SECONDS = 2.0


# ---------------------------------------------------------------------------
# State helpers — the state-file contract is preserved for backward
# compatibility.
# ---------------------------------------------------------------------------


def _read_state(path: str) -> Dict[str, str]:
    try:
        with open(path, "r", encoding="utf-8") as fh:
            return json.load(fh)
    except Exception:
        return {}


def _write_state(path: str, data: Dict[str, str]) -> None:
    try:
        with open(path, "w", encoding="utf-8") as fh:
            json.dump(data, fh)
    except Exception:
        LOG.exception("Failed to write state file %s", path)


def _state_file_path() -> str:
    return os.getenv("AMPA_STATE_FILE") or os.path.join(
        tempfile.gettempdir(), "ampa_state.json"
    )


# ---------------------------------------------------------------------------
# Dead-letter
# ---------------------------------------------------------------------------


def _default_deadletter_path() -> str:
    """Return the default dead-letter file path.

    Uses ``<cwd>/.worklog/ampa/deadletter.log`` so the file is project-local,
    discoverable alongside other AMPA state files, and writable by non-root
    users.  The daemon is always spawned with ``cwd = projectRoot`` (see
    ampa.mjs) so ``os.getcwd()`` gives the correct project root.
    """
    return os.path.join(os.getcwd(), ".worklog", "ampa", "deadletter.log")


def dead_letter(payload: Dict[str, Any], reason: Optional[str] = None) -> None:
    """Persist a failed notification so it is not silently lost.

    Writes to ``AMPA_DEADLETTER_FILE`` (default
    ``<projectRoot>/.worklog/ampa/deadletter.log``).
    """
    try:
        try:
            payload_str = json.dumps(payload)
        except Exception:
            payload_str = str(payload)
        LOG.error(
            "dead_letter invoked: reason=%s payload=%s",
            reason,
            payload_str[:1000],
        )
        record = {
            "ts": datetime.datetime.now(datetime.timezone.utc).isoformat(),
            "reason": reason,
            "payload": payload,
        }

        # If a dead-letter webhook is configured, try to POST the record there.
        webhook = os.getenv("AMPA_DEADLETTER_WEBHOOK")
        if webhook:
            # Import requests lazily so the package remains optional.
            session = None
            try:
                import requests  # type: ignore

                session = requests.Session()
                session.trust_env = False

                # Use a small, bounded retry/backoff when posting dead letters so
                # transient network issues have a chance to recover without
                # blocking the caller for too long. Reuse the general retry
                # configuration but keep attempts conservative (cap at 3).
                try:
                    max_retries, backoff_base = _retry_config()
                    web_retries = min(max_retries, 3)
                except Exception:
                    web_retries, backoff_base = 3, 2.0

                for attempt in range(1, web_retries + 1):
                    try:
                        resp = session.post(webhook, json=record, timeout=5)
                        resp.raise_for_status()
                        LOG.info(
                            "dead_letter: posted failure to dead-letter webhook %s (attempt %d/%d)",
                            webhook,
                            attempt,
                            web_retries,
                        )
                        return
                    except Exception:
                        LOG.exception(
                            "dead_letter: dead-letter webhook POST failed (attempt %d/%d), webhook=%s",
                            attempt,
                            web_retries,
                            webhook,
                        )
                        if attempt < web_retries:
                            try:
                                backoff = backoff_base * (2 ** (attempt - 1))
                                time.sleep(backoff)
                            except Exception:
                                pass
                # If we get here, all webhook attempts failed — fall through to
                # the file fallback below.
            except ImportError:
                LOG.debug(
                    "dead_letter: requests library not available, falling back to file"
                )
            except Exception:
                LOG.exception(
                    "dead_letter: unexpected error while attempting dead-letter webhook, falling back to file"
                )
            finally:
                try:
                    if session is not None:
                        session.close()
                except Exception:
                    pass

        # Fallback: append to the dead-letter file.
        dl_file = os.getenv("AMPA_DEADLETTER_FILE") or _default_deadletter_path()
        try:
            parent = os.path.dirname(dl_file)
            if parent and not os.path.isdir(parent):
                try:
                    os.makedirs(parent, exist_ok=True)
                except Exception:
                    pass
            with open(dl_file, "a", encoding="utf-8") as fh:
                fh.write(json.dumps(record) + "\n")
            LOG.info("dead_letter: appended failure record to %s", dl_file)
        except Exception:
            LOG.exception("dead_letter: failed to write dead-letter file %s", dl_file)
    except Exception:
        LOG.exception("dead_letter: unexpected error while handling dead letter")


# ---------------------------------------------------------------------------
# Payload builders — callers can use these to build the message content
# and then pass the result to notify().
# ---------------------------------------------------------------------------


def _truncate_output(output: str, limit: int = 900) -> str:
    if len(output) <= limit:
        return output
    return output[:limit] + "\n... (truncated)"


def build_payload(
    hostname: str,
    timestamp_iso: str,
    work_item_id: Optional[str] = None,
    extra_fields: Optional[List[Dict[str, Any]]] = None,
    title: str = "AMPA Heartbeat",
) -> Dict[str, Any]:
    """Build a simple markdown payload (same output as the legacy ``build_payload``).

    Returns ``{"content": "<markdown>"}`` — compatible with the bot socket
    protocol.
    """
    heading = f"# {title}"
    body: List[str] = []
    if extra_fields:
        for field in extra_fields:
            name = field.get("name")
            value = field.get("value")
            if name and value is not None:
                body.append(f"{name}: {value}")
    if body:
        content = heading + "\n\n" + "\n".join(body)
    else:
        content = heading
    return {"content": content}


def build_command_payload(
    hostname: str,
    timestamp_iso: str,
    command_id: Optional[str],
    output: Optional[str],
    exit_code: Optional[int],
    title: str = "AMPA Heartbeat",
) -> Dict[str, Any]:
    """Build a command-oriented payload (same as the legacy ``build_command_payload``).

    Returns ``{"content": "<markdown>"}``.
    """
    heading = f"# {title}" if title else "# AMPA Notification"
    body: List[str] = []
    if output:
        body.append(_truncate_output(output, limit=1000))
    if body:
        content = heading + "\n\n" + "\n".join(body)
    else:
        content = heading
    return {"content": content}


# ---------------------------------------------------------------------------
# Core notification function
# ---------------------------------------------------------------------------


def notify(
    title: str,
    body: str = "",
    message_type: str = "other",
    *,
    payload: Optional[Dict[str, Any]] = None,
    components: Optional[List[Dict[str, Any]]] = None,
    channel_id: Optional[int] = None,
) -> bool:
    """Send a notification to Discord via the bot's Unix socket.

    Parameters
    ----------
    title:
        The heading / title for the notification.
    body:
        The body text (markdown).
    message_type:
        A label for the kind of notification (``heartbeat``, ``command``,
        ``startup``, ``error``, ``completion``, ``warning``,
        ``waiting_for_input``, ``engine``, ``other``).  Used for state
        tracking and heartbeat suppression — not sent to Discord directly.
    payload:
        Optional pre-built payload dict.  If provided, this is sent directly
        to the bot socket (must contain ``content`` or ``title``/``body``).
        When *payload* is supplied, *title* and *body* are ignored.
    components:
        Optional list of component dicts to attach interactive UI elements
        (buttons) to the Discord message.  Each dict should have ``type``,
        ``label``, ``custom_id``, and optionally ``style``.  Ignored when
        *payload* is supplied (include ``components`` in the payload instead).

    Returns
    -------
    bool
        ``True`` if the message was accepted by the bot; ``False`` if the
        message was dead-lettered.  Returns ``True`` immediately if
        ``AMPA_DISABLE_DISCORD`` is set (no-op for CI).

    Notes
    -----
    Environment variables
    - ``AMPA_DISABLE_DISCORD``: if set (any non-empty value), ``notify()``
      becomes a no-op and returns ``True`` immediately.  Useful for CI runs
      where you want to exercise the notification path without posting to
      Discord.
    """
    socket_path = os.getenv("AMPA_BOT_SOCKET_PATH", DEFAULT_SOCKET_PATH)

    # Build the payload to send over the socket.
    if payload is not None:
        msg = dict(payload)
    else:
        msg = {}
        if title and body:
            msg["content"] = f"# {title}\n\n{body}"
        elif title:
            msg["content"] = f"# {title}"
        elif body:
            msg["content"] = body
        else:
            LOG.warning("notify() called with empty title and body – skipping")
            return False
        # Attach components when building from title/body (not from payload).
        if components:
            msg["components"] = components
    # Auto-detect CI/test environment and route to test channel
    # Check for common CI environment variables or pytest
    is_ci_env = (
        os.getenv("CI") == "true" or
        os.getenv("PYTEST_CURRENT_TEST") is not None or
        os.getenv("GITHUB_ACTIONS") == "true" or
        os.getenv("GITLAB_CI") == "true" or
        os.getenv("CIRCLECI") == "true" or
        os.getenv("JENKINS_URL") is not None or
        os.getenv("TRAVIS") == "true"
    )
    if is_ci_env and message_type not in ("test", "ci"):
        message_type = "ci"
    
    msg["message_type"] = message_type

    # CI / dry-run support: if AMPA_DISABLE_DISCORD is set, skip sending and
    # return True (no-op). This prevents CI from actually posting to Discord.
    disable_discord = os.getenv("AMPA_DISABLE_DISCORD")
    if disable_discord is not None:
        LOG.debug(
            "notify: AMPA_DISABLE_DISCORD=%s, skipping Discord send (message_type=%s)",
            disable_discord,
            message_type,
        )
        return True

    # Optional per-message channel override (forwarded to the bot socket).
    # Keep backward compatibility: callers that do not pass channel_id are
    # unaffected.
    if channel_id is not None:
        try:
            # Store as int when possible, but allow string-y values from callers
            # (the bot accepts either and attempts resolution).
            msg["channel_id"] = int(channel_id)
        except Exception:
            msg["channel_id"] = channel_id
        # Log when a non-default channel is explicitly requested so operators
        # can audit test/CI messages.
        default_chan = os.getenv("AMPA_DISCORD_CHANNEL_ID")
        try:
            if default_chan is None or str(channel_id) != str(default_chan):
                LOG.info(
                    "notify: forwarding to non-default channel_id=%s message_type=%s",
                    channel_id,
                    message_type,
                )
        except Exception:
            # Avoid raising from logging logic.
            pass

    # Try to send via Unix socket.
    # Log the exact payload we're about to send so operators can audit what
    # the scheduler is instructing the bot to post. This helps diagnose
    # misrouting when messages unexpectedly appear in the test channel.
    try:
        # Diagnostic payload logging should be at DEBUG in normal runs so
        # message content isn't emitted to default INFO logs.  Use DEBUG to
        # preserve the ability to audit payloads when the operator enables
        # detailed logging.
        LOG.debug(
            "notify: sending payload to bot socket %s: %s",
            socket_path,
            json.dumps(msg),
        )
    except Exception:
        # Avoid raising from logging; fall back to a concise message.
        LOG.debug("notify: sending payload to bot socket %s (payload redacted due to logging error)", socket_path)

    ok = _send_via_socket(socket_path, msg)

    # Update state file regardless of success/failure (matches legacy behavior
    # where state was updated even on failed attempts).
    state_file = _state_file_path()
    try:
        _write_state(
            state_file,
            {
                "last_message_ts": datetime.datetime.now(
                    datetime.timezone.utc
                ).isoformat(),
                "last_message_type": message_type,
            },
        )
    except Exception:
        LOG.debug("Failed to update state after notify()")

    if not ok:
        dead_letter(msg, reason="Unix socket unreachable")
        return False

    return True


def _retry_config() -> tuple:
    """Return ``(max_retries, backoff_base)`` from env vars or defaults.

    Environment variables:
    - ``AMPA_MAX_RETRIES`` — maximum number of send attempts (default 10,
      minimum 1).
    - ``AMPA_BACKOFF_BASE_SECONDS`` — base delay in seconds for exponential
      backoff (default 2.0, must be > 0).
    """
    try:
        max_retries = int(os.getenv("AMPA_MAX_RETRIES", str(DEFAULT_MAX_RETRIES)))
        if max_retries < 1:
            max_retries = 1
    except (TypeError, ValueError):
        max_retries = DEFAULT_MAX_RETRIES

    try:
        backoff_base = float(
            os.getenv("AMPA_BACKOFF_BASE_SECONDS", str(DEFAULT_BACKOFF_BASE_SECONDS))
        )
        if backoff_base <= 0:
            backoff_base = DEFAULT_BACKOFF_BASE_SECONDS
    except (TypeError, ValueError):
        backoff_base = DEFAULT_BACKOFF_BASE_SECONDS

    return max_retries, backoff_base


def _send_via_socket(socket_path: str, msg: Dict[str, Any]) -> bool:
    """Send a single JSON message to the bot via Unix socket with retries.

    Retries with exponential backoff on connection/send failures up to
    ``AMPA_MAX_RETRIES`` attempts.  Each failed attempt is logged with its
    attempt number and the error encountered.  A final failure after all
    retries are exhausted is logged at ERROR level.

    Returns ``True`` if the bot acknowledged the message, ``False`` otherwise.
    """
    max_retries, backoff_base = _retry_config()

    last_error: str = ""
    for attempt in range(1, max_retries + 1):
        sock = None
        try:
            sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
            sock.settimeout(SOCKET_TIMEOUT)
            sock.connect(socket_path)

            line = json.dumps(msg) + "\n"
            sock.sendall(line.encode("utf-8"))

            # Read the response line.
            data = b""
            while b"\n" not in data:
                chunk = sock.recv(4096)
                if not chunk:
                    break
                data += chunk

            if not data:
                last_error = "empty response from bot socket"
                LOG.warning(
                    "Send attempt %d/%d failed: %s",
                    attempt,
                    max_retries,
                    last_error,
                )
                if attempt < max_retries:
                    backoff = backoff_base * (2 ** (attempt - 1))
                    time.sleep(backoff)
                continue

            resp = json.loads(data.strip())
            if resp.get("ok"):
                LOG.debug("Notification sent successfully via bot socket")
                return True
            else:
                last_error = "bot returned error: %s" % resp.get("error", "unknown")
                LOG.warning(
                    "Send attempt %d/%d failed: %s",
                    attempt,
                    max_retries,
                    last_error,
                )
                if attempt < max_retries:
                    backoff = backoff_base * (2 ** (attempt - 1))
                    time.sleep(backoff)
                continue

        except FileNotFoundError:
            last_error = "socket not found at %s" % socket_path
            LOG.warning(
                "Send attempt %d/%d failed: %s",
                attempt,
                max_retries,
                last_error,
            )
        except ConnectionRefusedError:
            last_error = "connection refused at %s" % socket_path
            LOG.warning(
                "Send attempt %d/%d failed: %s",
                attempt,
                max_retries,
                last_error,
            )
        except OSError as exc:
            last_error = "socket error: %s" % exc
            LOG.warning(
                "Send attempt %d/%d failed: %s",
                attempt,
                max_retries,
                last_error,
            )
        except Exception as exc:
            last_error = "unexpected error: %s" % exc
            LOG.warning(
                "Send attempt %d/%d failed: %s",
                attempt,
                max_retries,
                last_error,
            )
        finally:
            if sock is not None:
                try:
                    sock.close()
                except Exception:
                    pass

        # Sleep before next retry (but not after the last attempt).
        if attempt < max_retries:
            backoff = backoff_base * (2 ** (attempt - 1))
            time.sleep(backoff)

    # All retries exhausted.
    LOG.error(
        "All %d send attempts failed. Last error: %s",
        max_retries,
        last_error,
    )
    return False
