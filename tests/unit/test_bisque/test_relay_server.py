"""Tests for bisque relay server v2 -- WS integration, auth, messages, replay, stress."""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import Any

import aiohttp
import pytest


# =============================================================================
# Helpers
# =============================================================================


async def _get_session_token(base_url: str, bootstrap_token: str = "test-bootstrap-token") -> str:
    """Exchange bootstrap token for session token via HTTP POST."""
    async with aiohttp.ClientSession() as session:
        async with session.post(
            f"{base_url}/auth/exchange",
            json={"token": bootstrap_token},
        ) as resp:
            assert resp.status == 200
            data = await resp.json()
            return data["sessionToken"]


async def _ws_connect(base_url: str):
    """Create an aiohttp ClientSession + WS connection. Caller must close both."""
    session = aiohttp.ClientSession()
    ws = await session.ws_connect(f"{base_url}/")
    return session, ws


async def _auth_ws(base_url: str, token: str):
    """Connect, auth, read auth_success + snapshot. Returns (session, ws, snapshot_data)."""
    session, ws = await _ws_connect(base_url)
    await ws.send_json({"v": 2, "type": "auth", "token": token})

    msg = await asyncio.wait_for(ws.receive(), timeout=5)
    data = json.loads(msg.data)
    assert data["type"] == "auth_success"

    msg = await asyncio.wait_for(ws.receive(), timeout=5)
    snapshot = json.loads(msg.data)
    assert snapshot["type"] == "snapshot"

    return session, ws, snapshot


async def _close(session, ws):
    """Close ws and session."""
    await ws.close()
    await session.close()


# =============================================================================
# HTTP auth exchange
# =============================================================================


class TestHTTPAuthExchange:
    async def test_exchange_success(self, relay_server):
        url = relay_server["ws_url"]
        async with aiohttp.ClientSession() as s:
            async with s.post(f"{url}/auth/exchange", json={"token": "test-bootstrap-token"}) as resp:
                assert resp.status == 200
                data = await resp.json()
                assert "sessionToken" in data
                assert data["email"] == "test@example.com"

    async def test_exchange_invalid_token(self, relay_server):
        url = relay_server["ws_url"]
        async with aiohttp.ClientSession() as s:
            async with s.post(f"{url}/auth/exchange", json={"token": "bad"}) as resp:
                assert resp.status == 401

    async def test_exchange_missing_token(self, relay_server):
        url = relay_server["ws_url"]
        async with aiohttp.ClientSession() as s:
            async with s.post(f"{url}/auth/exchange", json={}) as resp:
                assert resp.status == 400

    async def test_exchange_invalid_json(self, relay_server):
        url = relay_server["ws_url"]
        async with aiohttp.ClientSession() as s:
            async with s.post(
                f"{url}/auth/exchange",
                data=b"not json {{{",
                headers={"Content-Type": "application/json"},
            ) as resp:
                assert resp.status == 400


# =============================================================================
# WebSocket auth
# =============================================================================


class TestWSAuth:
    async def test_auth_success(self, relay_server):
        url = relay_server["ws_url"]
        token = relay_server["token_store"].create_session("ws@test.com")
        session, ws = await _ws_connect(url)
        try:
            await ws.send_json({"v": 2, "type": "auth", "token": token})
            msg = await asyncio.wait_for(ws.receive(), timeout=5)
            data = json.loads(msg.data)
            assert data["type"] == "auth_success"
            assert data["email"] == "ws@test.com"
        finally:
            await _close(session, ws)

    async def test_auth_timeout(self, relay_server):
        url = relay_server["ws_url"]
        session, ws = await _ws_connect(url)
        try:
            msg = await asyncio.wait_for(ws.receive(), timeout=10)
            data = json.loads(msg.data)
            assert data["type"] == "auth_error"
        finally:
            await _close(session, ws)

    async def test_auth_invalid_token(self, relay_server):
        url = relay_server["ws_url"]
        session, ws = await _ws_connect(url)
        try:
            await ws.send_json({"v": 2, "type": "auth", "token": "invalid"})
            msg = await asyncio.wait_for(ws.receive(), timeout=5)
            data = json.loads(msg.data)
            assert data["type"] == "auth_error"
        finally:
            await _close(session, ws)

    async def test_auth_wrong_frame_type(self, relay_server):
        url = relay_server["ws_url"]
        session, ws = await _ws_connect(url)
        try:
            await ws.send_json({"v": 2, "type": "ping"})
            msg = await asyncio.wait_for(ws.receive(), timeout=5)
            data = json.loads(msg.data)
            assert data["type"] == "auth_error"
        finally:
            await _close(session, ws)

    async def test_snapshot_on_connect(self, relay_server):
        url = relay_server["ws_url"]
        token = relay_server["token_store"].create_session("snap@test.com")
        session, ws, snapshot = await _auth_ws(url, token)
        try:
            assert snapshot["status"] == "idle"
        finally:
            await _close(session, ws)


