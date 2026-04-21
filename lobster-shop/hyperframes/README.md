# HyperFrames

**Create videos by writing HTML.** Lobster uses [HyperFrames](https://github.com/heygen-com/hyperframes) to produce MP4, WebM, or MOV files from HTML+CSS+GSAP compositions — fully locally, no API keys required.

## What you can do

- `/video make a 30-second product intro for my SaaS`
- `/video create a 9:16 TikTok-style video about [topic] with bouncy captions`
- `/video turn this PDF into a 45-second pitch video`
- `/video make an animated bar chart race from this CSV`
- `/hyperframes create a title card for my YouTube channel`

## How it works

HyperFrames turns HTML files into rendered video. AI agents are trained on the entire web, so
HTML is their native creative language — more capable than any video-specific JSON DSL. You
describe what you want in plain English; Lobster writes the HTML composition and renders it locally
using Puppeteer + FFmpeg.

## Requirements

- Node.js >= 22
- FFmpeg (`sudo apt install ffmpeg` or `brew install ffmpeg`)

Install HyperFrames:

```bash
npm install -g hyperframes
npx skills add heygen-com/hyperframes
```

## Video formats

| Format | Dimensions | Use for |
|---|---|---|
| 16:9 landscape | 1920×1080 | YouTube, LinkedIn, presentations |
| 9:16 portrait | 1080×1920 | TikTok, Instagram Reels, Shorts |
| 1:1 square | 1080×1080 | Instagram, Twitter |

## Links

- GitHub: https://github.com/heygen-com/hyperframes
- Docs: https://hyperframes.heygen.com
- Block catalog: https://hyperframes.heygen.com/catalog
- Prompting guide: https://hyperframes.heygen.com/guides/prompting
