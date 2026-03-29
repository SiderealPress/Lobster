#!/usr/bin/env python3
"""Startup script for the mock Telegram API server used in integration tests."""
import asyncio
import sys

sys.path.insert(0, '/home/testuser/lobster')
from tests.mocks.mock_telegram import MockTelegramServer


async def run():
    server = MockTelegramServer(port=8081)
    await server.start()
    print('Mock Telegram server running on port 8081')
    while True:
        await asyncio.sleep(3600)


asyncio.run(run())