# =============================================================================
# Messages
# =============================================================================


class TestMessages:
    async def test_send_message_creates_inbox_file(self, relay_server):
        url = relay_server["ws_url"]
        dirs = relay_server["dirs"]
        token = relay_server["token_store"].create_session("msg@test.com")
        session, ws, _ = await _auth_ws(url, token)

        try:
            await ws.send_json({"v": 2, "type": "send_message", "text": "Hello from test"})
            msg = await asyncio.wait_for(ws.receive(), timeout=5)
            data = json.loads(msg.data)
            assert data["type"] == "ack"
            assert "message_id" in data

            inbox_files = list(dirs["inbox"].glob("bisque_*.json"))
            assert len(inbox_files) >= 1
            content = json.loads(inbox_files[0].read_text())
            assert content["text"] == "Hello from test"
            assert content["source"] == "bisque"
        finally:
            await _close(session, ws)

    async def test_send_message_empty_rejected(self, relay_server):
        url = relay_server["ws_url"]
        token = relay_server["token_store"].create_session("empty@test.com")
        session, ws, _ = await _auth_ws(url, token)

        try:
            await ws.send_json({"v": 2, "type": "send_message", "text": "   "})
            msg = await asyncio.wait_for(ws.receive(), timeout=5)
            data = json.loads(msg.data)
            assert data["type"] == "error"
        finally:
            await _close(session, ws)

    async def test_send_message_too_long_rejected(self, relay_server):
        url = relay_server["ws_url"]
        token = relay_server["token_store"].create_session("long@test.com")
        session, ws, _ = await _auth_ws(url, token)

        try:
            await ws.send_json({"v": 2, "type": "send_message", "text": "x" * 33000})
            msg = await asyncio.wait_for(ws.receive(), timeout=5)
            data = json.loads(msg.data)
            assert data["type"] == "error"
            assert "too long" in data["message"].lower()
        finally:
            await _close(session, ws)

    async def test_ping_pong(self, relay_server):
        url = relay_server["ws_url"]
        token = relay_server["token_store"].create_session("ping@test.com")
        session, ws, _ = await _auth_ws(url, token)

        try:
            await ws.send_json({"v": 2, "type": "ping"})
            msg = await asyncio.wait_for(ws.receive(), timeout=5)
            data = json.loads(msg.data)
            assert data["type"] == "pong"
        finally:
            await _close(session, ws)

    async def test_binary_frame_rejected(self, relay_server):
        url = relay_server["ws_url"]
        token = relay_server["token_store"].create_session("bin@test.com")
        session, ws, _ = await _auth_ws(url, token)

        try:
            await ws.send_bytes(b"\x00\x01\x02")
            msg = await asyncio.wait_for(ws.receive(), timeout=5)
            data = json.loads(msg.data)
            assert data["type"] == "error"
        finally:
            await _close(session, ws)


# =============================================================================
# Event delivery (outbox → clients)
# =============================================================================


