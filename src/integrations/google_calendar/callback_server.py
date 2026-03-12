"""
Google Calendar OAuth callback server.

A minimal, single-use HTTP server that completes the OAuth 2.0 Authorization
Code flow after the user grants Lobster access to their Google Calendar.

Usage
-----
Start the server before generating the auth URL:

    python -m integrations.google_calendar.callback_server --user-id <user_id>

Then send the user to the auth URL (from ``oauth.generate_auth_url``).  After
the user clicks "Allow" on Google's consent screen, Google redirects to this
server.  The server:

    1. Reads the ``code`` and ``state`` query parameters from the redirect.
    2. Exchanges the code for access + refresh tokens via ``exchange_code_for_tokens``.
    3. Persists the tokens via ``save_token``.
    4. Returns a success or error HTML page to the browser.
    5. Shuts down cleanly (single-use).

Configuration
-------------
    GCAL_CALLBACK_HOST  — bind address (default: localhost)
    GCAL_CALLBACK_PORT  — listen port (default: 8080)

The redirect URI registered in Google Cloud Console **must** match the host and
port chosen here.  For single-user installs this is typically:

    http://localhost:8080/auth/google/callback

For the myownlobster.ai platform the redirect URI is:

    https://myownlobster.ai/auth/google/callback

Design principles
-----------------
- Pure HTML builders (no templates required).
- Side effects (token exchange, file I/O, HTTP server) isolated to the edges.
- No token values ever written to logs.
- Handles all error conditions with a user-friendly error page.
"""

from __future__ import annotations

import argparse
import logging
import os
import secrets
import sys
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from typing import Callable, NamedTuple, Optional
from urllib.parse import parse_qs, urlparse

from integrations.google_calendar.oauth import (
    OAuthError,
    exchange_code_for_tokens,
)
from integrations.google_calendar.token_store import save_token

log = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

_DEFAULT_HOST: str = os.environ.get("GCAL_CALLBACK_HOST", "localhost")
_DEFAULT_PORT: int = int(os.environ.get("GCAL_CALLBACK_PORT", "8080"))
_CALLBACK_PATH: str = "/auth/google/callback"


# ---------------------------------------------------------------------------
# Pure HTML response builders
# ---------------------------------------------------------------------------


def _success_html() -> str:
    """Return a success HTML page shown after a successful token exchange.

    This is a pure function — no I/O, no side effects.
    """
    return """<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="UTF-8">
  <meta name="viewport" content="width=device-width, initial-scale=1.0">
  <title>Google Calendar Connected</title>
  <style>
    body { font-family: system-ui, sans-serif; max-width: 480px; margin: 80px auto;
           padding: 0 24px; text-align: center; color: #1a1a1a; }
    .icon { font-size: 64px; margin-bottom: 16px; }
    h1 { font-size: 24px; font-weight: 600; margin-bottom: 8px; }
    p  { color: #555; line-height: 1.5; }
  </style>
</head>
<body>
  <div class="icon">&#x2705;</div>
  <h1>Google Calendar connected!</h1>
  <p>You can close this tab and return to your Lobster chat.<br>
     Your calendar is now linked.</p>
</body>
</html>"""


def _error_html(title: str, detail: str) -> str:
    """Return an error HTML page shown when something goes wrong.

    This is a pure function — no I/O, no side effects.

    Args:
        title:  Short human-readable error title (shown prominently).
        detail: Additional context for the user.
    """
    # Minimal HTML escaping for the user-facing strings
    safe_title = title.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
    safe_detail = detail.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
    return f"""<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="UTF-8">
  <meta name="viewport" content="width=device-width, initial-scale=1.0">
  <title>Connection Failed</title>
  <style>
    body {{ font-family: system-ui, sans-serif; max-width: 480px; margin: 80px auto;
            padding: 0 24px; text-align: center; color: #1a1a1a; }}
    .icon {{ font-size: 64px; margin-bottom: 16px; }}
    h1 {{ font-size: 24px; font-weight: 600; margin-bottom: 8px; }}
    p  {{ color: #555; line-height: 1.5; }}
    .detail {{ font-size: 13px; color: #888; margin-top: 16px; }}
  </style>
</head>
<body>
  <div class="icon">&#x274C;</div>
  <h1>{safe_title}</h1>
  <p>Something went wrong while connecting Google Calendar.<br>
     Please try again from Lobster.</p>
  <p class="detail">{safe_detail}</p>
</body>
</html>"""


