# -*- coding: utf-8 -*-
"""
Kiro Gateway GUI Launcher.

Wraps the FastAPI web UI in a native desktop window using pywebview.
Works on macOS and Windows.

Usage:
    python gui.py
    python gui.py --port 8000
"""

import argparse
import os
import sys
import socket
import threading
import time
from pathlib import Path

# PyInstaller sets sys._MEIPASS to the temp extraction directory
# In dev mode, use the script's parent directory
if getattr(sys, 'frozen', False):
    BUNDLE_DIR = Path(sys._MEIPASS)
else:
    BUNDLE_DIR = Path(__file__).parent

PROJECT_ROOT = BUNDLE_DIR
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))


CLAUDE_SETTINGS = Path.home() / ".claude" / "settings.json"
CLAUDE_STATE = Path.home() / ".claude.json"
CLAUDE_BACKUP = Path.home() / ".claude" / ".env_backup.json"


def inject_claude_settings(port: int):
    """Inject ANTHROPIC_API_KEY/BASE_URL into Claude CLI settings."""
    if not CLAUDE_SETTINGS.exists():
        return
    try:
        import json
        with open(CLAUDE_SETTINGS) as f:
            s = json.load(f)
        env = s.setdefault("env", {})
        # Backup to file
        backup = {
            "ANTHROPIC_API_KEY": env.get("ANTHROPIC_API_KEY"),
            "ANTHROPIC_BASE_URL": env.get("ANTHROPIC_BASE_URL"),
        }
        with open(CLAUDE_BACKUP, "w") as f:
            json.dump(backup, f)
        api_key = os.environ.get("PROXY_API_KEY", "kiro-gateway-local")
        env["ANTHROPIC_API_KEY"] = api_key
        env["ANTHROPIC_BASE_URL"] = f"http://127.0.0.1:{port}"
        with open(CLAUDE_SETTINGS, "w") as f:
            json.dump(s, f, indent=2, ensure_ascii=False)
        # Approve key
        if CLAUDE_STATE.exists():
            with open(CLAUDE_STATE) as f:
                d = json.load(f)
            approved = d.setdefault("customApiKeyResponses", {}).setdefault("approved", [])
            if api_key not in approved:
                approved.append(api_key)
                with open(CLAUDE_STATE, "w") as f:
                    json.dump(d, f, indent=2, ensure_ascii=False)
        print(f"Claude CLI settings -> kiro-gateway (port {port})")
    except Exception as e:
        print(f"WARNING: Failed to inject Claude settings: {e}")


def restore_claude_settings():
    """Restore original Claude CLI settings from backup file."""
    if not CLAUDE_SETTINGS.exists():
        return
    try:
        import json
        with open(CLAUDE_SETTINGS) as f:
            s = json.load(f)
        env = s.setdefault("env", {})
        if CLAUDE_BACKUP.exists():
            with open(CLAUDE_BACKUP) as f:
                backup = json.load(f)
            for key, val in backup.items():
                if val is None:
                    env.pop(key, None)
                else:
                    env[key] = val
            CLAUDE_BACKUP.unlink()
        else:
            env.pop("ANTHROPIC_API_KEY", None)
            env.pop("ANTHROPIC_BASE_URL", None)
        with open(CLAUDE_SETTINGS, "w") as f:
            json.dump(s, f, indent=2, ensure_ascii=False)
        print("Claude CLI settings -> restored")
    except Exception as e:
        print(f"WARNING: Failed to restore Claude settings: {e}")


