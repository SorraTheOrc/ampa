"""Tests for ampa.discord_bot module.

These tests verify the AMPABot class without connecting to Discord.  They
exercise the Unix socket protocol, message parsing, and error handling by
mocking the discord.py Client and channel objects.

NOTE: Tests avoid ``pytest-asyncio`` (not installed) and instead run async
code via ``asyncio.run()`` or ``loop.run_until_complete()`` inside regular
synchronous test functions.
"""

from __future__ import annotations

import asyncio
import json
import os
import sys
from typing import Any, Dict, List, Optional
from unittest.mock import MagicMock, patch

import pytest

# ---------------------------------------------------------------------------
# Import the module under test
# ---------------------------------------------------------------------------

from ampa.discord_bot import (
    AMPABot,
    DEFAULT_SOCKET_PATH,
    MAX_MESSAGE_SIZE,
    _build_view,
    _route_interaction,
    _route_pr_review_interaction,
    _validate_components,
    main,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


class FakeChannel:
    """Minimal fake for discord.TextChannel."""

    def __init__(self, name: str = "test-channel", channel_id: int = 12345):
        self.name = name
        self.id = channel_id
        self.sent: List[Dict[str, Any]] = []

    async def send(self, content: str = "", **kwargs: Any) -> None:
        record: Dict[str, Any] = {"content": content}
        if "view" in kwargs:
            record["view"] = kwargs["view"]
        self.sent.append(record)


async def _send_socket_messages(
    socket_path: str,
    messages: List[Dict[str, Any]],
    timeout: float = 5.0,
) -> List[Dict[str, Any]]:
    """Connect to the Unix socket and send JSON messages, collecting responses."""
    responses: List[Dict[str, Any]] = []
    reader, writer = await asyncio.open_unix_connection(socket_path)
    for msg in messages:
        line = json.dumps(msg) + "\n"
        writer.write(line.encode())
        await writer.drain()
        resp_line = await asyncio.wait_for(reader.readline(), timeout=timeout)
        if resp_line:
            responses.append(json.loads(resp_line))
    writer.close()
    await writer.wait_closed()
    return responses


async def _run_socket_test(bot, socket_path, messages):
    """Start socket server, send messages, stop server, return responses."""
    await bot._start_socket_server()
    try:
        return await _send_socket_messages(socket_path, messages)
    finally:
        if bot._server:
            bot._server.close()
            await bot._server.wait_closed()


# ---------------------------------------------------------------------------
# Tests: AMPABot socket protocol
# ---------------------------------------------------------------------------


class TestAMPABotSocketProtocol:
    """Test the Unix socket server and message handling."""

    @pytest.fixture
    def socket_path(self, tmp_path):
        return str(tmp_path / "test_bot.sock")

    @pytest.fixture
    def bot(self, socket_path):
        return AMPABot(
            token="fake-token",
            channel_id=12345,
            socket_path=socket_path,
        )

    @pytest.fixture
    def fake_channel(self):
        return FakeChannel()

    def test_send_content_message(self, bot, socket_path, fake_channel):
        """Bot sends content field as Discord message."""

        async def _test():
            bot._channel = fake_channel
            responses = await _run_socket_test(
                bot, socket_path, [{"content": "Hello from AMPA"}]
            )
            assert len(responses) == 1
            assert responses[0]["ok"] is True
            assert len(fake_channel.sent) == 1
            assert fake_channel.sent[0]["content"] == "Hello from AMPA"

        asyncio.run(_test())

    def test_send_title_body_message(self, bot, socket_path, fake_channel):
        """Bot constructs content from title + body when content is absent."""

        async def _test():
            bot._channel = fake_channel
            responses = await _run_socket_test(
                bot, socket_path, [{"title": "Test Title", "body": "Test body text"}]
            )
            assert len(responses) == 1
            assert responses[0]["ok"] is True
            assert len(fake_channel.sent) == 1
            assert fake_channel.sent[0]["content"] == "# Test Title\n\nTest body text"

        asyncio.run(_test())

    def test_send_title_only(self, bot, socket_path, fake_channel):
        """Bot handles title without body."""

        async def _test():
            bot._channel = fake_channel
            responses = await _run_socket_test(
                bot, socket_path, [{"title": "Just a Title"}]
            )
            assert responses[0]["ok"] is True
            assert fake_channel.sent[0]["content"] == "# Just a Title"

        asyncio.run(_test())

    def test_send_body_only(self, bot, socket_path, fake_channel):
        """Bot handles body without title."""

        async def _test():
            bot._channel = fake_channel
            responses = await _run_socket_test(
                bot, socket_path, [{"body": "Just a body"}]
            )
            assert responses[0]["ok"] is True
            assert fake_channel.sent[0]["content"] == "Just a body"

        asyncio.run(_test())

    def test_empty_message_rejected(self, bot, socket_path, fake_channel):
        """Bot rejects messages with no content, title, or body."""

        async def _test():
            bot._channel = fake_channel
            responses = await _run_socket_test(
                bot, socket_path, [{"message_type": "heartbeat"}]
            )
            assert responses[0]["ok"] is False
            assert "empty message" in responses[0]["error"]
            assert len(fake_channel.sent) == 0

        asyncio.run(_test())

    def test_invalid_json_rejected(self, bot, socket_path, fake_channel):
        """Bot rejects non-JSON input."""

        async def _test():
            bot._channel = fake_channel
            await bot._start_socket_server()
            try:
                reader, writer = await asyncio.open_unix_connection(socket_path)
                writer.write(b"not valid json\n")
                await writer.drain()
                resp_line = await asyncio.wait_for(reader.readline(), timeout=5.0)
                resp = json.loads(resp_line)
                assert resp["ok"] is False
                assert "invalid JSON" in resp["error"]
                writer.close()
                await writer.wait_closed()
            finally:
                if bot._server:
                    bot._server.close()
                    await bot._server.wait_closed()

        asyncio.run(_test())

    def test_multiple_messages_in_one_connection(self, bot, socket_path, fake_channel):
        """Bot handles multiple messages on a single connection."""

        async def _test():
            bot._channel = fake_channel
            messages = [
                {"content": "Message 1"},
                {"content": "Message 2"},
                {"content": "Message 3"},
            ]
            responses = await _run_socket_test(bot, socket_path, messages)
            assert len(responses) == 3
            assert all(r["ok"] for r in responses)
            assert [m["content"] for m in fake_channel.sent] == [
                "Message 1",
                "Message 2",
                "Message 3",
            ]

        asyncio.run(_test())

    def test_requested_channel_override_routes_message(self, bot, socket_path, fake_channel):
        """When a message includes channel_id it is resolved via client.get_channel and used."""

        async def _test():
            # Default channel should not receive the message when override works.
            bot._channel = fake_channel
            # Prepare an alternate channel and a fake client that resolves it.
            alt_channel = FakeChannel(name="alt-channel", channel_id=99999)
            mock_client = MagicMock()
            mock_client.get_channel.return_value = alt_channel
            bot._client = mock_client

            messages = [{"content": "Routed message", "channel_id": 99999}]
            responses = await _run_socket_test(bot, socket_path, messages)
            assert len(responses) == 1
            assert responses[0]["ok"] is True
            # Ensure alt channel received message and default did not.
            assert len(alt_channel.sent) == 1
            assert alt_channel.sent[0]["content"] == "Routed message"
            assert len(fake_channel.sent) == 0

        asyncio.run(_test())

    def test_discord_message_truncated_at_2000_chars(
        self, bot, socket_path, fake_channel
    ):
        """Messages exceeding 2000 chars are truncated before sending to Discord."""

        async def _test():
            bot._channel = fake_channel
            long_content = "x" * 2500
            responses = await _run_socket_test(
                bot, socket_path, [{"content": long_content}]
            )
            assert responses[0]["ok"] is True
            assert len(fake_channel.sent) == 1
            assert len(fake_channel.sent[0]["content"]) == 2000
            assert fake_channel.sent[0]["content"].endswith("...")

        asyncio.run(_test())

    def test_channel_not_resolved_returns_error(self, bot, socket_path):
        """If channel is not resolved, sending fails gracefully."""

        async def _test():
            bot._channel = None
            responses = await _run_socket_test(bot, socket_path, [{"content": "Hello"}])
            assert responses[0]["ok"] is False
            assert "failed to send" in responses[0].get("error", "")

        asyncio.run(_test())

    def test_discord_send_failure(self, bot, socket_path):
        """If Discord channel.send() raises, bot reports failure."""

        async def _test():
            channel = FakeChannel()

            async def fail_send(content: str = "", **kwargs: Any) -> None:
                raise RuntimeError("Discord API error")

            channel.send = fail_send
            bot._channel = channel

            responses = await _run_socket_test(bot, socket_path, [{"content": "Hello"}])
            assert responses[0]["ok"] is False

        asyncio.run(_test())


# ---------------------------------------------------------------------------
# Tests: _send_to_discord
# ---------------------------------------------------------------------------


class TestSendToDiscord:
    def test_send_returns_true_on_success(self):
        async def _test():
            ch = FakeChannel()
            bot = AMPABot(token="t", channel_id=1)
            bot._channel = ch
            result = await bot._send_to_discord("hello")
            assert result is True
            assert ch.sent == [{"content": "hello"}]

        asyncio.run(_test())

    def test_send_returns_false_when_no_channel(self):
        async def _test():
            bot = AMPABot(token="t", channel_id=1)
            bot._channel = None
            result = await bot._send_to_discord("hello")
            assert result is False

        asyncio.run(_test())

    def test_send_returns_false_on_exception(self):
        async def _test():
            ch = FakeChannel()

            async def raise_err(content: str = "", **kwargs: Any) -> None:
                raise Exception("boom")

            ch.send = raise_err
            bot = AMPABot(token="t", channel_id=1)
            bot._channel = ch
            result = await bot._send_to_discord("hello")
            assert result is False

        asyncio.run(_test())


# ---------------------------------------------------------------------------
# Tests: socket cleanup
# ---------------------------------------------------------------------------


class TestSocketCleanup:
    def test_cleanup_removes_socket_file(self, tmp_path):
        sock_path = str(tmp_path / "test.sock")
        with open(sock_path, "w") as f:
            f.write("")
        bot = AMPABot(token="t", channel_id=1, socket_path=sock_path)
        bot._cleanup_socket()
        assert not os.path.exists(sock_path)

    def test_cleanup_no_error_if_missing(self, tmp_path):
        sock_path = str(tmp_path / "nonexistent.sock")
        bot = AMPABot(token="t", channel_id=1, socket_path=sock_path)
        bot._cleanup_socket()  # Should not raise


# ---------------------------------------------------------------------------
# Tests: shutdown
# ---------------------------------------------------------------------------


class TestShutdown:
    def test_shutdown_closes_server_and_client(self):
        async def _test():
            bot = AMPABot(token="t", channel_id=1)

            # Fake server
            class FakeServer:
                closed = False

                def close(self):
                    self.closed = True

                async def wait_closed(self):
                    pass

            # Fake client
            class FakeClient:
                closed = False

                async def close(self):
                    self.closed = True

            server = FakeServer()
            client = FakeClient()
            bot._server = server
            bot._client = client

            await bot._shutdown()
            assert server.closed
            assert client.closed
            assert bot._server is None

        asyncio.run(_test())


# ---------------------------------------------------------------------------
# Tests: CLI entry point (main)
# ---------------------------------------------------------------------------


class TestMain:
    def test_missing_token_exits(self, monkeypatch):
        monkeypatch.delenv("AMPA_DISCORD_BOT_TOKEN", raising=False)
        monkeypatch.delenv("AMPA_DISCORD_CHANNEL_ID", raising=False)
        with pytest.raises(SystemExit) as exc_info:
            main()
        assert exc_info.value.code == 1

    def test_missing_channel_id_exits(self, monkeypatch):
        monkeypatch.setenv("AMPA_DISCORD_BOT_TOKEN", "fake-token")
        monkeypatch.delenv("AMPA_DISCORD_CHANNEL_ID", raising=False)
        with pytest.raises(SystemExit) as exc_info:
            main()
        assert exc_info.value.code == 1

    def test_invalid_channel_id_exits(self, monkeypatch):
        monkeypatch.setenv("AMPA_DISCORD_BOT_TOKEN", "fake-token")
        monkeypatch.setenv("AMPA_DISCORD_CHANNEL_ID", "not-a-number")
        with pytest.raises(SystemExit) as exc_info:
            main()
        assert exc_info.value.code == 1

    def test_valid_config_creates_bot(self, monkeypatch, tmp_path):
        monkeypatch.setenv("AMPA_DISCORD_BOT_TOKEN", "fake-token")
        monkeypatch.setenv("AMPA_DISCORD_CHANNEL_ID", "12345")
        sock = str(tmp_path / "test.sock")
        monkeypatch.setenv("AMPA_BOT_SOCKET_PATH", sock)

        run_called: List[Dict[str, Any]] = []

        def fake_run(self):
            run_called.append(
                {
                    "token": self.token,
                    "channel_id": self.channel_id,
                    "socket_path": self.socket_path,
                }
            )

        monkeypatch.setattr(AMPABot, "run", fake_run)
        main()
        assert len(run_called) == 1
        assert run_called[0]["token"] == "fake-token"
        assert run_called[0]["channel_id"] == 12345
        assert run_called[0]["socket_path"] == sock

    def test_default_socket_path_used(self, monkeypatch):
        monkeypatch.setenv("AMPA_DISCORD_BOT_TOKEN", "fake-token")
        monkeypatch.setenv("AMPA_DISCORD_CHANNEL_ID", "12345")
        monkeypatch.delenv("AMPA_BOT_SOCKET_PATH", raising=False)

        run_called: List[Dict[str, Any]] = []

        def fake_run(self):
            run_called.append({"socket_path": self.socket_path})

        monkeypatch.setattr(AMPABot, "run", fake_run)
        main()
        assert run_called[0]["socket_path"] == DEFAULT_SOCKET_PATH


# ---------------------------------------------------------------------------
# Tests: AMPABot initialization
# ---------------------------------------------------------------------------


class TestAMPABotInit:
    def test_default_socket_path(self):
        bot = AMPABot(token="t", channel_id=123)
        assert bot.socket_path == DEFAULT_SOCKET_PATH
        assert bot.token == "t"
        assert bot.channel_id == 123

    def test_custom_socket_path(self):
        bot = AMPABot(token="t", channel_id=123, socket_path="/custom/path.sock")
        assert bot.socket_path == "/custom/path.sock"

    def test_initial_state_is_none(self):
        bot = AMPABot(token="t", channel_id=123)
        assert bot._channel is None
        assert bot._client is None
        assert bot._server is None

    def test_discord_import_error(self, monkeypatch):
        """If discord.py is not installed, bot.run() exits with code 1."""
        bot = AMPABot(token="t", channel_id=123)

        import builtins

        original_import = builtins.__import__

        def mock_import(name, *args, **kwargs):
            if name == "discord":
                raise ImportError("No module named 'discord'")
            return original_import(name, *args, **kwargs)

        monkeypatch.setattr(builtins, "__import__", mock_import)

        with pytest.raises(SystemExit) as exc_info:
            bot.run()
        assert exc_info.value.code == 1


# ---------------------------------------------------------------------------
# Tests: _validate_components
# ---------------------------------------------------------------------------


class TestValidateComponents:
    def test_valid_single_button(self):
        comps = [{"type": "button", "label": "Blue", "custom_id": "test_blue"}]
        assert _validate_components(comps) is None

    def test_valid_multiple_buttons(self):
        comps = [
            {
                "type": "button",
                "label": "Blue",
                "custom_id": "test_blue",
                "style": "primary",
            },
            {
                "type": "button",
                "label": "Red",
                "custom_id": "test_red",
                "style": "danger",
            },
        ]
        assert _validate_components(comps) is None

    def test_not_a_list(self):
        err = _validate_components("not a list")
        assert err is not None
        assert "must be a list" in err

    def test_element_not_a_dict(self):
        err = _validate_components(["not a dict"])
        assert err is not None
        assert "must be an object" in err

    def test_missing_required_fields(self):
        # Missing label and custom_id
        err = _validate_components([{"type": "button"}])
        assert err is not None
        assert "missing required fields" in err

    def test_unsupported_type(self):
        err = _validate_components(
            [{"type": "select", "label": "Pick", "custom_id": "test_pick"}]
        )
        assert err is not None
        assert "unsupported type" in err

    def test_empty_list_is_valid(self):
        assert _validate_components([]) is None


# ---------------------------------------------------------------------------
# Tests: _route_interaction
# ---------------------------------------------------------------------------


class TestRouteInteraction:
    def test_test_prefix_is_noop(self):
        """test_* custom_ids are handled as no-ops (no exception, no side effect)."""
        _route_interaction("test_blue", "user#1234", "2026-01-01T00:00:00Z")

    def test_non_test_prefix_logs(self):
        """Non-test custom_ids log an info message but don't raise."""
        _route_interaction("survey_q1", "user#1234", "2026-01-01T00:00:00Z")


class TestProcessInteractionAck:
    """Tests for the interaction acknowledgement path (process_interaction).

    These tests avoid requiring discord.py by constructing a minimal fake
    Interaction-like object.  The acknowledgement path should call
    ``interaction.response.send_message(...)`` with the expected formatted
    string.
    """

    def test_process_interaction_sends_ack_for_known_custom_id(self):
        from types import SimpleNamespace
        from unittest.mock import AsyncMock

        try:
            import discord  # noqa: F401

            interaction_type = discord.InteractionType.component
        except Exception:
            interaction_type = "component"

        resp = SimpleNamespace(send_message=AsyncMock())
        response_wrapper = SimpleNamespace(send_message=resp.send_message)

        user = SimpleNamespace(name="alice", discriminator="1234")
        interaction = SimpleNamespace(
            type=interaction_type,
            data={"custom_id": "test_blue"},
            user=user,
            response=response_wrapper,
        )

        # Call the coroutine and assert send_message was awaited with expected
        # acknowledgement containing the human-readable label and user id.
        import asyncio

        from ampa.discord_bot import process_interaction

        asyncio.run(process_interaction(interaction))
        resp.send_message.assert_awaited()
        sent_msg = resp.send_message.call_args[0][0]
        assert "You selected Blue, good luck." in sent_msg
        assert "alice#1234" in sent_msg

    def test_process_interaction_handles_unparseable_custom_id(self, caplog):
        from types import SimpleNamespace
        from unittest.mock import AsyncMock

        try:
            import discord  # noqa: F401

            interaction_type = discord.InteractionType.component
        except Exception:
            interaction_type = "component"

        resp = SimpleNamespace(send_message=AsyncMock())
        response_wrapper = SimpleNamespace(send_message=resp.send_message)

        user = SimpleNamespace(name="bob", discriminator="4321")
        # custom_id without underscore should trigger a warning but still be
        # acknowledged using the fallback label.
        interaction = SimpleNamespace(
            type=interaction_type,
            data={"custom_id": "mysteryid"},
            user=user,
            response=response_wrapper,
        )

        import asyncio
        from ampa.discord_bot import process_interaction

        caplog.clear()
        asyncio.run(process_interaction(interaction))
        resp.send_message.assert_awaited()
        sent_msg = resp.send_message.call_args[0][0]
        assert "You selected Mysteryid, good luck." in sent_msg
        # A warning should have been emitted for the unparsable custom_id
        assert any(
            "Could not derive label from custom_id" in r.message for r in caplog.records
        )


# ---------------------------------------------------------------------------
# Tests: _build_view (requires discord.py mock)
# ---------------------------------------------------------------------------


class TestBuildView:
    def test_build_view_creates_view_with_buttons(self):
        """_build_view returns a discord.ui.View with the right number of items."""
        # We need discord.py for this test — skip if not available.
        try:
            import discord  # noqa: F401
        except ImportError:
            pytest.skip("discord.py not installed")

        # Populate the style map as run() would
        global _BUTTON_STYLE_MAP
        from ampa.discord_bot import _BUTTON_STYLE_MAP

        # Temporarily set styles for test
        original = dict(_BUTTON_STYLE_MAP)
        try:
            import ampa.discord_bot as db_mod

            db_mod._BUTTON_STYLE_MAP = {
                "primary": discord.ButtonStyle.primary,
                "secondary": discord.ButtonStyle.secondary,
                "success": discord.ButtonStyle.success,
                "danger": discord.ButtonStyle.danger,
            }
            comps = [
                {
                    "type": "button",
                    "label": "Blue",
                    "custom_id": "test_blue",
                    "style": "primary",
                },
                {
                    "type": "button",
                    "label": "Red",
                    "custom_id": "test_red",
                    "style": "danger",
                },
            ]

            # discord.ui.View requires a running asyncio event loop
            async def _run():
                return _build_view(comps)

            view = asyncio.run(_run())
            assert view is not None
            assert len(view.children) == 2
        finally:
            db_mod._BUTTON_STYLE_MAP = original

    def test_build_view_default_style(self):
        """Buttons without explicit style default to secondary."""
        try:
            import discord
        except ImportError:
            pytest.skip("discord.py not installed")

        import ampa.discord_bot as db_mod

        original = dict(db_mod._BUTTON_STYLE_MAP)
        try:
            db_mod._BUTTON_STYLE_MAP = {
                "primary": discord.ButtonStyle.primary,
                "secondary": discord.ButtonStyle.secondary,
                "success": discord.ButtonStyle.success,
                "danger": discord.ButtonStyle.danger,
            }
            comps = [{"type": "button", "label": "OK", "custom_id": "test_ok"}]

            # discord.ui.View requires a running asyncio event loop
            async def _run():
                return _build_view(comps)

            view = asyncio.run(_run())
            assert len(view.children) == 1
            assert view.children[0].style == discord.ButtonStyle.secondary
        finally:
            db_mod._BUTTON_STYLE_MAP = original


# ---------------------------------------------------------------------------
# Tests: Component socket protocol (end-to-end via socket)
# ---------------------------------------------------------------------------


class TestComponentSocketProtocol:
    @pytest.fixture
    def socket_path(self, tmp_path):
        return str(tmp_path / "test_bot.sock")

    @pytest.fixture
    def bot(self, socket_path):
        return AMPABot(
            token="fake-token",
            channel_id=12345,
            socket_path=socket_path,
        )

    @pytest.fixture
    def fake_channel(self):
        return FakeChannel()

    def test_message_with_valid_components(self, bot, socket_path, fake_channel):
        """Message with valid components attaches a view to the send call."""
        try:
            import discord  # noqa: F401
        except ImportError:
            pytest.skip("discord.py not installed")

        import ampa.discord_bot as db_mod

        original = dict(db_mod._BUTTON_STYLE_MAP)
        db_mod._BUTTON_STYLE_MAP = {
            "primary": discord.ButtonStyle.primary,
            "secondary": discord.ButtonStyle.secondary,
            "success": discord.ButtonStyle.success,
            "danger": discord.ButtonStyle.danger,
        }

        async def _test():
            bot._channel = fake_channel
            msg = {
                "content": "Pick a colour",
                "components": [
                    {
                        "type": "button",
                        "label": "Blue",
                        "custom_id": "test_blue",
                        "style": "primary",
                    },
                    {
                        "type": "button",
                        "label": "Red",
                        "custom_id": "test_red",
                        "style": "danger",
                    },
                ],
            }
            responses = await _run_socket_test(bot, socket_path, [msg])
            assert len(responses) == 1
            assert responses[0]["ok"] is True
            assert len(fake_channel.sent) == 1
            assert fake_channel.sent[0]["content"] == "Pick a colour"
            assert "view" in fake_channel.sent[0]
            assert fake_channel.sent[0]["view"] is not None

        try:
            asyncio.run(_test())
        finally:
            db_mod._BUTTON_STYLE_MAP = original

    def test_message_with_invalid_components_rejected(
        self, bot, socket_path, fake_channel
    ):
        """Message with invalid components returns an error."""

        async def _test():
            bot._channel = fake_channel
            msg = {
                "content": "Bad components",
                "components": [{"type": "button"}],  # missing required fields
            }
            responses = await _run_socket_test(bot, socket_path, [msg])
            assert len(responses) == 1
            assert responses[0]["ok"] is False
            assert "missing required fields" in responses[0]["error"]
            assert len(fake_channel.sent) == 0

        asyncio.run(_test())

    def test_message_without_components_sends_plain(
        self, bot, socket_path, fake_channel
    ):
        """Message without components field sends as plain text (no view)."""

        async def _test():
            bot._channel = fake_channel
            responses = await _run_socket_test(
                bot, socket_path, [{"content": "Plain text"}]
            )
            assert responses[0]["ok"] is True
            assert fake_channel.sent[0]["content"] == "Plain text"
            assert "view" not in fake_channel.sent[0]

        asyncio.run(_test())


# ---------------------------------------------------------------------------
# Tests: _send_to_discord with view parameter
# ---------------------------------------------------------------------------


class TestSendToDiscordWithView:
    def test_send_with_view(self):
        """_send_to_discord passes view kwarg to channel.send."""

        async def _test():
            ch = FakeChannel()
            bot = AMPABot(token="t", channel_id=1)
            bot._channel = ch
            mock_view = MagicMock()
            result = await bot._send_to_discord("hello", view=mock_view)
            assert result is True
            assert len(ch.sent) == 1
            assert ch.sent[0]["content"] == "hello"
            assert ch.sent[0]["view"] is mock_view

        asyncio.run(_test())

    def test_send_without_view(self):
        """_send_to_discord without view does not include view key."""

        async def _test():
            ch = FakeChannel()
            bot = AMPABot(token="t", channel_id=1)
            bot._channel = ch
            result = await bot._send_to_discord("hello")
            assert result is True
            assert len(ch.sent) == 1
            assert ch.sent[0]["content"] == "hello"
            assert "view" not in ch.sent[0]

        asyncio.run(_test())

    def test_disable_discord_env_skip_send(self, monkeypatch):
        """When AMPA_DISABLE_DISCORD is set, _send_to_discord returns True without sending."""
        import os

        async def _test():
            ch = FakeChannel()
            bot = AMPABot(token="t", channel_id=1)
            bot._channel = ch
            monkeypatch.setenv("AMPA_DISABLE_DISCORD", "1")
            result = await bot._send_to_discord("hello")
            assert result is True
            assert len(ch.sent) == 0

        asyncio.run(_test())


# ---------------------------------------------------------------------------
# Tests: _route_pr_review_interaction
# ---------------------------------------------------------------------------


class TestRoutePrReviewInteraction:
    """Tests for the PR review approve/reject routing function."""

    def test_approve_calls_responder_with_accept(self):
        """pr_review_approve_42 routes to responder with action=accept."""
        with patch("ampa.responder.resume_from_payload") as mock_resume, \
            patch("ampa.pr_monitor.PRMonitorRunner.handle_review_decision") as mock_handle:
            mock_resume.return_value = {}
            _route_pr_review_interaction(
                "pr_review_approve_42", "alice#1234", "2026-01-01T00:00:00Z"
            )
            mock_resume.assert_called_once()
            payload = mock_resume.call_args[0][0]
            assert payload["session_id"] == "pr-review-42"
            assert payload["action"] == "accept"
            assert payload["metadata"]["pr_number"] == 42
            assert payload["metadata"]["approved_by"] == "alice#1234"
            assert payload["metadata"]["timestamp"] == "2026-01-01T00:00:00Z"
            assert payload["metadata"]["source"] == "discord_button"
            mock_handle.assert_not_called()

    def test_approve_resumed_triggers_pr_monitor_decision(self):
        """A resumed session should invoke PRMonitorRunner.handle_review_decision."""
        with patch("ampa.responder.resume_from_payload") as mock_resume, \
            patch("ampa.pr_monitor.PRMonitorRunner.handle_review_decision") as mock_handle:
            mock_resume.return_value = {
                "status": "resumed",
                "context": [{"pr_number": 42, "work_item_id": "SA-123"}],
            }
            _route_pr_review_interaction(
                "pr_review_approve_42", "alice#1234", "2026-01-01T00:00:00Z"
            )
            mock_handle.assert_called_once_with(
                action="accept",
                pr_number=42,
                work_item_id="SA-123",
                approved_by="alice#1234",
            )

    def test_reject_calls_responder_with_decline(self):
        """pr_review_reject_99 routes to responder with action=decline."""
        with patch("ampa.responder.resume_from_payload") as mock_resume, \
            patch("ampa.pr_monitor.PRMonitorRunner.handle_review_decision") as mock_handle:
            mock_resume.return_value = {}
            _route_pr_review_interaction(
                "pr_review_reject_99", "bob#5678", "2026-02-15T12:00:00Z"
            )
            mock_resume.assert_called_once()
            payload = mock_resume.call_args[0][0]
            assert payload["session_id"] == "pr-review-99"
            assert payload["action"] == "decline"
            assert payload["metadata"]["pr_number"] == 99
            assert payload["metadata"]["approved_by"] == "bob#5678"
            mock_handle.assert_not_called()

    def test_reject_resumed_triggers_pr_monitor_decision(self):
        """A resumed reject session should invoke PRMonitorRunner decision flow."""
        with patch("ampa.responder.resume_from_payload") as mock_resume, \
            patch("ampa.pr_monitor.PRMonitorRunner.handle_review_decision") as mock_handle:
            mock_resume.return_value = {
                "status": "resumed",
                "context": [{"pr_number": 99, "work_item_id": "SA-999"}],
            }
            _route_pr_review_interaction(
                "pr_review_reject_99", "bob#5678", "2026-02-15T12:00:00Z"
            )
            mock_handle.assert_called_once_with(
                action="decline",
                pr_number=99,
                work_item_id="SA-999",
                approved_by="bob#5678",
            )

    def test_invalid_pr_number_logs_error_and_returns(self, caplog):
        """Non-numeric PR suffix logs an error and does not call responder."""
        import logging

        with caplog.at_level(logging.ERROR):
            _route_pr_review_interaction(
                "pr_review_approve_notanumber", "alice#1234", "2026-01-01T00:00:00Z"
            )
        assert any("Invalid PR number" in r.message for r in caplog.records)

    def test_empty_pr_number_logs_error(self, caplog):
        """Empty PR number suffix logs an error."""
        import logging

        with caplog.at_level(logging.ERROR):
            _route_pr_review_interaction(
                "pr_review_approve_", "alice#1234", "2026-01-01T00:00:00Z"
            )
        assert any("Invalid PR number" in r.message for r in caplog.records)

    def test_responder_exception_is_caught(self, caplog):
        """If responder.resume_from_payload raises, exception is logged, not propagated."""
        import logging

        with patch("ampa.responder.resume_from_payload") as mock_resume:
            mock_resume.side_effect = RuntimeError("session not found")
            with caplog.at_level(logging.ERROR):
                # Should NOT raise
                _route_pr_review_interaction(
                    "pr_review_approve_10", "alice#1234", "2026-01-01T00:00:00Z"
                )
        assert any(
            "PR review routing failed" in r.message for r in caplog.records
        )

    def test_responder_import_failure_is_caught(self, caplog):
        """If responder cannot be imported, error is logged and function returns."""
        import logging

        with caplog.at_level(logging.ERROR):
            with patch.dict("sys.modules", {"ampa.responder": None}):
                # Patch the import inside the function to raise ImportError
                original_import = __builtins__["__import__"] if isinstance(__builtins__, dict) else __builtins__.__import__

                def failing_import(name, *args, **kwargs):
                    if name == "ampa.responder" or (args and "responder" in str(args)):
                        raise ImportError("no responder")
                    return original_import(name, *args, **kwargs)

                with patch("builtins.__import__", side_effect=failing_import):
                    _route_pr_review_interaction(
                        "pr_review_approve_5", "alice#1234", "2026-01-01T00:00:00Z"
                    )
        assert any(
            "Cannot import responder" in r.message for r in caplog.records
        )


class TestRouteInteractionPRReviewDelegation:
    """Tests that _route_interaction correctly delegates to _route_pr_review_interaction."""

    def test_approve_prefix_delegates(self):
        """_route_interaction with pr_review_approve_* delegates to PR review routing."""
        with patch("ampa.discord_bot._route_pr_review_interaction") as mock_fn:
            _route_interaction(
                "pr_review_approve_42", "alice#1234", "2026-01-01T00:00:00Z"
            )
            mock_fn.assert_called_once_with(
                "pr_review_approve_42", "alice#1234", "2026-01-01T00:00:00Z"
            )

    def test_reject_prefix_delegates(self):
        """_route_interaction with pr_review_reject_* delegates to PR review routing."""
        with patch("ampa.discord_bot._route_pr_review_interaction") as mock_fn:
            _route_interaction(
                "pr_review_reject_7", "bob#5678", "2026-02-15T12:00:00Z"
            )
            mock_fn.assert_called_once_with(
                "pr_review_reject_7", "bob#5678", "2026-02-15T12:00:00Z"
            )

    def test_test_prefix_does_not_delegate(self):
        """test_* prefix should NOT delegate to PR review routing."""
        with patch("ampa.discord_bot._route_pr_review_interaction") as mock_fn:
            _route_interaction("test_blue", "user#1234", "2026-01-01T00:00:00Z")
            mock_fn.assert_not_called()

    def test_unknown_prefix_does_not_delegate(self):
        """Unknown prefix should NOT delegate to PR review routing."""
        with patch("ampa.discord_bot._route_pr_review_interaction") as mock_fn:
            _route_interaction("survey_q1", "user#1234", "2026-01-01T00:00:00Z")
            mock_fn.assert_not_called()
