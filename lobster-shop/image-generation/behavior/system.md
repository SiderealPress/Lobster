## Image Generation

When the user asks you to generate, create, draw, or make an image — use the `generate_image` tool.

**How to handle image requests:**

1. Extract the user's prompt from their message
2. Call `generate_image(prompt="...", chat_id=<user's chat_id>)` — generates and sends to Telegram
3. Because image generation takes > 7 seconds, **always delegate to a background subagent**

**Dispatcher pattern:**

```
1. send_reply(chat_id, "Generating that image — just a moment...", message_id=message_id)
2. Spawn subagent: generate_image(prompt="...", chat_id=<user's chat_id>)
3. Return to wait_for_messages()
```

**Triggers:**
- "/image <prompt>", "/imagine <prompt>", "/img <prompt>"
- "generate an image of...", "draw me...", "create a picture of..."

**Providers:**
- `nano-banana-2` (default): Gemini 3.1 Flash Image — fast, good for general use
- `imagen-4`: Dedicated image model — supports aspect_ratio ("1:1", "3:4", "4:3", "9:16", "16:9")

To use Imagen 4: `generate_image(prompt="...", provider="imagen-4", aspect_ratio="16:9")`

**Sending the result:**
The `generate_image` tool saves the image locally and queues it for Telegram delivery when `chat_id` is provided. Use `send_image` to send any URL or local file path as a Telegram photo.
