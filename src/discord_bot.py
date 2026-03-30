"""Discord bot process for AMPA notifications.

This module implements a Discord bot that listens on a Unix domain socket for
incoming notification requests and sends them as messages to a configured
Discord channel.

Usage::

    python -m ampa.discord_bot

Environment variables:

- ``AMPA_DISCORD_BOT_TOKEN``  – Discord bot token (required)
- ``AMPA_DISCORD_CHANNEL_ID`` – Target channel ID as an integer (required)
- ``AMPA_DISCORD_TEST_CHANNEL_ID`` – Test channel ID as an integer. When set,
  messages without a per-message ``channel_id`` override will be sent to this
  test channel instead of the default ``AMPA_DISCORD_CHANNEL_ID``. Useful for
  routing test/CI notifications to a separate channel.
- ``AMPA_BOT_SOCKET_PATH``    – Unix socket path (default: ``/tmp/ampa_bot.sock``)
- ``AMPA_DISABLE_DISCORD``    – If set (any non-empty value), the bot will
  skip sending messages to Discord and log a debug message instead. Useful
  for local testing or CI scenarios where you want to exercise the socket
  protocol without actually posting to Discord. When this is set, the bot
  will still accept connections and respond ``{"ok": true}`` to requests,
  but no messages will be sent to Discord.

    The bot accepts newline-delimited JSON messages on the Unix socket.  Each
    message must be a JSON object; it is sent to the configured Discord channel.
    Backwards-compatible plain-text messages use the ``content`` field
    (payload format ``{"content": "..."}``).
    
    The bot also supports richer payloads via optional ``embeds`` (list of
    embed objects) and ``components`` (interactive buttons).  When ``embeds`` is
    present the message will be sent with embeds where the bot can construct
    ``discord.Embed`` objects.  For compatibility the server will accept embed
    dicts and attempt to build proper Embed objects when ``discord`` is
    available.

Protocol
--------
Each client connection may send one or more JSON messages separated by
newlines.  The bot reads each line, deserializes it, and sends it to Discord.
A JSON response is written back per message::

    {"ok": true}           # success
    {"ok": false, "error": "..."} # failure

The connection is closed by the client when done.

Component protocol extension
-----------------------------
Messages may include an optional ``components`` list to attach interactive
UI elements (buttons) to the Discord message.  Each component object must
have ``type``, ``label``, and ``custom_id`` fields.  ``style`` is optional
and defaults to ``secondary``.  Example::

    {
      "content": "Pick a colour",
      "components": [
        {"type": "button", "label": "Blue", "style": "primary", "custom_id": "test_blue"},
        {"type": "button", "label": "Red",  "style": "danger",  "custom_id": "test_red"}
      ]
    }

When ``components`` is absent or empty, messages are sent as plain text
(backward-compatible).
"""

from __future__ import annotations

import asyncio
import datetime
import json
import logging
import os
import signal
import subprocess
import sys
from typing import Any, Dict, List, Optional

LOG = logging.getLogger("ampa.discord_bot")

# Style name -> discord.ButtonStyle mapping (resolved at runtime when discord
# is imported).  Unknown styles fall back to ``secondary``.
_BUTTON_STYLE_MAP: Dict[str, Any] = {}  # populated in run()

# Required fields for a valid component object.
_COMPONENT_REQUIRED_FIELDS = {"type", "label", "custom_id"}


# ---------------------------------------------------------------------------
# Component helpers
# ---------------------------------------------------------------------------


def _validate_components(components: Any) -> Optional[str]:
    """Return an error string if *components* is malformed, else ``None``."""
    if not isinstance(components, list):
        return "components must be a list"
    for idx, comp in enumerate(components):
        if not isinstance(comp, dict):
            return f"components[{idx}] must be an object"
        missing = _COMPONENT_REQUIRED_FIELDS - set(comp.keys())
        if missing:
            return f"components[{idx}] missing required fields: {sorted(missing)}"
        if comp.get("type") != "button":
            return f"components[{idx}] unsupported type: {comp.get('type')}"
    return None