def auto_detect_credentials():
    """
    Auto-detect Kiro credentials on this machine and inject as env vars.
    This allows the app to work without a .env file.
    """
    # Set default API key if not configured
    if not os.environ.get("PROXY_API_KEY"):
        os.environ["PROXY_API_KEY"] = "kiro-gateway-local"

    # Already configured? Skip detection
    if os.environ.get("REFRESH_TOKEN") or os.environ.get("KIRO_CREDS_FILE") or os.environ.get("KIRO_CLI_DB_FILE"):
        return

    # Priority 1: kiro-cli SQLite database
    sqlite_paths = [
        Path.home() / ".local" / "share" / "kiro-cli" / "data.sqlite3",
        Path.home() / "Library" / "Application Support" / "kiro-cli" / "data.sqlite3",
        Path(os.environ.get("APPDATA", "")) / "kiro-cli" / "data.sqlite3",
    ]
    for p in sqlite_paths:
        if p.exists():
            os.environ["KIRO_CLI_DB_FILE"] = str(p)
            print(f"Auto-detected kiro-cli credentials: {p}")
            return

    # Priority 2: Kiro IDE JSON token
    json_path = Path.home() / ".aws" / "sso" / "cache" / "kiro-auth-token.json"
    if json_path.exists():
        os.environ["KIRO_CREDS_FILE"] = str(json_path)
        print(f"Auto-detected Kiro IDE credentials: {json_path}")
        return

    print("WARNING: No Kiro credentials found. Please login to Kiro IDE first.")


def wait_for_server(host: str, port: int, timeout: float = 15.0) -> bool:
    """Wait until the server is accepting connections."""
    start = time.time()
    while time.time() - start < timeout:
        try:
            with socket.create_connection((host, port), timeout=1):
                return True
        except OSError:
            time.sleep(0.2)
    return False


def start_server(host: str, port: int):
    """Start the FastAPI server in a background thread."""
    import uvicorn
    # Import after sys.path is set
    from main import app, validate_configuration, UVICORN_LOG_CONFIG

    validate_configuration()

    uvicorn.run(
        app,
        host=host,
        port=port,
        log_config=UVICORN_LOG_CONFIG,
        log_level="info",
    )


def main():
    parser = argparse.ArgumentParser(description="Kiro Gateway GUI")
    parser.add_argument("-p", "--port", type=int, default=None, help="Server port")
    parser.add_argument("--no-gui", action="store_true", help="Start server only, no GUI window")
    args = parser.parse_args()

    host = "127.0.0.1"
    port = args.port or 8000

    # Prevent multiple instances
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
            s.bind((host, port))
    except OSError:
        print(f"Port {port} already in use. Kiro Gateway is already running.")
        sys.exit(0)

    # Change to project directory so .env and relative paths work
    os.chdir(PROJECT_ROOT)

    # Auto-detect credentials before server starts
    auto_detect_credentials()

    # Inject Claude CLI settings to point to this gateway
    inject_claude_settings(port)

    # Start server in background thread
    server_thread = threading.Thread(target=start_server, args=(host, port), daemon=True)
    server_thread.start()

    url = f"http://{host}:{port}/ui"

    if args.no_gui:
        print(f"Kiro Gateway running at {url}")
        print("Press Ctrl+C to stop")
        try:
            server_thread.join()
        except KeyboardInterrupt:
            print("\nStopping...")
            restore_claude_settings()
            sys.exit(0)
        return

    # Wait for server to be ready
    print(f"Starting Kiro Gateway on port {port}...")
    if not wait_for_server(host, port):
        print("Server failed to start within timeout")
        sys.exit(1)

    # Launch native window
    try:
        import webview
    except ImportError:
        print("pywebview not installed. Install it with: pip install pywebview")
        print(f"Falling back to browser: {url}")
        import webbrowser
        webbrowser.open(url)
        try:
            server_thread.join()
        except KeyboardInterrupt:
            sys.exit(0)
        return

    window = webview.create_window(
        title="Kiro Gateway",
        url=url,
        width=960,
        height=680,
        min_size=(720, 500),
        background_color="#0a0e14",
    )

    # On window close, restore Claude settings and exit
    def on_closed():
        restore_claude_settings()
        os._exit(0)

    window.events.closed += on_closed

    webview.start(
        gui=None,  # Auto-detect: Cocoa on macOS, EdgeChromium/MSHTML on Windows
        debug=False,
    )


if __name__ == "__main__":
    main()