class TestEventDelivery:
    async def test_outbox_file_delivered(self, relay_server):
        url = relay_server["ws_url"]
        dirs = relay_server["dirs"]
        token = relay_server["token_store"].create_session("outbox@test.com")
        session, ws, _ = await _auth_ws(url, token)

        try:
            msg_data = {
                "id": "out-1",
                "source": "bisque",
                "chat_id": "outbox@test.com",
                "text": "Reply from Lobster",
                "timestamp": "2025-01-01T00:00:00Z",
            }
            (dirs["bisque_outbox"] / "out-1.json").write_text(json.dumps(msg_data))

            msg = await asyncio.wait_for(ws.receive(), timeout=10)
            data = json.loads(msg.data)
            assert data["type"] == "message"
            assert data["text"] == "Reply from Lobster"
            assert data["role"] == "assistant"
        finally:
            await _close(session, ws)

    async def test_wire_event_delivered(self, relay_server):
        url = relay_server["ws_url"]
        dirs = relay_server["dirs"]
        token = relay_server["token_store"].create_session("wire@test.com")
        session, ws, _ = await _auth_ws(url, token)

        try:
            event = {"type": "status", "status": "thinking", "detail": "Processing"}
            (dirs["wire_events"] / "evt-1.json").write_text(json.dumps(event))

            msg = await asyncio.wait_for(ws.receive(), timeout=10)
            data = json.loads(msg.data)
            assert data["type"] == "status"
            assert data["status"] == "thinking"
        finally:
            await _close(session, ws)

    async def test_multi_client_fan_out(self, relay_server):
        url = relay_server["ws_url"]
        dirs = relay_server["dirs"]
        store = relay_server["token_store"]

        token1 = store.create_session("fan1@test.com")
        token2 = store.create_session("fan2@test.com")

        s1, ws1, _ = await _auth_ws(url, token1)
        s2, ws2, _ = await _auth_ws(url, token2)

        try:
            msg_data = {"id": "fan-1", "text": "Broadcast", "chat_id": "x"}
            (dirs["bisque_outbox"] / "fan-1.json").write_text(json.dumps(msg_data))

            m1 = await asyncio.wait_for(ws1.receive(), timeout=10)
            m2 = await asyncio.wait_for(ws2.receive(), timeout=10)
            assert json.loads(m1.data)["type"] == "message"
            assert json.loads(m2.data)["type"] == "message"
        finally:
            await _close(s1, ws1)
            await _close(s2, ws2)


# =============================================================================
# Replay
# =============================================================================


class TestReplay:
    async def test_replay_missed_events(self, relay_server):
        url = relay_server["ws_url"]
        event_log = relay_server["event_log"]
        store = relay_server["token_store"]

        event_log.append("evt-old", json.dumps({"v": 2, "type": "status", "status": "idle", "id": "a", "ts": "t"}))
        event_log.append("evt-new", json.dumps({"v": 2, "type": "status", "status": "thinking", "id": "b", "ts": "t"}))

        token = store.create_session("replay@test.com")

        session, ws = await _ws_connect(url)
        try:
            await ws.send_json({"v": 2, "type": "auth", "token": token, "last_event_id": "evt-old"})

            msg = await asyncio.wait_for(ws.receive(), timeout=5)
            assert json.loads(msg.data)["type"] == "auth_success"

            # Should get replay, not snapshot
            msg = await asyncio.wait_for(ws.receive(), timeout=5)
            data = json.loads(msg.data)
            assert data["type"] == "status"
            assert data["status"] == "thinking"
        finally:
            await _close(session, ws)

    async def test_stale_id_gets_snapshot(self, relay_server):
        url = relay_server["ws_url"]
        store = relay_server["token_store"]
        token = store.create_session("stale@test.com")

        session, ws = await _ws_connect(url)
        try:
            await ws.send_json({"v": 2, "type": "auth", "token": token, "last_event_id": "nonexistent"})

            msg = await asyncio.wait_for(ws.receive(), timeout=5)
            assert json.loads(msg.data)["type"] == "auth_success"

            msg = await asyncio.wait_for(ws.receive(), timeout=5)
            assert json.loads(msg.data)["type"] == "snapshot"
        finally:
            await _close(session, ws)

    async def test_no_last_event_id_gets_snapshot(self, relay_server):
        url = relay_server["ws_url"]
        store = relay_server["token_store"]
        token = store.create_session("new@test.com")

        session, ws = await _ws_connect(url)
        try:
            await ws.send_json({"v": 2, "type": "auth", "token": token})

            msg = await asyncio.wait_for(ws.receive(), timeout=5)
            assert json.loads(msg.data)["type"] == "auth_success"

            msg = await asyncio.wait_for(ws.receive(), timeout=5)
            assert json.loads(msg.data)["type"] == "snapshot"
        finally:
            await _close(session, ws)


