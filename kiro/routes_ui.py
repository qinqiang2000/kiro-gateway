# -*- coding: utf-8 -*-
"""
Web UI API routes for Kiro Gateway management dashboard.

Provides endpoints for:
- Dashboard page serving
- Gateway status monitoring
- Credential detection and validation
- Log streaming
- Gateway process control (start/stop)
"""

import asyncio
import json
import os
import platform
import sqlite3
import subprocess
import signal
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse, JSONResponse, StreamingResponse
from loguru import logger

router = APIRouter(tags=["UI"])

# Log file path
LOG_FILE = "/tmp/kiro-gateway.log"
PID_FILE = "/tmp/kiro-gateway.pid"

# Credential search paths
CRED_PATHS = {
    "kiro_cli_sqlite": [
        "~/.local/share/kiro-cli/data.sqlite3",
        "~/Library/Application Support/kiro-cli/data.sqlite3",
    ],
    "kiro_ide_json": [
        "~/.aws/sso/cache/kiro-auth-token.json",
    ],
}


def _detect_credentials() -> list[dict]:
    """Scan local filesystem for Kiro credentials."""
    found = []

    # Check kiro-cli SQLite
    sqlite_paths = list(CRED_PATHS["kiro_cli_sqlite"])
    appdata = os.environ.get("APPDATA", "")
    if appdata:
        sqlite_paths.append(str(Path(appdata) / "kiro-cli" / "data.sqlite3"))
    for p in sqlite_paths:
        path = Path(p).expanduser()
        if path.exists():
            try:
                conn = sqlite3.connect(str(path))
                cursor = conn.cursor()
                keys = [
                    "kirocli:social:token",
                    "kirocli:odic:token",
                    "codewhisperer:odic:token",
                ]
                for key in keys:
                    cursor.execute("SELECT value FROM auth_kv WHERE key = ?", (key,))
                    row = cursor.fetchone()
                    if row:
                        data = json.loads(row[0])
                        has_access = bool(data.get("access_token"))
                        has_refresh = bool(data.get("refresh_token"))
                        expires_at = data.get("expires_at", "")
                        found.append({
                            "source": "kiro-cli",
                            "type": "sqlite",
                            "path": str(path),
                            "key": key,
                            "has_access_token": has_access,
                            "has_refresh_token": has_refresh,
                            "expires_at": expires_at,
                        })
                        break
                conn.close()
            except Exception as e:
                logger.debug(f"Error reading SQLite {path}: {e}")

    # Check Kiro IDE JSON
    for p in CRED_PATHS["kiro_ide_json"]:
        path = Path(p).expanduser()
        if path.exists():
            try:
                with open(path, "r") as f:
                    data = json.load(f)
                has_access = bool(data.get("accessToken"))
                has_refresh = bool(data.get("refreshToken"))
                expires_at = data.get("expiresAt", "")
                auth_type = "aws_sso_oidc" if data.get("clientId") else "kiro_desktop"
                found.append({
                    "source": "kiro-ide",
                    "type": "json",
                    "path": str(path),
                    "auth_type": auth_type,
                    "has_access_token": has_access,
                    "has_refresh_token": has_refresh,
                    "expires_at": expires_at,
                })
            except Exception as e:
                logger.debug(f"Error reading JSON {path}: {e}")

    # Check .env refresh token
    env_path = Path(__file__).parent.parent / ".env"
    if env_path.exists():
        try:
            content = env_path.read_text()
            for line in content.splitlines():
                if line.strip().startswith("REFRESH_TOKEN=") and not line.strip().startswith("#"):
                    val = line.split("=", 1)[1].strip().strip('"').strip("'")
                    if val:
                        found.append({
                            "source": "env-file",
                            "type": "env",
                            "path": str(env_path),
                            "has_access_token": False,
                            "has_refresh_token": True,
                            "expires_at": "",
                        })
                    break
        except Exception:
            pass

    return found


def _get_gateway_pid() -> Optional[int]:
    """Get the running gateway PID if available."""
    try:
        if Path(PID_FILE).exists():
            pid = int(Path(PID_FILE).read_text().strip())
            # Check if process is actually running
            os.kill(pid, 0)
            return pid
    except (ProcessLookupError, ValueError, PermissionError):
        pass
    return None