# ---------------------------------------------------------------------------
# Query-parameter parsing (pure)
# ---------------------------------------------------------------------------


class CallbackParams(NamedTuple):
    """Parsed parameters from a Google OAuth callback request."""

    code: Optional[str]
    state: Optional[str]
    error: Optional[str]
    error_description: Optional[str]


def _parse_callback_params(query_string: str) -> CallbackParams:
    """Parse OAuth callback query parameters from a URL query string.

    This is a pure function — it reads only the query string it is given.

    Args:
        query_string: The raw query string from the redirect URL
                      (everything after the ``?``).

    Returns:
        CallbackParams with fields populated from the query string.
        Fields absent in the query are None.
    """
    params = parse_qs(query_string, keep_blank_values=False)

    def _first(key: str) -> Optional[str]:
        values = params.get(key)
        return values[0] if values else None

    return CallbackParams(
        code=_first("code"),
        state=_first("state"),
        error=_first("error"),
        error_description=_first("error_description"),
    )


# ---------------------------------------------------------------------------
# Callback outcome type
# ---------------------------------------------------------------------------


class _CallbackResult(NamedTuple):
    """Result of handling one OAuth callback request."""

    success: bool
    html: str
    log_message: str


# ---------------------------------------------------------------------------
# Core handler logic (pure-ish — depends on injected exchange/save fns)
# ---------------------------------------------------------------------------


def _handle_callback(
    params: CallbackParams,
    user_id: str,
    expected_state: Optional[str],
    exchange_fn: Callable,
    save_fn: Callable,
) -> _CallbackResult:
    """Process parsed OAuth callback parameters and return an HTML result.

    The ``exchange_fn`` and ``save_fn`` arguments are injected so this
    function can be tested without real network calls or filesystem I/O.

    Args:
        params:         Parsed query parameters from the redirect URL.
        user_id:        Lobster user ID to associate the token with.
        expected_state: CSRF state value that was embedded in the auth URL,
                        or None to skip state validation (not recommended for
                        production use; useful in testing).
        exchange_fn:    Callable matching ``exchange_code_for_tokens`` signature.
        save_fn:        Callable matching ``save_token`` signature.

    Returns:
        _CallbackResult with success flag, HTML body, and a log message.
    """
    # Google reported an error (user denied access, etc.)
    if params.error:
        description = params.error_description or "No additional detail."
        log.warning("OAuth callback received error: %s", params.error)
        return _CallbackResult(
            success=False,
            html=_error_html(
                "Authorization denied",
                f"Google returned: {params.error}",
            ),
            log_message=f"oauth error from google: {params.error}",
        )

    # Missing authorization code
    if not params.code:
        log.warning("OAuth callback missing 'code' parameter")
        return _CallbackResult(
            success=False,
            html=_error_html(
                "Missing authorization code",
                "The redirect from Google was missing the required 'code' parameter.",
            ),
            log_message="callback missing 'code' parameter",
        )

    # CSRF state validation
    if expected_state is not None and params.state != expected_state:
        log.warning(
            "OAuth callback state mismatch (expected != received) — possible CSRF"
        )
        return _CallbackResult(
            success=False,
            html=_error_html(
                "Security check failed",
                "The state parameter did not match. Please try connecting again.",
            ),
            log_message="state mismatch — possible CSRF attempt",
        )

    # Exchange the authorization code for tokens
    try:
        token = exchange_fn(params.code)
    except OAuthError as exc:
        log.error("Token exchange failed: %s", type(exc).__name__)
        return _CallbackResult(
            success=False,
            html=_error_html(
                "Authorization code expired or invalid",
                "The code from Google could not be exchanged for tokens. "
                "This often happens if the code was already used or has expired. "
                "Please try connecting again.",
            ),
            log_message=f"token exchange failed: {type(exc).__name__}",
        )
    except Exception as exc:
        log.exception("Unexpected error during token exchange")
        return _CallbackResult(
            success=False,
            html=_error_html(
                "Unexpected error",
                "An internal error occurred. Please try again.",
            ),
            log_message=f"unexpected exchange error: {type(exc).__name__}",
        )

    # Persist the token
    try:
        save_fn(user_id, token)
    except Exception as exc:
        log.exception("Failed to save token for user_id=%r", user_id)
        return _CallbackResult(
            success=False,
            html=_error_html(
                "Failed to save credentials",
                "Tokens were obtained but could not be written to disk. "
                "Check Lobster's file permissions and try again.",
            ),
            log_message=f"token save failed: {type(exc).__name__}",
        )

    log.info(
        "OAuth callback complete — token saved for user_id=%r, has_refresh=%s",
        user_id,
        token.refresh_token is not None,
    )
    return _CallbackResult(
        success=True,
        html=_success_html(),
        log_message="token exchange and save successful",
    )


