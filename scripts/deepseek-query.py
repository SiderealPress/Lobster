#!/usr/bin/env python3
"""
DeepSeek query script for Lobster.
Usage: uv run ~/lobster/scripts/deepseek-query.py "your question here"

Reads DEEPSEEK_API_KEY from ~/lobster-config/deepseek.env and calls the
DeepSeek API (OpenAI-compatible) with the provided query.
"""

import sys
import os
from pathlib import Path


def load_api_key() -> str:
    env_file = Path.home() / "lobster-config" / "deepseek.env"
    if not env_file.exists():
        print(f"Error: {env_file} not found", file=sys.stderr)
        sys.exit(1)

    for line in env_file.read_text().splitlines():
        line = line.strip()
        if line.startswith("#") or not line:
            continue
        if line.startswith("DEEPSEEK_API_KEY="):
            return line.split("=", 1)[1].strip()

    print("Error: DEEPSEEK_API_KEY not found in deepseek.env", file=sys.stderr)
    sys.exit(1)


def main() -> None:
    if len(sys.argv) < 2:
        print("Usage: deepseek-query.py <query>", file=sys.stderr)
        sys.exit(1)

    query = sys.argv[1]

    try:
        from openai import OpenAI
    except ImportError:
        print("Error: openai package not installed. Run: uv pip install openai", file=sys.stderr)
        sys.exit(1)

    api_key = load_api_key()

    try:
        client = OpenAI(api_key=api_key, base_url="https://api.deepseek.com/v1")
        response = client.chat.completions.create(
            model="deepseek-chat",
            messages=[{"role": "user", "content": query}],
        )
        print(response.choices[0].message.content)
    except Exception as e:
        print(f"Error calling DeepSeek API: {e}", file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    main()