@router.get("/api/ui/status")
async def get_status(request: Request):
    """Get gateway status including auth and model info."""
    status = {
        "running": True,  # If this endpoint responds, gateway is running
        "pid": os.getpid(),
        "platform": platform.system(),
        "python": platform.python_version(),
        "uptime": None,
    }

    # Auth status
    try:
        auth = request.app.state.auth_manager
        status["auth"] = {
            "type": auth.auth_type.value,
            "region": auth.region,
            "token_valid": not auth.is_token_expired(),
            "token_expiring_soon": auth.is_token_expiring_soon(),
            "expires_at": auth._expires_at.isoformat() if auth._expires_at else None,
        }
    except Exception as e:
        status["auth"] = {"error": str(e)}

    # Model info
    try:
        cache = request.app.state.model_cache
        models = cache.get_all_model_ids()
        status["models"] = {
            "count": len(models),
            "list": sorted(models),
        }
    except Exception:
        status["models"] = {"count": 0, "list": []}

    return JSONResponse(status)


CLAUDE_SETTINGS = Path.home() / ".claude" / "settings.json"
CLAUDE_STATE = Path.home() / ".claude.json"
CLAUDE_BACKUP = Path.home() / ".claude" / ".env_backup.json"


def _read_json(path: Path) -> dict:
    with open(path) as f:
        return json.load(f)


def _write_json(path: Path, data: dict):
    with open(path, "w") as f:
        json.dump(data, f, indent=2, ensure_ascii=False)


def _inject_claude(port: int):
    """Inject gateway config into Claude CLI settings, backup originals to file."""
    if not CLAUDE_SETTINGS.exists():
        return
    s = _read_json(CLAUDE_SETTINGS)
    env = s.setdefault("env", {})
    # Save backup to file (survives process restart)
    backup = {
        "ANTHROPIC_API_KEY": env.get("ANTHROPIC_API_KEY"),
        "ANTHROPIC_BASE_URL": env.get("ANTHROPIC_BASE_URL"),
    }
    _write_json(CLAUDE_BACKUP, backup)
    api_key = os.environ.get("PROXY_API_KEY", "kiro-gateway-local")
    env["ANTHROPIC_API_KEY"] = api_key
    env["ANTHROPIC_BASE_URL"] = f"http://127.0.0.1:{port}"
    _write_json(CLAUDE_SETTINGS, s)
    # Approve key
    if CLAUDE_STATE.exists():
        try:
            d = _read_json(CLAUDE_STATE)
            approved = d.setdefault("customApiKeyResponses", {}).setdefault("approved", [])
            if api_key not in approved:
                approved.append(api_key)
                _write_json(CLAUDE_STATE, d)
        except Exception:
            pass


def _restore_claude():
    """Restore original Claude CLI settings from backup file."""
    if not CLAUDE_SETTINGS.exists():
        return
    s = _read_json(CLAUDE_SETTINGS)
    env = s.setdefault("env", {})
    # Read backup
    if CLAUDE_BACKUP.exists():
        backup = _read_json(CLAUDE_BACKUP)
        for key, val in backup.items():
            if val is None:
                env.pop(key, None)
            else:
                env[key] = val
        CLAUDE_BACKUP.unlink()
    else:
        env.pop("ANTHROPIC_API_KEY", None)
        env.pop("ANTHROPIC_BASE_URL", None)
    _write_json(CLAUDE_SETTINGS, s)


@router.get("/api/ui/proxy/status")
async def proxy_status():
    """Get proxy injection status by checking Claude settings file."""
    if not CLAUDE_SETTINGS.exists():
        return JSONResponse({"enabled": False})
    try:
        s = _read_json(CLAUDE_SETTINGS)
        base_url = s.get("env", {}).get("ANTHROPIC_BASE_URL", "")
        enabled = "127.0.0.1" in base_url or "localhost" in base_url
        return JSONResponse({"enabled": enabled})
    except Exception:
        return JSONResponse({"enabled": False})


