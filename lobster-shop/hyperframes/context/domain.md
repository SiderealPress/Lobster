# HyperFrames — Domain Knowledge

## What is HyperFrames?

HyperFrames is an open-source video toolchain built by HeyGen (heygen-com/hyperframes) that
treats HTML as the source format for video. It was open-sourced in April 2026 after being used
internally at HeyGen to power their AI Video Agent.

The core insight: AI agents are trained on billions of HTML pages. HTML, CSS, and JavaScript
animation (especially GSAP) are already in their training data at massive scale. By letting
agents write HTML to describe video compositions, you get higher-quality creative output than
any proprietary JSON/XML video DSL could produce.

## How it works

1. You write an HTML file with standard HTML/CSS/JS plus a thin set of `data-*` attributes
   that define a video timeline (start times, durations, tracks, dimensions).
2. GSAP (GreenSock Animation Platform) drives all motion — the same library used on millions
   of production websites.
3. HyperFrames' capture engine (Puppeteer + FFmpeg) seeks through the timeline frame-by-frame
   and assembles the frames into a video file.

The result: same-input = identical-output, fully deterministic rendering. No cloud, no API
keys, runs entirely locally.

## Key data attributes

| Attribute | Required On | Meaning |
|---|---|---|
| `data-composition-id` | Root div | Unique ID for the composition |
| `data-width` / `data-height` | Root div | Pixel dimensions (e.g. 1920x1080) |
| `data-start` | Every clip | When this clip starts (seconds, or `"clip-id"`, or `"clip-id + 2"`) |
| `data-duration` | img, div, compositions | How long this clip lasts (seconds) |
| `data-track-index` | Every clip | Integer track. Same-track clips cannot overlap. |
| `data-volume` | audio/video | Volume 0–1 |
| `data-media-start` | audio/video | Trim offset into source (seconds) |
| `data-composition-src` | Sub-composition | Path to external HTML file |

## Common dimensions

| Format | Width x Height |
|---|---|
| 16:9 (YouTube, LinkedIn) | 1920 x 1080 |
| 9:16 (TikTok, Reels) | 1080 x 1920 |
| 1:1 (Instagram square) | 1080 x 1080 |
| 4:5 (Instagram portrait) | 1080 x 1350 |

## CLI commands

```bash
npx hyperframes init <project>      # Scaffold a new project
npx hyperframes preview             # Browser preview with live reload
npx hyperframes render              # Render to MP4 (out/<composition-id>.mp4)
npx hyperframes render --format webm
npx hyperframes lint                # Validate data attributes
npx hyperframes validate            # WCAG contrast + animation audit
npx hyperframes add <block-name>    # Install a block from the catalog
npx hyperframes doctor              # Full environment diagnostic
npx hyperframes tts                 # Generate TTS narration (Kokoro-82M, local)
npx hyperframes transcribe          # Transcribe audio (local Whisper)
```

## Skills system

HyperFrames ships agent "skills" — markdown files that teach Claude (and other agents)
framework-specific patterns. Install with:

```bash
npx skills add heygen-com/hyperframes
```

This registers `/hyperframes`, `/hyperframes-cli`, `/hyperframes-registry`, and `/gsap`
as slash commands in Claude Code.

## Animation runtime

- **GSAP 3.14+** is the primary animation library. Load from CDN:
  `https://cdn.jsdelivr.net/npm/gsap@3.14.2/dist/gsap.min.js`
- CSS animations, Lottie, Three.js, D3 visualizations all work too
- The capture engine drives the GSAP timeline seekably (not wall-clock time)

## Package structure

| Package | Purpose |
|---|---|
| `hyperframes` (CLI) | Create, preview, lint, render |
| `@hyperframes/core` | Types, parsers, linter, runtime |
| `@hyperframes/engine` | Puppeteer-based capture engine |
| `@hyperframes/producer` | Full render pipeline (capture + encode + audio mix) |
| `@hyperframes/studio` | Browser-based composition editor UI |
| `@hyperframes/shader-transitions` | WebGL shader transitions |

## License and source

Apache 2.0. Source at https://github.com/heygen-com/hyperframes

Documentation: https://hyperframes.heygen.com
Block catalog: https://hyperframes.heygen.com/catalog
Prompting guide: https://hyperframes.heygen.com/guides/prompting
