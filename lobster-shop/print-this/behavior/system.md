## print-this Skill

When the user says "print this", "print that", or asks you to print a URL, follow this procedure:

### Trigger detection

Activate when the message contains:
- "print this" or "print that" followed by (or in the same message as) a URL
- A URL with "print" somewhere in the message
- "send me a PDF of", "make a PDF of", "save this as PDF"

### Step-by-step procedure

**1. Acknowledge immediately**
Send a brief ack: "Fetching and printing that — will send the PDF shortly."
Then delegate ALL remaining work to a background subagent (general-purpose).

**2. Subagent: Fetch the content**

Try `fetch_page` first. If blocked (CAPTCHA, bot detection, JS wall), escalate to camofox:
```
camofox_navigate(url)
camofox_snapshot()  # Get accessibility tree for text extraction
```

For **tweets / X.com posts** — CRITICAL: tweets are almost always pointers to real content. Follow this logic:

1. **Fetch the tweet via camofox** (X requires JS):
   ```
   camofox_navigate(tweet_url)
   camofox_snapshot()  # Get accessibility tree
   ```
2. **Extract all URLs from the tweet body** — look for t.co links, substack.com links, any external URL in the tweet text.
3. **Dereference t.co / shortened URLs** to get the real destination:
   ```python
   import requests
   resp = requests.head(tco_url, allow_redirects=True, timeout=10)
   final_url = resp.url
   ```
4. **Decision tree based on what you find:**

   **Case A — Tweet contains one or more external links (most common):**
   - Dereference the primary link to get the real destination URL
   - Fetch the LINKED CONTENT (article, essay, Substack post, etc.) using `fetch_page` or camofox if blocked
   - That linked content is the main document to print
   - Add a small attribution line at the top: `via @handle on X: {tweet_url}`
   - Do NOT print just the tweet text — the tweet is just a pointer

   **Case B — Tweet is part of a thread (same author replying to themselves, no external link):**
   - Use camofox to navigate and extract all tweets in the thread from that author
   - Print the full thread as the document (each tweet as a paragraph)
   - Format as a continuous essay, not individual tweet boxes

   **Case C — Standalone tweet with no external link and no thread (rare):**
   - Print the tweet text, author (@handle), date, like/retweet counts
   - Note at the top: "Standalone tweet — no linked content found"

**Default assumption: if the tweet has any URL in it, always dereference and print the linked content (Case A).** Short tweet text alone is never sufficient — it is a wrapper, not the content.

For **articles / blog posts / web pages**:
- Extract: title, author, publication date, full article text
- Strip navigation, ads, comments sections — only article content
- Preserve headings and paragraph structure

For **GitHub issues / PRs**:
- Extract: title, issue number, author, date opened, body text, first 5-10 comments
- Include labels, status (open/closed/merged)

**3. Subagent: Generate LaTeX source**

Write to `/tmp/print-this-<timestamp>/document.tex`. Use this template:

```latex
\documentclass[12pt,letterpaper]{article}
\usepackage[margin=1in]{geometry}
\usepackage{fontenc}
\usepackage{inputenc}
\usepackage{microtype}
\usepackage{parskip}
\usepackage{hyperref}
\usepackage{fancyhdr}
\usepackage{datetime2}
\usepackage{xcolor}
\usepackage{mdframed}

\pagestyle{fancy}
\fancyhf{}
\fancyhead[L]{\small\textcolor{gray}{Printed by Lobster}}
\fancyhead[R]{\small\textcolor{gray}{\today}}
\fancyfoot[C]{\small\thepage}

\begin{document}

% Title block
{\Large\bfseries TITLE GOES HERE}

\vspace{4pt}
{\small\textcolor{gray}{AUTHOR · SOURCE URL · DATE}}

\vspace{12pt}
\hrule
\vspace{12pt}

% Content goes here

\end{document}
```