@router.post("/api/ui/proxy/enable")
async def proxy_enable(request: Request):
    """Enable proxy: inject Claude CLI settings."""
    try:
        # Detect port from current request
        port = request.url.port or 8000
        _inject_claude(port)
        return JSONResponse({"success": True, "enabled": True})
    except Exception as e:
        return JSONResponse({"success": False, "error": str(e)}, status_code=500)


@router.post("/api/ui/proxy/disable")
async def proxy_disable():
    """Disable proxy: restore Claude CLI settings."""
    try:
        _restore_claude()
        return JSONResponse({"success": True, "enabled": False})
    except Exception as e:
        return JSONResponse({"success": False, "error": str(e)}, status_code=500)


@router.get("/api/ui/proxy/check")
async def proxy_check():
    """Check if Claude CLI is configured to use this gateway."""
    if not CLAUDE_SETTINGS.exists():
        return JSONResponse({"ok": False, "error": "Claude CLI 未安装（~/.claude/settings.json 不存在）"})
    try:
        s = _read_json(CLAUDE_SETTINGS)
        env = s.get("env", {})
        base_url = env.get("ANTHROPIC_BASE_URL", "")
        is_local = "127.0.0.1" in base_url or "localhost" in base_url
        return JSONResponse({
            "ok": is_local,
            "base_url": base_url or "未配置",
            "api_key": "已配置" if env.get("ANTHROPIC_API_KEY") else "未配置",
        })
    except Exception as e:
        return JSONResponse({"ok": False, "error": str(e)})


@router.get("/api/ui/credentials")
async def get_credentials():
    """Detect available Kiro credentials on this machine."""
    creds = _detect_credentials()
    return JSONResponse({"credentials": creds, "count": len(creds)})


@router.get("/api/ui/logs")
async def stream_logs(lines: int = 100):
    """Get recent log lines."""
    log_path = Path(LOG_FILE)
    if not log_path.exists():
        return JSONResponse({"logs": [], "file": LOG_FILE, "exists": False})

    try:
        content = log_path.read_text(errors="replace")
        all_lines = content.splitlines()
        recent = all_lines[-lines:] if len(all_lines) > lines else all_lines
        return JSONResponse({"logs": recent, "file": LOG_FILE, "exists": True, "total_lines": len(all_lines)})
    except Exception as e:
        return JSONResponse({"logs": [], "error": str(e)}, status_code=500)


@router.get("/api/ui/logs/stream")
async def stream_logs_sse():
    """Stream logs via SSE."""
    log_path = Path(LOG_FILE)

    async def event_generator():
        if not log_path.exists():
            yield f"data: {json.dumps({'type': 'error', 'message': 'Log file not found'})}\n\n"
            return

        # Send last 50 lines first
        try:
            content = log_path.read_text(errors="replace")
            for line in content.splitlines()[-50:]:
                yield f"data: {json.dumps({'type': 'log', 'line': line})}\n\n"
        except Exception:
            pass

        # Then tail the file
        last_size = log_path.stat().st_size if log_path.exists() else 0
        while True:
            await asyncio.sleep(1)
            try:
                if not log_path.exists():
                    continue
                current_size = log_path.stat().st_size
                if current_size > last_size:
                    with open(log_path, "r", errors="replace") as f:
                        f.seek(last_size)
                        new_content = f.read()
                        for line in new_content.splitlines():
                            if line.strip():
                                yield f"data: {json.dumps({'type': 'log', 'line': line})}\n\n"
                    last_size = current_size
                elif current_size < last_size:
                    last_size = 0  # File was truncated/rotated
            except Exception:
                await asyncio.sleep(2)

    return StreamingResponse(event_generator(), media_type="text/event-stream")


@router.post("/api/ui/token/refresh")
async def force_refresh_token(request: Request):
    """Force refresh the auth token."""
    try:
        auth = request.app.state.auth_manager
        token = await auth.force_refresh()
        return JSONResponse({
            "success": True,
            "expires_at": auth._expires_at.isoformat() if auth._expires_at else None,
        })
    except Exception as e:
        return JSONResponse({"success": False, "error": str(e)}, status_code=500)
