#!/usr/bin/env python3
"""
Image Generation MCP Server for Lobster

Provides image generation tools using Google AI Studio.
Two providers available:
  - Nano Banana 2 (gemini-3.1-flash-image-preview) via generateContent — default
  - Imagen 4 (imagen-4.0-generate-001) via predict endpoint

Tools provided:
- generate_image: Generate an image from a text prompt and optionally send to Telegram
- send_image: Send an image URL or local file path to a Telegram chat as a photo message
"""

import asyncio
import base64
import json
import os
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import httpx

from mcp.server import Server
from mcp.server.stdio import stdio_server
from mcp.types import Tool, TextContent

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

_HOME = Path.home()
_MESSAGES = Path(os.environ.get("LOBSTER_MESSAGES", _HOME / "messages"))
_WORKSPACE = Path(os.environ.get("LOBSTER_WORKSPACE", _HOME / "lobster-workspace"))

OUTBOX_DIR = _MESSAGES / "outbox"
SENT_DIR = _MESSAGES / "sent"
GENERATED_IMAGES_DIR = _MESSAGES / "images" / "generated"

# Ensure directories exist
for _d in [OUTBOX_DIR, SENT_DIR, GENERATED_IMAGES_DIR]:
    _d.mkdir(parents=True, exist_ok=True)

# ---------------------------------------------------------------------------
# API key resolution (env → lobster-config → lobster/config)
# ---------------------------------------------------------------------------

def _load_env_file(path: Path) -> dict[str, str]:
    """Parse a simple KEY=VALUE env file. Ignores comments and blank lines."""
    result: dict[str, str] = {}
    if not path.exists():
        return result
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        if "=" not in line:
            continue
        key, _, val = line.partition("=")
        key = key.strip()
        val = val.strip().strip('"').strip("'")
        if key:
            result[key] = val
    return result


def _resolve_key(key_name: str) -> str:
    """Look up an API key: environment → lobster-config → lobster/config."""
    # 1. Environment variable
    val = os.environ.get(key_name, "")
    if val:
        return val

    # 2. lobster-config/config.env (private overlay)
    config_dir = os.environ.get("LOBSTER_CONFIG_DIR", str(_HOME / "lobster-config"))
    for env_file in [
        Path(config_dir) / "config.env",
        Path(config_dir) / "global.env",
        _HOME / "lobster" / "config" / "config.env",
    ]:
        env_vars = _load_env_file(env_file)
        if key_name in env_vars and env_vars[key_name]:
            return env_vars[key_name]

    return ""


GOOGLE_AI_STUDIO_KEY = _resolve_key("GOOGLE_AI_STUDIO_KEY")

# Default provider — can be overridden via skill preferences
DEFAULT_PROVIDER = os.environ.get("IMAGE_GEN_PROVIDER", "nano-banana-2")

# Nano Banana 2 (Gemini 3.1 Flash Image) via generateContent
NANO_BANANA_MODEL = "gemini-3.1-flash-image-preview"
NANO_BANANA_API_URL = f"https://generativelanguage.googleapis.com/v1beta/models/{NANO_BANANA_MODEL}:generateContent"

# Imagen 4 via predict endpoint
IMAGEN4_MODEL = "imagen-4.0-generate-001"
IMAGEN4_API_URL = f"https://generativelanguage.googleapis.com/v1beta/models/{IMAGEN4_MODEL}:predict"

# ---------------------------------------------------------------------------
# Image generation functions
# ---------------------------------------------------------------------------

def _save_image(image_bytes: bytes, prompt: str, ext: str = "png") -> Path:
    """Decode and save image bytes to disk. Returns the local path."""
    ts = int(time.time())
    safe_prompt = "".join(c if c.isalnum() or c in "-_ " else "_" for c in prompt[:50]).strip()
    filename = f"{ts}_{safe_prompt[:40]}.{ext}"
    local_path = GENERATED_IMAGES_DIR / filename
    local_path.write_bytes(image_bytes)
    return local_path