# ---------------------------------------------------------------------------
# HTTP request handler
# ---------------------------------------------------------------------------


class _OAuthCallbackHandler(BaseHTTPRequestHandler):
    """Single-use HTTP request handler for the Google OAuth callback.

    Instances of this class are created per-request by HTTPServer.
    The ``user_id``, ``expected_state``, ``exchange_fn``, ``save_fn``,
    and ``shutdown_event`` attributes are injected via the server object
    (see ``_make_callback_server``).
    """

    def do_GET(self) -> None:
        """Handle a GET request to /auth/google/callback."""
        parsed = urlparse(self.path)

        if parsed.path != _CALLBACK_PATH:
            self._respond(404, _error_html("Not found", f"Path {parsed.path!r} is not handled."))
            return

        params = _parse_callback_params(parsed.query)
        result = _handle_callback(
            params=params,
            user_id=self.server.user_id,  # type: ignore[attr-defined]
            expected_state=self.server.expected_state,  # type: ignore[attr-defined]
            exchange_fn=self.server.exchange_fn,  # type: ignore[attr-defined]
            save_fn=self.server.save_fn,  # type: ignore[attr-defined]
        )

        status = 200 if result.success else 400
        self._respond(status, result.html)

        # Signal the server to shut down after responding
        self.server.shutdown_event.set()  # type: ignore[attr-defined]

    def _respond(self, status: int, html: str) -> None:
        """Write an HTTP response with the given status code and HTML body."""
        encoded = html.encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(encoded)))
        # Prevent caching of the callback page
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(encoded)

    def log_message(self, fmt: str, *args) -> None:
        """Route access log lines through the module logger."""
        log.debug("HTTP %s", fmt % args)


# ---------------------------------------------------------------------------
# Server factory (side-effecting, but injectable)
# ---------------------------------------------------------------------------


def _make_callback_server(
    host: str,
    port: int,
    user_id: str,
    expected_state: Optional[str],
    exchange_fn: Callable,
    save_fn: Callable,
) -> HTTPServer:
    """Create and configure an HTTPServer for the OAuth callback.

    Attaches the user context and injectable function references directly to
    the server object so ``_OAuthCallbackHandler`` instances can access them
    without global state.

    Args:
        host:           Bind address.
        port:           Port to listen on.
        user_id:        Lobster user ID to associate the token with.
        expected_state: Expected CSRF state value; None skips validation.
        exchange_fn:    Token exchange function (injectable for testing).
        save_fn:        Token save function (injectable for testing).

    Returns:
        A configured HTTPServer (not yet started).
    """
    server = HTTPServer((host, port), _OAuthCallbackHandler)
    server.user_id = user_id  # type: ignore[attr-defined]
    server.expected_state = expected_state  # type: ignore[attr-defined]
    server.exchange_fn = exchange_fn  # type: ignore[attr-defined]
    server.save_fn = save_fn  # type: ignore[attr-defined]
    server.shutdown_event = threading.Event()  # type: ignore[attr-defined]
    return server


# ---------------------------------------------------------------------------
# Public entry point
# ---------------------------------------------------------------------------