# =============================================================================
# Edge cases
# =============================================================================


class TestEdgeCases:
    async def test_rapid_messages(self, relay_server):
        url = relay_server["ws_url"]
        store = relay_server["token_store"]
        token = store.create_session("rapid@test.com")
        session, ws, _ = await _auth_ws(url, token)

        try:
            for i in range(50):
                await ws.send_json({"v": 2, "type": "send_message", "text": f"Message {i}"})

            acks = []
            for _ in range(50):
                msg = await asyncio.wait_for(ws.receive(), timeout=10)
                data = json.loads(msg.data)
                assert data["type"] == "ack"
                acks.append(data)

            assert len(acks) == 50
        finally:
            await _close(session, ws)

    async def test_large_message(self, relay_server):
        url = relay_server["ws_url"]
        store = relay_server["token_store"]
        token = store.create_session("large@test.com")
        session, ws, _ = await _auth_ws(url, token)

        try:
            await ws.send_json({"v": 2, "type": "send_message", "text": "x" * 30000})
            msg = await asyncio.wait_for(ws.receive(), timeout=5)
            assert json.loads(msg.data)["type"] == "ack"
        finally:
            await _close(session, ws)

    async def test_invalid_json(self, relay_server):
        url = relay_server["ws_url"]
        store = relay_server["token_store"]
        token = store.create_session("json@test.com")
        session, ws, _ = await _auth_ws(url, token)

        try:
            await ws.send_str("not valid json {{{")
            msg = await asyncio.wait_for(ws.receive(), timeout=5)
            data = json.loads(msg.data)
            assert data["type"] == "error"
        finally:
            await _close(session, ws)


# =============================================================================
# Stress tests
# =============================================================================


@pytest.mark.stress
class TestStress:
    async def test_concurrent_connections(self, relay_server):
        url = relay_server["ws_url"]
        store = relay_server["token_store"]

        connections = []
        for i in range(20):
            token = store.create_session(f"stress{i}@test.com")
            s, ws, _ = await _auth_ws(url, token)
            connections.append((s, ws))

        try:
            for _, ws in connections:
                await ws.send_json({"v": 2, "type": "ping"})

            for _, ws in connections:
                msg = await asyncio.wait_for(ws.receive(), timeout=10)
                assert json.loads(msg.data)["type"] == "pong"
        finally:
            for s, ws in connections:
                await _close(s, ws)

    async def test_rapid_events_to_multiple_clients(self, relay_server):
        url = relay_server["ws_url"]
        dirs = relay_server["dirs"]
        store = relay_server["token_store"]

        connections = []
        for i in range(5):
            token = store.create_session(f"multi{i}@test.com")
            s, ws, _ = await _auth_ws(url, token)
            connections.append((s, ws))

        try:
            for i in range(20):
                msg_data = {"id": f"rapid-{i}", "text": f"Rapid {i}", "chat_id": "x"}
                (dirs["bisque_outbox"] / f"rapid-{i}.json").write_text(json.dumps(msg_data))
                await asyncio.sleep(0.01)

            for _, ws in connections:
                received = []
                for _ in range(20):
                    try:
                        msg = await asyncio.wait_for(ws.receive(), timeout=15)
                        data = json.loads(msg.data)
                        if data["type"] == "message":
                            received.append(data)
                    except asyncio.TimeoutError:
                        break
                assert len(received) >= 15
        finally:
            for s, ws in connections:
                await _close(s, ws)