def _validate_embeds(embeds: Any) -> Optional[str]:
    """Validate the embed payload format. Returns error string or None."""
    if not isinstance(embeds, list):
        return "embeds must be a list"
    for idx, e in enumerate(embeds):
        if not isinstance(e, dict):
            return f"embeds[{idx}] must be an object"
        # permit minimal embed dicts; keys like title/description/url/color/fields
        if "fields" in e and not isinstance(e["fields"], list):
            return f"embeds[{idx}].fields must be a list"
    return None


def _build_view(components: List[Dict[str, Any]]) -> Any:
    """Construct a ``discord.ui.View`` from a list of component dicts.

    Each component must have ``type``, ``label``, ``custom_id``, and
    optionally ``style`` (defaults to ``secondary``).  Unknown style
    values fall back to ``secondary`` with a warning.

    The returned ``View`` has ``timeout=None`` so buttons remain
    clickable until the bot restarts.
    """
    import discord  # type: ignore

    view = discord.ui.View(timeout=None)
    for comp in components:
        style_name = comp.get("style", "secondary")
        style = _BUTTON_STYLE_MAP.get(style_name)
        if style is None:
            LOG.warning(
                "Unknown button style %r for custom_id=%s; defaulting to secondary",
                style_name,
                comp.get("custom_id"),
            )
            style = discord.ButtonStyle.secondary
        button = discord.ui.Button(
            label=comp["label"],
            style=style,
            custom_id=comp["custom_id"],
        )
        view.add_item(button)
    return view


def _build_embeds(embeds: List[Dict[str, Any]]) -> List[Any]:
    """Convert embed dicts into discord.Embed objects when possible.

    If discord is not available, return the original dicts as a fallback so
    tests and non-discord runs can still pass the payload through.
    """
    try:
        import discord  # type: ignore
    except Exception:
        return embeds

    out: List[Any] = []
    for e in embeds:
        title = e.get("title")
        description = e.get("description")
        url = e.get("url")
        color = e.get("color")
        try:
            embed_obj = discord.Embed(title=title, description=description, url=url)
            if color is not None:
                try:
                    embed_obj.colour = discord.Colour(int(color))
                except Exception:
                    # accept hex ints like 0x123456 or decimal ints
                    try:
                        embed_obj.colour = discord.Colour(int(color))
                    except Exception:
                        pass
            # fields
            fields = e.get("fields") or []
            for f in fields:
                fname = f.get("name")
                fval = f.get("value")
                finline = bool(f.get("inline", False))
                if fname is not None and fval is not None:
                    embed_obj.add_field(name=fname, value=fval, inline=finline)
            out.append(embed_obj)
        except Exception:
            # On any failure, fall back to the raw dict for compatibility
            out.append(e)
    return out


def _route_interaction(custom_id: str, user: str, timestamp: str) -> None:
    """Dispatch a button interaction through conversation_manager plumbing.

    For the MVP, interactions with ``test_*`` custom_id prefixes are treated
    as no-ops (acknowledge only, no session started or resumed).

    PR review interactions (``pr_review_approve_*`` / ``pr_review_reject_*``)
    are routed through ``responder.resume_from_payload()`` to trigger
    merge or rejection workflows.

    This function is called *before* the interaction acknowledgement is sent,
    so it must return quickly (well under the 3-second Discord timeout).
    """
    if custom_id.startswith("test_"):
        LOG.debug(
            "No-op route for test interaction: custom_id=%s user=%s ts=%s",
            custom_id,
            user,
            timestamp,
        )
        return

    # PR review approve/reject routing
    if custom_id.startswith("pr_review_approve_") or custom_id.startswith(
        "pr_review_reject_"
    ):
        _route_pr_review_interaction(custom_id, user, timestamp)
        return

    LOG.info(
        "Unrecognised interaction: custom_id=%s user=%s ts=%s",
        custom_id,
        user,
        timestamp,
    )


