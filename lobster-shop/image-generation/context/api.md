## Image Generation API Reference

### Provider 1: Nano Banana 2 (default)

- **Model**: `gemini-3.1-flash-image-preview`
- **Endpoint**: `POST .../models/gemini-3.1-flash-image-preview:generateContent`
- **Method**: `generateContent` with `responseModalities: ["TEXT", "IMAGE"]`
- **Auth**: `x-goog-api-key` header
- **Response**: `candidates[0].content.parts[].inlineData.data` (base64)
- **Strengths**: Fast, supports text+image mixed output, multi-turn editing

### Provider 2: Imagen 4

- **Model**: `imagen-4.0-generate-001`
- **Endpoint**: `POST .../models/imagen-4.0-generate-001:predict`
- **Method**: `predict` with `instances[].prompt` and `parameters`
- **Auth**: `x-goog-api-key` header
- **Response**: `predictions[0].bytesBase64Encoded` (base64 PNG)
- **Parameters**: `sampleCount` (1-4), `aspectRatio` ("1:1", "3:4", "4:3", "9:16", "16:9")
- **Strengths**: Purpose-built for image generation, aspect ratio control

### Key lookup order

1. Environment variable (`GOOGLE_AI_STUDIO_KEY`)
2. `$LOBSTER_CONFIG_DIR/config.env` (default: `~/lobster-config/config.env`)
3. `~/lobster/config/config.env`

### Sending images to Telegram

The `generate_image` tool saves the decoded PNG locally and writes a photo outbox file.
`lobster_bot.py` picks up the outbox file and sends it using `bot.send_photo()`.

The `send_image` tool accepts any URL or local file path and queues it for Telegram delivery.
