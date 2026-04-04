"""Minimal discord_bot shim for installer package.

This shim provides the names used by the installer and daemon to start a
Discord bot process. It is intentionally lightweight and logs actions rather
than implementing the full discord.py client. The real implementation exists
in src/ampa/discord_bot.py.
"""
from __future__ import annotations

import logging
import os
import time

log = logging.getLogger("ampa.discord_bot")


class DummyBot:
    def __init__(self, token: str | None, channel_id: int | None, socket: str | None):
        self.token = token
        self.channel_id = channel_id
        self.socket = socket

    def run(self) -> None:
        log.info("DummyBot.run: token=%s channel_id=%s socket=%s", bool(self.token), self.channel_id, self.socket)
        # Simulate a running bot loop until killed
        try:
            while True:
                time.sleep(1)
        except KeyboardInterrupt:
            log.info("DummyBot received KeyboardInterrupt, exiting")


def main() -> None:
    token = os.environ.get("AMPA_DISCORD_BOT_TOKEN")
    channel = os.environ.get("AMPA_DISCORD_CHANNEL_ID")
    socket = os.environ.get("AMPA_BOT_SOCKET") or "/tmp/ampa_bot.sock"
    bot = DummyBot(token=token, channel_id=int(channel) if channel else None, socket=socket)
    bot.run()


if __name__ == "__main__":
    main()