def _route_pr_review_interaction(
    custom_id: str, user: str, timestamp: str
) -> None:
    """Route pr_review_approve_* / pr_review_reject_* interactions.

    Calls ``responder.resume_from_payload()`` with the appropriate action
    and metadata.  Errors are caught and logged so the bot never crashes.
    """
    try:
        from . import responder
    except ImportError:
        LOG.error("Cannot import responder — PR review routing unavailable")
        return

    is_approve = custom_id.startswith("pr_review_approve_")
    # Extract the PR number from the custom_id suffix
    prefix = "pr_review_approve_" if is_approve else "pr_review_reject_"
    pr_number_str = custom_id[len(prefix):]
    try:
        pr_number = int(pr_number_str)
    except (ValueError, TypeError):
        LOG.error(
            "Invalid PR number in custom_id=%s — cannot route", custom_id
        )
        return

    action = "accept" if is_approve else "decline"
    session_id = f"pr-review-{pr_number}"

    LOG.info(
        "Routing PR review interaction: custom_id=%s action=%s "
        "pr_number=%d user=%s",
        custom_id,
        action,
        pr_number,
        user,
    )

    try:
        resume_result = responder.resume_from_payload({
            "session_id": session_id,
            "action": action,
            "metadata": {
                "pr_number": pr_number,
                "approved_by": user,
                "timestamp": timestamp,
                "source": "discord_button",
            },
        })

        # Execute merge/reject workflow directly in daemon process after
        # successful session resume.
        if not isinstance(resume_result, dict) or resume_result.get("status") != "resumed":
            return

        # Execute merge/reject workflow directly in daemon process.
        work_item_id = None
        context = resume_result.get("context")
        if isinstance(context, list) and context:
            first = context[0]
            if isinstance(first, dict):
                work_item_id = first.get("work_item_id")

        from .pr_monitor import PRMonitorRunner

        runner = PRMonitorRunner(
            run_shell=subprocess.run,
            command_cwd=os.getcwd(),
        )
        runner.handle_review_decision(
            action=action,
            pr_number=pr_number,
            work_item_id=work_item_id,
            approved_by=user,
        )
    except Exception:
        LOG.exception(
            "PR review routing failed for custom_id=%s pr_number=%d",
            custom_id,
            pr_number,
        )


async def process_interaction(interaction: object) -> None:
    """Process a discord.Interaction: route and acknowledge.

    Separated from the client event so tests can call it directly without
    wrapping in the discord.py event system.
    """
    try:
        import discord  # type: ignore
    except Exception:
        # If discord isn't available, we can't inspect types — attempt best-effort
        # access to expected attributes and fail gracefully in tests.
        discord = None  # type: ignore

    # Only handle component (button) interactions.
    if discord is not None:
        if getattr(interaction, "type", None) != discord.InteractionType.component:
            LOG.debug(
                "Ignoring non-component interaction type=%s",
                getattr(interaction, "type", None),
            )
            return

    custom_id = (
        interaction.data.get("custom_id", "")
        if getattr(interaction, "data", None)
        else ""
    )
    user = getattr(interaction, "user", None)
    user_str = (
        f"{getattr(user, 'name', 'unknown')}#{getattr(user, 'discriminator', '')}"
        if user
        else "unknown"
    )
    now_iso = datetime.datetime.now(datetime.timezone.utc).isoformat()

    # Route through conversation_manager plumbing (no-op for test_*).
    _route_interaction(custom_id, user_str, now_iso)

    # Derive a human-readable label from the custom_id.
    # Convention: custom_id is "prefix_label", e.g. "test_blue" -> "Blue".
    label = custom_id.rsplit("_", 1)[-1].capitalize() if custom_id else "Unknown"

    # Warn if we couldn't parse a conventional prefix_label format but a
    # non-empty custom_id was provided.  This helps surface telemetry when
    # producers send unexpected custom_id values.
    if custom_id and "_" not in custom_id:
        LOG.warning(
            "Could not derive label from custom_id=%r; using %r",
            custom_id,
            label,
        )

    ack_message = f"You selected {label}, good luck. (clicked by {user_str}, {now_iso})"
    try:
        # interaction.response.send_message is an async callable on real Interaction
        await interaction.response.send_message(ack_message)  # type: ignore
        LOG.info("Acknowledged button click: custom_id=%s user=%s", custom_id, user_str)
    except Exception:
        LOG.exception("Failed to acknowledge interaction custom_id=%s", custom_id)


# Default socket path
DEFAULT_SOCKET_PATH = "/tmp/ampa_bot.sock"

# Maximum message size we'll accept on the socket (64 KiB).
MAX_MESSAGE_SIZE = 65_536


