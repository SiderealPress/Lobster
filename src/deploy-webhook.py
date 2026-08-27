#!/usr/bin/env python3
"""
Lobster Deploy Webhook Server
Listens for GitHub push events and triggers Vercel deployments.

Usage:
    python3 deploy-webhook.py [--port 9120]

Configuration via environment variables (loaded from config files):
    DEPLOY_WEBHOOK_SECRET - GitHub webhook secret
    VERCEL_TOKEN - Vercel CLI token
"""

import hashlib
import hmac
import http.server
import json
import logging
import os
import subprocess
import threading
from pathlib import Path

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [deploy-webhook] %(levelname)s %(message)s",
)
log = logging.getLogger("deploy-webhook")

# --- Configuration ---
PORT = int(os.environ.get("DEPLOY_WEBHOOK_PORT", "9120"))
WEBHOOK_SECRET = os.environ.get("DEPLOY_WEBHOOK_SECRET", "")
VERCEL_TOKEN = os.environ.get("VERCEL_TOKEN", "")

# Map GitHub repo full_name → deploy config
DEPLOY_MAP = {
    "eloso-bisque/eloso-bisque": {
        "branch": "main",
        "project_dir": str(Path.home() / "lobster-workspace/projects/eloso-bisque"),
        "vercel_env": "production",
    },
}


def verify_signature(payload: bytes, sig_header: str, secret: str) -> bool:
    """Verify GitHub HMAC-SHA256 webhook signature.

    SECURITY NOTE: fails open when `secret` is empty — any caller, signed or
    not, is accepted. This mirrors the existing production behavior (a
    startup warning is logged, see `load_config`/`__main__` below) rather
    than changing it here, but it means DEPLOY_WEBHOOK_SECRET being unset
    silently disables auth on this endpoint. TODO: consider failing closed
    (rejecting all requests) instead once this is confirmed safe to change.
    """
    if not secret:
        log.warning("No webhook secret configured — skipping signature check")
        return True
    if not sig_header or not sig_header.startswith("sha256="):
        return False
    expected = "sha256=" + hmac.new(
        secret.encode(), payload, hashlib.sha256
    ).hexdigest()
    return hmac.compare_digest(expected, sig_header)


def deploy(project_dir: str, vercel_token: str) -> tuple[bool, str]:
    """Run vercel --prod in the project directory."""
    log.info("Deploying %s to Vercel production...", project_dir)
    env = os.environ.copy()
    env["VERCEL_TOKEN"] = vercel_token
    try:
        result = subprocess.run(
            ["vercel", "--prod", "--yes", "--token", vercel_token],
            cwd=project_dir,
            capture_output=True,
            text=True,
            timeout=300,
            env=env,
        )
        output = result.stdout + result.stderr
        if result.returncode == 0:
            log.info("Deploy succeeded: %s", output.strip()[-500:])
            return True, output.strip()
        else:
            log.error("Deploy failed (rc=%d): %s", result.returncode, output.strip())
            return False, output.strip()
    except subprocess.TimeoutExpired:
        log.error("Deploy timed out after 300s")
        return False, "Timed out"
    except Exception as exc:
        log.error("Deploy error: %s", exc)
        return False, str(exc)


class WebhookHandler(http.server.BaseHTTPRequestHandler):
    def log_message(self, fmt, *args):  # suppress default access log spam
        log.debug(fmt, *args)

    def do_GET(self):
        if self.path == "/health":
            self.send_response(200)
            self.end_headers()
            self.wfile.write(b"ok")
        else:
            self.send_response(404)
            self.end_headers()

    def do_POST(self):
        if self.path != "/deploy/eloso-bisque":
            self.send_response(404)
            self.end_headers()
            return

        length = int(self.headers.get("Content-Length", 0))
        body = self.rfile.read(length)

        sig = self.headers.get("X-Hub-Signature-256", "")
        if not verify_signature(body, sig, WEBHOOK_SECRET):
            log.warning("Invalid signature from %s", self.client_address)
            self.send_response(403)
            self.end_headers()
            self.wfile.write(b"Forbidden")
            return

        event = self.headers.get("X-GitHub-Event", "")
        if event == "ping":
            self.send_response(200)
            self.end_headers()
            self.wfile.write(b"pong")
            return

        if event != "push":
            self.send_response(200)
            self.end_headers()
            self.wfile.write(b"ignored")
            return

        try:
            payload = json.loads(body)
        except json.JSONDecodeError:
            self.send_response(400)
            self.end_headers()
            return

        ref = payload.get("ref", "")
        repo_name = payload.get("repository", {}).get("full_name", "")
        config = DEPLOY_MAP.get(repo_name)

        if not config:
            log.info("No deploy config for repo %s", repo_name)
            self.send_response(200)
            self.end_headers()
            self.wfile.write(b"no config")
            return

        expected_ref = f"refs/heads/{config['branch']}"
        if ref != expected_ref:
            log.info("Push to %s (not %s) — ignoring", ref, expected_ref)
            self.send_response(200)
            self.end_headers()
            self.wfile.write(b"ignored branch")
            return

        # Acknowledge immediately, deploy in background
        self.send_response(202)
        self.end_headers()
        self.wfile.write(b"Deploy triggered")

        def run_deploy():
            ok, output = deploy(config["project_dir"], VERCEL_TOKEN)
            if ok:
                log.info("Deploy complete for %s", repo_name)
            else:
                log.error("Deploy failed for %s: %s", repo_name, output[-200:])

        t = threading.Thread(target=run_deploy, daemon=True)
        t.start()


def load_config():
    """Load VERCEL_TOKEN and DEPLOY_WEBHOOK_SECRET from config.env if not already set."""
    global VERCEL_TOKEN, WEBHOOK_SECRET
    config_path = Path.home() / "lobster-config/config.env"
    if config_path.exists():
        for line in config_path.read_text().splitlines():
            line = line.strip()
            if line.startswith("#") or "=" not in line:
                continue
            k, _, v = line.partition("=")
            k = k.strip()
            v = v.strip().strip('"').strip("'")
            if k == "VERCEL_TOKEN" and not VERCEL_TOKEN:
                VERCEL_TOKEN = v
            if k == "DEPLOY_WEBHOOK_SECRET" and not WEBHOOK_SECRET:
                WEBHOOK_SECRET = v


if __name__ == "__main__":
    load_config()
    if not VERCEL_TOKEN:
        log.error("VERCEL_TOKEN not set — deploys will fail")
    if not WEBHOOK_SECRET:
        log.warning("DEPLOY_WEBHOOK_SECRET not set — webhook signature validation disabled")

    server = http.server.ThreadingHTTPServer(("127.0.0.1", PORT), WebhookHandler)
    log.info("Deploy webhook listening on 127.0.0.1:%d", PORT)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        log.info("Shutting down")