async def _generate_with_nano_banana(
    prompt: str,
    client: httpx.AsyncClient | None = None,
) -> dict:
    """Generate image using Nano Banana 2 (gemini-3.1-flash-image-preview).

    Uses the generateContent endpoint with responseModalities=["TEXT", "IMAGE"].
    Returns dict with 'local_path' (str) on success, or 'error' (str) on failure.
    """
    if not GOOGLE_AI_STUDIO_KEY:
        return {"error": "GOOGLE_AI_STUDIO_KEY not configured. Add it to ~/lobster-config/config.env"}

    payload = {
        "contents": [{"parts": [{"text": prompt}]}],
        "generationConfig": {
            "responseModalities": ["TEXT", "IMAGE"],
        },
    }

    headers = {
        "x-goog-api-key": GOOGLE_AI_STUDIO_KEY,
        "Content-Type": "application/json",
    }

    own_client = client is None
    if own_client:
        client = httpx.AsyncClient(timeout=120.0)

    try:
        resp = await client.post(NANO_BANANA_API_URL, json=payload, headers=headers)
        resp.raise_for_status()
        data = resp.json()

        candidates = data.get("candidates", [])
        if not candidates:
            return {"error": f"No candidates in Nano Banana 2 response: {json.dumps(data)[:300]}"}

        parts = candidates[0].get("content", {}).get("parts", [])
        b64_bytes = ""
        mime_type = "image/png"
        for part in parts:
            inline = part.get("inlineData", {})
            if inline.get("data"):
                b64_bytes = inline["data"]
                mime_type = inline.get("mimeType", "image/png")
                break

        if not b64_bytes:
            return {"error": f"No inlineData in Nano Banana 2 response: {json.dumps(parts)[:300]}"}

        ext = "jpg" if "jpeg" in mime_type else "png"
        local_path = _save_image(base64.b64decode(b64_bytes), prompt, ext)

        return {
            "local_path": str(local_path),
            "model": NANO_BANANA_MODEL,
            "provider": "nano-banana-2",
        }

    except httpx.HTTPStatusError as e:
        body = e.response.text[:400] if e.response else ""
        return {"error": f"Nano Banana 2 API error {e.response.status_code}: {body}"}
    except Exception as e:
        return {"error": f"Nano Banana 2 request failed: {e}"}
    finally:
        if own_client:
            await client.aclose()


async def _generate_with_imagen4(
    prompt: str,
    aspect_ratio: str = "1:1",
    client: httpx.AsyncClient | None = None,
) -> dict:
    """Generate image using Imagen 4 (imagen-4.0-generate-001).

    Uses the predict endpoint. Supports aspect_ratio and negative_prompt.
    Returns dict with 'local_path' (str) on success, or 'error' (str) on failure.
    """
    if not GOOGLE_AI_STUDIO_KEY:
        return {"error": "GOOGLE_AI_STUDIO_KEY not configured. Add it to ~/lobster-config/config.env"}

    payload = {
        "instances": [{"prompt": prompt}],
        "parameters": {
            "sampleCount": 1,
            "aspectRatio": aspect_ratio,
        },
    }

    headers = {
        "x-goog-api-key": GOOGLE_AI_STUDIO_KEY,
        "Content-Type": "application/json",
    }

    own_client = client is None
    if own_client:
        client = httpx.AsyncClient(timeout=120.0)

    try:
        resp = await client.post(IMAGEN4_API_URL, json=payload, headers=headers)
        resp.raise_for_status()
        data = resp.json()

        predictions = data.get("predictions", [])
        if not predictions:
            return {"error": f"No predictions in Imagen 4 response: {json.dumps(data)[:300]}"}

        b64_bytes = predictions[0].get("bytesBase64Encoded", "")
        if not b64_bytes:
            return {"error": f"No image bytes in Imagen 4 response: {json.dumps(predictions[0])[:300]}"}

        local_path = _save_image(base64.b64decode(b64_bytes), prompt)

        return {
            "local_path": str(local_path),
            "model": IMAGEN4_MODEL,
            "provider": "imagen-4",
        }

    except httpx.HTTPStatusError as e:
        body = e.response.text[:400] if e.response else ""
        return {"error": f"Imagen 4 API error {e.response.status_code}: {body}"}
    except Exception as e:
        return {"error": f"Imagen 4 request failed: {e}"}
    finally:
        if own_client:
            await client.aclose()