class AMPABot:
    """Thin wrapper around a ``discord.Client`` that also runs a Unix socket
    server for receiving notification requests from synchronous callers."""

    def __init__(
        self,
        token: str,
        channel_id: int,
        socket_path: str = DEFAULT_SOCKET_PATH,
    ) -> None:
        self.token = token
        self.channel_id = channel_id
        self.socket_path = socket_path

        self._channel: Optional[object] = None  # discord.TextChannel once resolved
        self._client: Optional[object] = None  # discord.Client
        self._server: Optional[asyncio.AbstractServer] = None

    # ------------------------------------------------------------------
    # Public entry point
    # ------------------------------------------------------------------

    def run(self) -> None:
        """Start the bot and socket server.  Blocks until shutdown."""
        try:
            import discord  # type: ignore
        except ImportError:
            LOG.error(
                "discord.py is not installed.  Install it with: pip install discord.py"
            )
            sys.exit(1)

        intents = discord.Intents.default()
        client = discord.Client(intents=intents)
        self._client = client

        # Populate the button style mapping now that discord is imported.
        global _BUTTON_STYLE_MAP
        _BUTTON_STYLE_MAP = {
            "primary": discord.ButtonStyle.primary,
            "secondary": discord.ButtonStyle.secondary,
            "success": discord.ButtonStyle.success,
            "danger": discord.ButtonStyle.danger,
        }

        @client.event
        async def on_ready() -> None:
            LOG.info(
                "Connected to Discord as %s (id=%s)",
                client.user,
                client.user.id if client.user else "?",
            )
            channel = client.get_channel(self.channel_id)
            if channel is None:
                LOG.error(
                    "Channel ID %s not found in any server the bot has access to.  "
                    "Ensure the bot is invited to the correct server and the "
                    "channel ID is valid.",
                    self.channel_id,
                )
                await client.close()
                return

            self._channel = channel
            LOG.info("Target channel: #%s (id=%s)", channel.name, channel.id)

            # Start the Unix socket server now that we have a valid channel.
            await self._start_socket_server()

        @client.event
        async def on_interaction(interaction: discord.Interaction) -> None:
            """Handle button click interactions.

            Acknowledges the click with the clicker's identity and timestamp.
            Routes through _route_interaction() for conversation_manager
            plumbing (no-op for MVP test messages).
            """
            # Delegate to top-level processor to make the logic testable.
            await process_interaction(interaction)

        @client.event
        async def on_disconnect() -> None:
            LOG.warning(
                "Disconnected from Discord – discord.py will attempt to reconnect"
            )

        @client.event
        async def on_resumed() -> None:
            LOG.info("Resumed Discord session")

        # Register signal handlers so the bot exits cleanly.
        loop = asyncio.new_event_loop()

        def _handle_signal() -> None:
            LOG.info("Received termination signal – shutting down")
            loop.create_task(self._shutdown())

        for sig in (signal.SIGTERM, signal.SIGINT):
            try:
                loop.add_signal_handler(sig, _handle_signal)
            except NotImplementedError:
                # Windows does not support add_signal_handler; fall through
                pass

        try:
            loop.run_until_complete(client.start(self.token))
        except KeyboardInterrupt:
            LOG.info("KeyboardInterrupt – shutting down")
            loop.run_until_complete(self._shutdown())
        finally:
            self._cleanup_socket()
            loop.close()

    # ------------------------------------------------------------------
    # Socket server
    # ------------------------------------------------------------------

    async def _start_socket_server(self) -> None:
        """Create an asyncio Unix socket server."""
        self._cleanup_socket()
        self._server = await asyncio.start_unix_server(
            self._handle_client,
            path=self.socket_path,
        )
        LOG.info("Listening on Unix socket: %s", self.socket_path)

    async def _handle_client(
        self,
        reader: asyncio.StreamReader,
        writer: asyncio.StreamWriter,
    ) -> None:
        """Handle a single client connection on the Unix socket.

        Each line is expected to be a JSON object.  We send the ``content``
        field as a Discord message and respond with ``{"ok": true}`` or
        ``{"ok": false, "error": "..."}``.
        """
        try:
            while True:
                line = await reader.readline()
                if not line:
                    break  # EOF – client closed connection

                if len(line) > MAX_MESSAGE_SIZE:
                    response = {"ok": False, "error": "message too large"}
                    writer.write(json.dumps(response).encode() + b"\n")
                    await writer.drain()
                    continue

                try:
                    data = json.loads(line)
                except json.JSONDecodeError as exc:
                    response = {"ok": False, "error": f"invalid JSON: {exc}"}
                    writer.write(json.dumps(response).encode() + b"\n")
                    await writer.drain()
                    continue

                # Extract the message content.  Accept either the
                # ``{"content": "..."}`` format or a ``{"body": "...", "title":
                # "..."}`` format from the notification API.
                content = data.get("content")
                if content is None:
                    title = data.get("title", "")
                    body = data.get("body", "")
                    if title and body:
                        content = f"# {title}\n\n{body}"
                    elif title:
                        content = f"# {title}"
                    elif body:
                        content = body

                # Check whether we have embeds — embeds-only messages are valid
                # even when content is empty.
                raw_embeds = data.get("embeds")
                has_embeds = isinstance(raw_embeds, list) and len(raw_embeds) > 0

                # Optional per-message channel override. If provided, attempt
                # to resolve the requested channel; fall back to the default
                # configured channel on failure.  We accept either int or
                # string channel IDs for compatibility.
                requested_channel = data.get("channel_id")
                target_channel = self._channel
                message_type = data.get("message_type", "other")

                # Priority 1: If channel_id is explicitly set in message, use it
                if requested_channel is not None:
                    try:
                        requested_channel_int = int(requested_channel)
                        resolved = self._client.get_channel(requested_channel_int)  # type: ignore
                        if resolved is None:
                            LOG.warning(
                                "Requested channel_id=%s not found or not visible to bot; using default channel",
                                requested_channel,
                            )
                        else:
                            target_channel = resolved
                            LOG.info(
                                "Routing message to requested channel #%s (id=%s)",
                                getattr(resolved, "name", "?"),
                                getattr(resolved, "id", requested_channel_int),
                            )
                    except Exception:
                        LOG.exception("Invalid requested channel_id=%r; using default channel", requested_channel)
                # Priority 2: If it's a test/CI message, use test channel if configured
                elif message_type in ("test", "ci"):
                    test_channel_env = os.getenv("AMPA_DISCORD_TEST_CHANNEL_ID")
                    if test_channel_env is not None:
                        try:
                            test_channel_int = int(test_channel_env)
                            resolved = self._client.get_channel(test_channel_int)  # type: ignore
                            if resolved is None:
                                LOG.warning(
                                    "AMPA_DISCORD_TEST_CHANNEL_ID=%s not found or not visible to bot; using default channel",
                                    test_channel_env,
                                )
                            else:
                                target_channel = resolved
                                LOG.info(
                                    "Routing %s message to test channel #%s (id=%s) via AMPA_DISCORD_TEST_CHANNEL_ID",
                                    message_type,
                                    getattr(resolved, "name", "?"),
                                    getattr(resolved, "id", test_channel_int),
                                )
                        except Exception:
                            LOG.exception("Invalid AMPA_DISCORD_TEST_CHANNEL_ID=%r; using default channel", test_channel_env)
                    # If test channel not configured or not found, fall through to default

                if not content and not has_embeds:
                    response = {
                        "ok": False,
                        "error": "empty message: no 'content', 'body', or 'embeds' field",
                    }
                    writer.write(json.dumps(response).encode() + b"\n")
                    await writer.drain()
                    continue

                # Parse optional components for interactive buttons and embeds.
                components = data.get("components")
                view = None
                if components:
                    validation_error = _validate_components(components)
                    if validation_error:
                        response = {"ok": False, "error": validation_error}
                        writer.write(json.dumps(response).encode() + b"\n")
                        await writer.drain()
                        continue
                    view = _build_view(components)

                embeds_out = None
                if has_embeds:
                    validation_error = _validate_embeds(raw_embeds)
                    if validation_error:
                        response = {"ok": False, "error": validation_error}
                        writer.write(json.dumps(response).encode() + b"\n")
                        await writer.drain()
                        continue
                    embeds_out = _build_embeds(raw_embeds)

                # Discord messages are limited to 2000 characters.
                if content and len(content) > 2000:
                    content = content[:1997] + "..."

                # Use the resolved target_channel when sending. Temporarily
                # swap self._channel so _send_to_discord uses the selected
                # channel with minimal code changes.
                orig_channel = self._channel
                try:
                    self._channel = target_channel
                    ok = await self._send_to_discord(content, view=view, embeds=embeds_out)
                finally:
                    self._channel = orig_channel
                response: Dict[str, Any] = {"ok": ok}
                if not ok:
                    response["error"] = "failed to send to Discord"
                writer.write(json.dumps(response).encode() + b"\n")
                await writer.drain()
        except asyncio.CancelledError:
            pass
        except Exception:
            LOG.exception("Error handling socket client")
        finally:
            try:
                writer.close()
                await writer.wait_closed()
            except Exception:
                pass

    async def _send_to_discord(
        self, content: str, *, view: Optional[Any] = None, embeds: Optional[List[Any]] = None
    ) -> bool:
        """Send a text message to the configured Discord channel.

        Parameters
        ----------
        content:
            The text content of the message.
        view:
            Optional ``discord.ui.View`` to attach interactive components.
        """
        # CI / dry-run support: skip actual send if AMPA_DISABLE_DISCORD is set.
        disable_discord = os.getenv("AMPA_DISABLE_DISCORD")
        if disable_discord is not None:
            LOG.debug(
                "_send_to_discord: AMPA_DISABLE_DISCORD=%s, skipping Discord send",
                disable_discord,
            )
            return True

        if self._channel is None:
            LOG.error("Cannot send message: channel not resolved")
            return False
        try:
            kwargs: Dict[str, Any] = {}
            if content:
                kwargs["content"] = content
            if view is not None:
                kwargs["view"] = view
            if embeds is not None:
                kwargs["embeds"] = embeds
            await self._channel.send(**kwargs)
            LOG.debug(
                "Sent message to #%s (%d chars, embeds=%s, components=%s)",
                getattr(self._channel, "name", "?"),
                len(content) if content else 0,
                embeds is not None,
                view is not None,
            )
            return True
        except Exception:
            LOG.exception("Failed to send message to Discord")
            return False

    # ------------------------------------------------------------------
    # Shutdown helpers
    # ------------------------------------------------------------------

    async def _shutdown(self) -> None:
        """Gracefully stop the socket server and disconnect from Discord."""
        LOG.info("Shutting down...")
        if self._server is not None:
            self._server.close()
            await self._server.wait_closed()
            self._server = None
        if self._client is not None:
            await self._client.close()

    def _cleanup_socket(self) -> None:
        """Remove the Unix socket file if it exists."""
        try:
            if os.path.exists(self.socket_path):
                os.unlink(self.socket_path)
        except OSError:
            LOG.warning("Could not remove socket file: %s", self.socket_path)


