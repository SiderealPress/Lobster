## HyperFrames — HTML-to-Video Skill

When the user asks to create, render, or produce a video, use HyperFrames.

HyperFrames is an open-source video rendering framework from HeyGen that lets AI agents
produce MP4/WebM/MOV files by writing HTML, CSS, and GSAP JavaScript animations. Agents
are trained on billions of HTML pages — HTML is their native creative medium. HyperFrames
adds a thin layer of `data-*` attributes to standard HTML to define video timelines, then
renders them locally using Puppeteer + FFmpeg. No API keys, no cloud, fully local.

**GitHub:** https://github.com/heygen-com/hyperframes

---

### Dispatcher behavior (main thread, 7-second rule)

This task always takes more than 7 seconds. Do NOT attempt inline.

1. Acknowledge immediately:
   `send_reply(chat_id, "On it — I'll build that video with HyperFrames and report back.", message_id=message_id)`
2. Spawn a background subagent using the prompt template below.
3. Return to `wait_for_messages()`.

---

### When to trigger

- `/video <description>`, `/hyperframes <description>`, `/make-video <description>`
- "make me a video about...", "create a video that shows...", "build a product intro..."
- "turn this into a video", "animate this slide", "render an explainer video"
- "make a title card", "create a motion graphic", "produce a pitch video"

---

### Subagent prompt template

```
You are a HyperFrames video producer. Your job is to create an HTML-based video
composition and render it to MP4.

## User request
{user_message}

## Steps

### 1. Set up the project

```bash
# Install HyperFrames CLI if not present
which hyperframes 2>/dev/null || npm install -g hyperframes

# Create a new project in the lobster-workspace projects dir
cd $LOBSTER_PROJECTS
npx hyperframes init {project_slug}
cd {project_slug}

# Install the agent skills (teaches Claude the correct composition patterns)
npx skills add heygen-com/hyperframes
```

### 2. Plan the video

Before writing HTML, think at a high level:
- **What** — what should the viewer experience? Narrative arc, key moments, emotional beats.
- **Structure** — how many scenes, which carry video/audio/overlays/captions.
- **Timing** — how long per scene, where do transitions land, overall pacing.
- **Layout** — design the end-state (fully-visible frame) first, THEN add animations.
- **Style** — dark or light, mood (cinematic, technical, warm), brand colors if specified.

### 3. Write the composition (index.html)

Key rules (non-negotiable):
- Every element needs `data-start`, `data-duration`, `data-track-index` attributes.
- Root composition also needs `data-composition-id`, `data-width`, `data-height`.
- All GSAP timelines must start `{ paused: true }` and register as `window.__timelines["<id>"] = tl`.
- Use `gsap.from()` for entrances (animate FROM invisible/offscreen TO CSS position).
- Use `gsap.to()` for exits only on the FINAL scene.
- NEVER use `repeat: -1` — infinite loops break the capture engine.
- NEVER build timelines inside `async/await/setTimeout/Promise`.
- Always use transitions between scenes — no jump cuts.
- Minimum font sizes: 60px+ headlines, 20px+ body, 16px+ data labels.

Example minimal composition:
```html
<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="UTF-8">
  <script src="https://cdn.jsdelivr.net/npm/gsap@3.14.2/dist/gsap.min.js"></script>
  <style>
    body { margin:0; width:1920px; height:1080px; overflow:hidden; background:#0D1B2A; }
    .scene { position:absolute; inset:0; }
    .content { display:flex; flex-direction:column; justify-content:center;
                width:100%; height:100%; padding:120px 160px; gap:24px; box-sizing:border-box; }
    h1 { font-size:96px; color:#fff; }
    p  { font-size:36px; color:#aaa; }
  </style>
</head>
<body>
  <div id="root" data-composition-id="my-video"
       data-width="1920" data-height="1080"
       data-start="0" data-duration="8">
    <div id="scene1" class="scene">
      <div class="content">
        <h1>Your Title Here</h1>
        <p>Your subtitle or tagline</p>
      </div>
    </div>
  </div>
  <script>
    window.__timelines = window.__timelines || {};
    const tl = gsap.timeline({ paused: true });
    tl.from("h1", { y:60, opacity:0, duration:0.7, ease:"power3.out" }, 0.3);
    tl.from("p",  { y:40, opacity:0, duration:0.5, ease:"power2.out" }, 0.6);
    window.__timelines["my-video"] = tl;
  </script>
</body>
</html>
```

### 4. Lint and validate

```bash
npx hyperframes lint        # Check data attribute correctness
npx hyperframes validate    # WCAG contrast audit + animation check
```

Fix any warnings before rendering.

### 5. Preview (optional — skip if running headless/CI)

```bash
npx hyperframes preview     # Opens browser with live reload
```

### 6. Render

```bash
npx hyperframes render      # Renders to out/my-video.mp4 by default
```

Output is saved to `{project_dir}/out/`. Default format is MP4.
For other formats: `npx hyperframes render --format webm` or `--format mov`.

### 7. Report back

Use `write_result` MCP tool to report:
- What was created (title, duration, dimensions)
- Path to the rendered file
- Any issues encountered and how they were resolved

If the render fails, share the error message from `npx hyperframes doctor` output.
```

---

### Common video types and guidance

**Product intro (15-30s)**
- 3-4 scenes: hook → problem → solution → CTA
- Start with a bold full-screen title, end with logo + URL
- Use `data-volume` on background music: 0.15-0.25

**Explainer / pitch (30-90s)**
- Plan a narrative arc before writing HTML
- One claim or idea per scene, 5-8s each
- Data visualizations: see `npx hyperframes add data-chart` for ready-made blocks

**Title card / motion graphic (3-10s)**
- Single composition, one or two scenes max
- Typography-forward: large font, bold entrance animations
- GSAP stagger pattern: `tl.from(".words", { y:30, opacity:0, stagger:0.1, duration:0.5 })`

**Social / vertical (TikTok / Instagram Reels)**
- Set dimensions to 1080x1920 on the root composition
- Bold captions (80px+), high contrast, snappy animations (<0.3s each)

---

### Install HyperFrames

One command installs the CLI and agent skills:

```bash
npx hyperframes init my-video   # Creates project + installs skills automatically
# OR install globally:
npm install -g hyperframes
npx skills add heygen-com/hyperframes
```

Requirements: Node.js >= 22, FFmpeg (`sudo apt install ffmpeg` or `brew install ffmpeg`)

---

### Catalog (ready-to-use blocks)

HyperFrames ships 50+ reusable blocks — install any into your project:

```bash
npx hyperframes add flash-through-white   # Shader transition
npx hyperframes add instagram-follow      # Social media overlay
npx hyperframes add data-chart            # Animated chart
```

Browse the full catalog: https://hyperframes.heygen.com/catalog

---

### Troubleshooting

Run `npx hyperframes doctor` for a full diagnostic.

Common issues:
- **Render hangs** — likely an `async` timeline or `repeat: -1` in GSAP
- **Black frames** — missing `window.__timelines` registration
- **Audio out of sync** — use separate `<audio>` element, not embedded in `<video>`
- **Font not found** — check `hyperframes lint` output for supported font list
- **Contrast warnings** — brighten/darken the color until WCAG 4.5:1 passes, then re-run `validate`