def _atomic_write_json(path: Path, data: dict) -> None:
    """Write JSON atomically using a temp file + rename."""
    import tempfile
    path.parent.mkdir(parents=True, exist_ok=True)
    content = json.dumps(data, indent=2)
    fd, tmp_path = tempfile.mkstemp(dir=str(path.parent), suffix=".tmp")
    try:
        with os.fdopen(fd, "w") as f:
            f.write(content)
            f.flush()
            os.fsync(f.fileno())
        os.rename(tmp_path, str(path))
    except BaseException:
        try:
            os.unlink(tmp_path)
        except OSError:
            pass
        raise


def _queue_photo_outbox(chat_id: Any, photo_path_or_url: str, caption: str = "") -> str:
    """Write a photo outbox file for lobster_bot.py to pick up and send via send_photo."""
    reply_id = f"{int(time.time() * 1000)}_telegram_photo"
    reply_data = {
        "id": reply_id,
        "source": "telegram",
        "type": "photo",
        "chat_id": chat_id,
        "photo_url": photo_path_or_url,
        "caption": caption,
        "timestamp": datetime.now(timezone.utc).isoformat(),
    }
    outbox_file = OUTBOX_DIR / f"{reply_id}.json"
    _atomic_write_json(outbox_file, reply_data)
    # Also save to sent for audit
    sent_file = SENT_DIR / f"{reply_id}.json"
    _atomic_write_json(sent_file, reply_data)
    return reply_id


# ---------------------------------------------------------------------------
# MCP Server
# ---------------------------------------------------------------------------

server = Server("image-generation")


def text_result(data: Any) -> list[TextContent]:
    if isinstance(data, str):
        return [TextContent(type="text", text=data)]
    return [TextContent(type="text", text=json.dumps(data, indent=2))]


def error_result(msg: str) -> list[TextContent]:
    return [TextContent(type="text", text=f"Error: {msg}")]


@server.list_tools()
async def list_tools() -> list[Tool]:
    """List available image generation tools."""
    return [
        Tool(
            name="generate_image",
            description=(
                "Generate an image from a text prompt using Google AI Studio. "
                "Two providers: 'nano-banana-2' (Gemini 3.1 Flash Image, default) or "
                "'imagen-4' (Imagen 4, dedicated image model with aspect ratio control). "
                "Saves the generated image locally and optionally sends it directly to a "
                "Telegram chat as a photo. Returns the local file path.\n\n"
                "Requires GOOGLE_AI_STUDIO_KEY in ~/lobster-config/config.env.\n"
                "The image is saved to ~/messages/images/generated/<timestamp>_<prompt>.png."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "prompt": {
                        "type": "string",
                        "description": "Text description of the image to generate. Be specific and detailed for best results.",
                    },
                    "chat_id": {
                        "oneOf": [{"type": "integer"}, {"type": "string"}],
                        "description": "Optional Telegram chat_id. If provided, the generated image is automatically sent as a Telegram photo.",
                    },
                    "caption": {
                        "type": "string",
                        "description": "Optional caption for the Telegram photo. Defaults to the prompt (truncated to 200 chars).",
                        "default": "",
                    },
                    "provider": {
                        "type": "string",
                        "enum": ["nano-banana-2", "imagen-4"],
                        "description": "Image generation provider. 'nano-banana-2' (default): Gemini 3.1 Flash Image, fast, supports text+image mixed output. 'imagen-4': dedicated image model, supports aspect_ratio.",
                    },
                    "aspect_ratio": {
                        "type": "string",
                        "enum": ["1:1", "3:4", "4:3", "9:16", "16:9"],
                        "description": "Aspect ratio for the generated image. Only supported by imagen-4 provider. Default: 1:1.",
                    },
                },
                "required": ["prompt"],
            },
        ),
        Tool(
            name="send_image",
            description=(
                "Send an image to a Telegram chat as a photo (not just a link). "
                "The image can be a URL (http/https) or a local file path. "
                "Writes a photo outbox file for lobster_bot.py to deliver via send_photo."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "chat_id": {
                        "oneOf": [{"type": "integer"}, {"type": "string"}],
                        "description": "Telegram chat_id to send the photo to.",
                    },
                    "photo": {
                        "type": "string",
                        "description": "URL (https://...) or local file path to the image to send.",
                    },
                    "caption": {
                        "type": "string",
                        "description": "Optional caption for the photo.",
                        "default": "",
                    },
                },
                "required": ["chat_id", "photo"],
            },
        ),
    ]