# ------------------------------------------------------------------
# CLI entry point
# ------------------------------------------------------------------


def main() -> None:
    """CLI entry point for ``python -m ampa.discord_bot``."""
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )

    token = os.getenv("AMPA_DISCORD_BOT_TOKEN")
    if not token:
        LOG.error(
            "AMPA_DISCORD_BOT_TOKEN is not set.  "
            "Create a bot in the Discord Developer Portal and set this env var "
            "to the bot token."
        )
        sys.exit(1)

    channel_id_raw = os.getenv("AMPA_DISCORD_CHANNEL_ID")
    if not channel_id_raw:
        LOG.error(
            "AMPA_DISCORD_CHANNEL_ID is not set.  "
            "Set this to the integer ID of the Discord channel where "
            "notifications should be sent."
        )
        sys.exit(1)

    try:
        channel_id = int(channel_id_raw)
    except ValueError:
        LOG.error(
            "AMPA_DISCORD_CHANNEL_ID=%r is not a valid integer.  "
            "Channel IDs are numeric (right-click channel > Copy ID in Discord).",
            channel_id_raw,
        )
        sys.exit(1)

    socket_path = os.getenv("AMPA_BOT_SOCKET_PATH", DEFAULT_SOCKET_PATH)

    LOG.info(
        "Starting AMPA Discord bot – channel_id=%s socket=%s",
        channel_id,
        socket_path,
    )
    bot = AMPABot(token=token, channel_id=channel_id, socket_path=socket_path)
    bot.run()


if __name__ == "__main__":
    main()