**LaTeX escaping rules** (critical — unescaped chars break compilation):
- `&` → `\&`
- `%` → `\%`
- `$` → `\$`
- `#` → `\#`
- `_` → `\_`
- `^` → `\^{}`
- `{` → `\{`
- `}` → `\}`
- `~` → `\textasciitilde{}`
- `\` → `\textbackslash{}`
- `>` and `<` in text → `\textgreater{}` and `\textless{}`
- Unicode: use direct UTF-8 with `\usepackage[utf8]{inputenc}` — most chars work fine
- Em-dash `—` → `---`, en-dash `–` → `--`

For tweets, the LaTeX layout depends on which case applies:

**Case A (linked article — the normal case):** Use the standard article template above. Add a small attribution header before the main content:
```latex
\begin{mdframed}[linecolor=blue!20,backgroundcolor=blue!3]
\small\textcolor{gray}{via \textbf{@HANDLE} on X: \url{TWEET\_URL}}
\end{mdframed}
\vspace{8pt}
```
Then the article title, author, and body follow as normal.

**Case B (thread):** Print each tweet as a paragraph, separated by a thin rule. Use the article title block with "Thread by @HANDLE" as the title:
```latex
{\Large\bfseries Thread by @HANDLE}
\vspace{4pt}
{\small\textcolor{gray}{DATE · \url{TWEET\_URL}}}
\vspace{12pt}\hrule\vspace{12pt}
% Each tweet becomes a paragraph:
TWEET 1 TEXT

\vspace{4pt}\textcolor{gray!50}{\hrule}\vspace{4pt}

TWEET 2 TEXT
% ... and so on
```

**Case C (standalone tweet — last resort):** Use a centered blockquote layout:
```latex
{\Large\bfseries Standalone Tweet}
\vspace{4pt}
{\small\textcolor{gray}{\textbf{@HANDLE} · DATE · \url{TWEET\_URL}}}
\vspace{12pt}\hrule\vspace{12pt}

\begin{mdframed}[linecolor=gray!30,backgroundcolor=gray!5]
TWEET TEXT HERE

\textcolor{gray}{\small LIKES likes · RETWEETS retweets}
\end{mdframed}
```

**4. Subagent: Compile with pdflatex**

```bash
cd /tmp/print-this-<timestamp>/
pdflatex -interaction=nonstopmode document.tex
# If compile fails, check document.log for errors, fix the .tex, retry once
pdflatex -interaction=nonstopmode document.tex  # Second pass for references
```

Check that `document.pdf` was created and is non-empty (> 1KB).

If compilation fails twice, fall back: use a simpler template with just `\begin{document}` and plain paragraphs — no fancy packages.

**5. Subagent: Send as Telegram file attachment**

Read the bot token from the Lobster config (the path is in `LOBSTER_CONFIG_DIR`):
```bash
source "${LOBSTER_CONFIG_DIR}/config.env"
echo $TELEGRAM_BOT_TOKEN
```

Then send via Telegram sendDocument API:
```bash
curl -s -X POST "https://api.telegram.org/bot${TELEGRAM_BOT_TOKEN}/sendDocument" \
  -F "chat_id=${LOBSTER_CHAT_ID}" \
  -F "document=@/tmp/print-this-<timestamp>/document.pdf" \
  -F "caption=Printed: TITLE (SOURCE)" \
  --max-time 60
```

Check the curl response for `"ok":true`. If it fails, report the error in write_result.

**6. Subagent: Cleanup and report**

```bash
rm -rf /tmp/print-this-<timestamp>/
```

Call `write_result` with:
- `sent_reply_to_user=True` (the file was already sent directly to the user)
- A brief status: "Sent PDF of: TITLE"
- If it failed: `sent_reply_to_user=False` and explain what went wrong so the dispatcher can relay

### Error handling

- **Fetch failed / paywalled**: Reply "Couldn't fetch that URL — it may be paywalled or blocked. Try a different link."
- **LaTeX compile failed**: Try simplified template. If still failing, reply "Had trouble rendering that as PDF — could you send the text directly?"
- **PDF too large** (> 50MB): Compress or truncate content before retrying
- **Telegram send failed**: Report the exact error to the user

### Privacy

- The /tmp directory is cleaned up after each print job
- No content is stored permanently
- No content is logged or saved to disk beyond the working directory