@server.call_tool()
async def call_tool(name: str, arguments: dict[str, Any]) -> list[TextContent]:
    """Dispatch tool calls."""
    try:
        if name == "generate_image":
            return await handle_generate_image(arguments)
        elif name == "send_image":
            return await handle_send_image(arguments)
        else:
            return error_result(f"Unknown tool: {name}")
    except Exception as e:
        return error_result(f"Tool '{name}' failed: {e}")


async def handle_generate_image(args: dict) -> list[TextContent]:
    """Generate an image from a prompt, optionally sending to Telegram."""
    prompt = str(args.get("prompt", "")).strip()
    if not prompt:
        return error_result("'prompt' is required")

    chat_id = args.get("chat_id")
    caption = str(args.get("caption", "")).strip() or prompt[:200]
    provider = str(args.get("provider", DEFAULT_PROVIDER)).strip()
    aspect_ratio = str(args.get("aspect_ratio", "1:1")).strip()

    if not GOOGLE_AI_STUDIO_KEY:
        return error_result(
            "GOOGLE_AI_STUDIO_KEY not configured. "
            "Add it to ~/lobster-config/config.env"
        )

    print(f"[INFO] Generating with {provider}: {prompt[:80]}...", file=sys.stderr)

    if provider == "imagen-4":
        result = await _generate_with_imagen4(prompt=prompt, aspect_ratio=aspect_ratio)
    else:
        result = await _generate_with_nano_banana(prompt=prompt)

    if result.get("error"):
        return error_result(result["error"])

    local_path = result["local_path"]
    provider_name = result.get("provider", provider)
    model_name = result.get("model", "unknown")

    output_lines = [
        "Image generated successfully!",
        f"Provider: {provider_name} ({model_name})",
        f"Saved locally: {local_path}",
    ]

    if chat_id is not None:
        reply_id = _queue_photo_outbox(chat_id, local_path, caption)
        output_lines.append(f"Queued for Telegram delivery (chat_id={chat_id}, reply_id={reply_id})")

    return text_result("\n".join(output_lines))


async def handle_send_image(args: dict) -> list[TextContent]:
    """Queue a photo to be sent to a Telegram chat."""
    chat_id = args.get("chat_id")
    photo = str(args.get("photo", "")).strip()
    caption = str(args.get("caption", "")).strip()

    if not chat_id:
        return error_result("'chat_id' is required")
    if not photo:
        return error_result("'photo' (URL or file path) is required")

    reply_id = _queue_photo_outbox(chat_id, photo, caption)
    return text_result(f"Photo queued for delivery to chat_id={chat_id} (reply_id={reply_id})")


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

async def main():
    print("[INFO] Image Generation MCP Server starting...", file=sys.stderr)
    print(f"[INFO] GOOGLE_AI_STUDIO_KEY: {'configured' if GOOGLE_AI_STUDIO_KEY else 'not set'}", file=sys.stderr)
    print(f"[INFO] Default provider: {DEFAULT_PROVIDER}", file=sys.stderr)
    print(f"[INFO] Nano Banana 2: {NANO_BANANA_API_URL}", file=sys.stderr)
    print(f"[INFO] Imagen 4: {IMAGEN4_API_URL}", file=sys.stderr)
    print(f"[INFO] Outbox dir: {OUTBOX_DIR}", file=sys.stderr)

    async with stdio_server() as (read_stream, write_stream):
        await server.run(
            read_stream,
            write_stream,
            server.create_initialization_options(),
        )


if __name__ == "__main__":
    asyncio.run(main())