def run_callback_server(
    user_id: str,
    host: str = _DEFAULT_HOST,
    port: int = _DEFAULT_PORT,
    expected_state: Optional[str] = None,
    exchange_fn: Callable = exchange_code_for_tokens,
    save_fn: Callable = save_token,
) -> bool:
    """Start the OAuth callback server and block until one request is handled.

    This function starts a minimal HTTP server that waits for exactly one
    GET request to ``/auth/google/callback``, processes the token exchange,
    saves the token, then shuts down.

    Args:
        user_id:        Lobster user ID (e.g. Telegram ``chat_id`` as a string).
        host:           Address to bind the server to.  Defaults to
                        ``GCAL_CALLBACK_HOST`` env var, then ``localhost``.
        port:           Port to listen on.  Defaults to ``GCAL_CALLBACK_PORT``
                        env var, then ``8080``.
        expected_state: CSRF state token embedded in the auth URL.  If provided,
                        the callback handler validates that Google echoes it back
                        unchanged.  Pass ``None`` to skip validation (tests only).
        exchange_fn:    Callable for exchanging the auth code — injectable for
                        testing without network calls.
        save_fn:        Callable for persisting the token — injectable for
                        testing without filesystem I/O.

    Returns:
        True if the token was successfully obtained and saved, False otherwise.
    """
    server = _make_callback_server(
        host=host,
        port=port,
        user_id=user_id,
        expected_state=expected_state,
        exchange_fn=exchange_fn,
        save_fn=save_fn,
    )

    log.info(
        "OAuth callback server listening on http://%s:%d%s",
        host, port, _CALLBACK_PATH,
    )

    # Run the server in a thread so we can wait on the shutdown event
    # and then call server.shutdown() from the main thread.
    server_thread = threading.Thread(target=server.serve_forever, daemon=True)
    server_thread.start()

    # Block until the handler signals completion (or a KeyboardInterrupt)
    try:
        server.shutdown_event.wait()
    except KeyboardInterrupt:
        log.info("OAuth callback server interrupted by user.")
    finally:
        server.shutdown()
        server_thread.join(timeout=5)

    return server.shutdown_event.is_set()


# ---------------------------------------------------------------------------
# CLI entry point
# ---------------------------------------------------------------------------


def _build_arg_parser() -> argparse.ArgumentParser:
    """Build the argument parser for the callback server CLI.

    Pure function — no side effects.
    """
    parser = argparse.ArgumentParser(
        description=(
            "Lobster Google Calendar OAuth callback server.\n\n"
            "Start this server, then open the auth URL generated by Lobster "
            "in your browser. After you click 'Allow' on Google's consent screen "
            "this server will capture the token and shut down automatically."
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument(
        "--user-id",
        required=True,
        help="Lobster user ID to associate the token with (e.g. your Telegram chat_id).",
    )
    parser.add_argument(
        "--host",
        default=_DEFAULT_HOST,
        help=f"Address to bind the server to (default: {_DEFAULT_HOST}).",
    )
    parser.add_argument(
        "--port",
        type=int,
        default=_DEFAULT_PORT,
        help=f"Port to listen on (default: {_DEFAULT_PORT}).",
    )
    parser.add_argument(
        "--state",
        default=None,
        help=(
            "Expected CSRF state value. "
            "When provided, the callback will reject requests where Google "
            "does not echo back this exact value. "
            "Defaults to a freshly-generated random token."
        ),
    )
    parser.add_argument(
        "--log-level",
        default="INFO",
        choices=["DEBUG", "INFO", "WARNING", "ERROR"],
        help="Log verbosity (default: INFO).",
    )
    return parser


def main() -> None:
    """CLI entry point: parse arguments and run the callback server."""
    parser = _build_arg_parser()
    args = parser.parse_args()

    logging.basicConfig(
        level=getattr(logging, args.log_level),
        format="%(asctime)s [%(name)s] %(levelname)s: %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )

    # Generate a fresh CSRF state token if the caller did not supply one
    state = args.state if args.state is not None else secrets.token_urlsafe(32)
    if args.state is None:
        log.info("Generated CSRF state token for this session.")

    # Import here to provide a useful error if credentials are not configured
    from integrations.google_calendar.config import is_enabled
    from integrations.google_calendar.oauth import generate_auth_url

    if not is_enabled():
        log.error(
            "Google Calendar credentials not configured. "
            "Set GOOGLE_CLIENT_ID and GOOGLE_CLIENT_SECRET in config.env."
        )
        sys.exit(1)

    auth_url = generate_auth_url(state=state)

    print()
    print("=" * 72)
    print("  Lobster Google Calendar OAuth")
    print("=" * 72)
    print()
    print(f"  Callback server: http://{args.host}:{args.port}{_CALLBACK_PATH}")
    print()
    print("  Open the following URL in your browser to connect Google Calendar:")
    print()
    print(f"  {auth_url}")
    print()
    print("  Waiting for Google to redirect back... (Ctrl-C to cancel)")
    print()

    success = run_callback_server(
        user_id=args.user_id,
        host=args.host,
        port=args.port,
        expected_state=state,
    )

    if success:
        print()
        print("  Google Calendar connected successfully!")
        print(f"  Token saved for user_id={args.user_id!r}.")
        print()
        sys.exit(0)
    else:
        print()
        print("  Connection failed. Check the log output above for details.")
        print()
        sys.exit(1)


if __name__ == "__main__":
    main()
