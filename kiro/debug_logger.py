# -*- coding: utf-8 -*-

# Kiro Gateway
# https://github.com/jwadow/kiro-gateway
# Copyright (C) 2025 Jwadow
#
# This program is free software: you can redistribute it and/or modify
# it under the terms of the GNU Affero General Public License as published by
# the Free Software Foundation, either version 3 of the License, or
# (at your option) any later version.
#
# This program is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE. See the
# GNU Affero General Public License for more details.
#
# You should have received a copy of the GNU Affero General Public License
# along with this program. If not, see <https://www.gnu.org/licenses/>.

"""
Debug logging module for requests.

Supports three modes (DEBUG_MODE):
- off: logging disabled
- errors: logs are saved only on errors (4xx, 5xx)
- all: logs are overwritten on each request

In "errors" mode, data is buffered in memory and flushed to files
only when flush_on_error() is called.

Also captures application logs (loguru) for each request and saves
them to app_logs.txt file for debugging convenience.
"""

import contextvars
import io
import json
import shutil
import time
import uuid
from pathlib import Path
from typing import Optional
from loguru import logger

from kiro.config import DEBUG_MODE, DEBUG_DIR


# Per-request debug directory, scoped via ContextVar so concurrent requests
# don't overwrite each other's debug artifacts. Each request gets its own
# subdirectory under DEBUG_DIR (e.g. debug_logs/req_HHMMSS_xxxxxx/).
_request_dir_var: contextvars.ContextVar[Optional[Path]] = contextvars.ContextVar(
    "debug_request_dir", default=None
)

# Keep at most this many per-request directories under DEBUG_DIR.
# Older directories are pruned by mtime when a new request is prepared.
MAX_REQUEST_DIRS = 50


class DebugLogger:
    """
    Singleton for managing debug request logs.
    
    Operating modes:
    - off: does nothing
    - errors: buffers data, flushes to files only on errors
    - all: writes data immediately to files (as before)
    """
    _instance = None

    def __new__(cls):
        if cls._instance is None:
            cls._instance = super(DebugLogger, cls).__new__(cls)
            cls._instance._initialized = False
        return cls._instance

    def __init__(self):
        if self._initialized:
            return
        self.debug_dir = Path(DEBUG_DIR)
        self._initialized = True
        
        # Buffers for "errors" mode
        self._request_body_buffer: Optional[bytes] = None
        self._kiro_request_body_buffer: Optional[bytes] = None
        self._raw_chunks_buffer: bytearray = bytearray()
        self._modified_chunks_buffer: bytearray = bytearray()
        
        # Buffer for application logs (loguru)
        self._app_logs_buffer: io.StringIO = io.StringIO()
        self._loguru_sink_id: Optional[int] = None
    
    def _is_enabled(self) -> bool:
        """Checks if logging is enabled."""
        return DEBUG_MODE in ("errors", "all")

    def _is_immediate_write(self) -> bool:
        """Checks if immediate file writing is needed (all mode)."""
        return DEBUG_MODE == "all"

    def _current_dir(self) -> Path:
        """
        Returns the per-request debug directory for the current request.

        Falls back to ``self.debug_dir`` when no request has been prepared
        (e.g. background tasks that aren't tied to an HTTP request).
        """
        d = _request_dir_var.get()
        return d if d is not None else self.debug_dir

    def _prune_old_request_dirs(self) -> None:
        """Keep only the most recent MAX_REQUEST_DIRS request subdirectories."""
        try:
            if not self.debug_dir.exists():
                return
            req_dirs = [
                p for p in self.debug_dir.iterdir()
                if p.is_dir() and p.name.startswith("req_")
            ]
            if len(req_dirs) <= MAX_REQUEST_DIRS:
                return
            req_dirs.sort(key=lambda p: p.stat().st_mtime, reverse=True)
            for stale in req_dirs[MAX_REQUEST_DIRS:]:
                try:
                    shutil.rmtree(stale)
                except Exception:
                    pass
        except Exception as e:
            logger.debug(f"[DebugLogger] Prune skipped: {e}")
    
    def _clear_buffers(self):
        """Clears all buffers."""
        self._request_body_buffer = None
        self._kiro_request_body_buffer = None
        self._raw_chunks_buffer.clear()
        self._modified_chunks_buffer.clear()
        self._clear_app_logs_buffer()
    
    def _clear_app_logs_buffer(self):
        """Clears the application logs buffer and removes sink."""
        # Remove sink from loguru
        if self._loguru_sink_id is not None:
            try:
                logger.remove(self._loguru_sink_id)
            except ValueError:
                # Sink already removed
                pass
            self._loguru_sink_id = None
        
        # Clear buffer
        self._app_logs_buffer = io.StringIO()
    
    def _setup_app_logs_capture(self):
        """
        Sets up application log capture to buffer.
        
        Adds a temporary sink to loguru that writes to StringIO buffer.
        Captures ALL logs without filtering, as sink is active only
        during processing of a specific request.
        """
        # Remove previous sink if exists
        self._clear_app_logs_buffer()
        
        # Add new sink to capture ALL logs
        # Format: time | level | module:function:line | message
        self._loguru_sink_id = logger.add(
            self._app_logs_buffer,
            format="{time:YYYY-MM-DD HH:mm:ss.SSS} | {level: <8} | {name}:{function}:{line} | {message}",
            level="DEBUG",  # Capture all levels from DEBUG and above
            colorize=False,  # No ANSI colors in file
            # No filter - capture ALL logs during request processing
        )

    def prepare_new_request(self):
        """
        Prepares the logger for a new request.

        Creates a per-request subdirectory under DEBUG_DIR and binds it to the
        current request's ContextVar so concurrent requests don't overwrite
        each other's artifacts.
        """
        if not self._is_enabled():
            return

        # Clear buffers in any case
        self._clear_buffers()

        # Set up application log capture
        self._setup_app_logs_capture()

        try:
            self.debug_dir.mkdir(parents=True, exist_ok=True)
            req_id = f"req_{time.strftime('%H%M%S')}_{uuid.uuid4().hex[:6]}"
            req_dir = self.debug_dir / req_id
            req_dir.mkdir(parents=True, exist_ok=True)
            _request_dir_var.set(req_dir)
            self._prune_old_request_dirs()
            logger.debug(f"[DebugLogger] New request dir: {req_dir}")
        except Exception as e:
            logger.error(f"[DebugLogger] Error preparing request dir: {e}")

    def log_request_body(self, body: bytes):
        """
        Saves the request body (from client, OpenAI format).
        
        In "all" mode: writes immediately to file.
        In "errors" mode: buffers.
        """
        if not self._is_enabled():
            return

        if self._is_immediate_write():
            self._write_request_body_to_file(body)
        else:
            # "errors" mode - buffer
            self._request_body_buffer = body

    def log_kiro_request_body(self, body: bytes):
        """
        Saves the modified request body (to Kiro API).
        
        In "all" mode: writes immediately to file.
        In "errors" mode: buffers.
        """
        if not self._is_enabled():
            return

        if self._is_immediate_write():
            self._write_kiro_request_body_to_file(body)
        else:
            # "errors" mode - buffer
            self._kiro_request_body_buffer = body

    def log_raw_chunk(self, chunk: bytes):
        """
        Appends raw response chunk (from provider).
        
        In "all" mode: writes immediately to file.
        In "errors" mode: buffers.
        """
        if not self._is_enabled():
            return

        if self._is_immediate_write():
            self._append_raw_chunk_to_file(chunk)
        else:
            # "errors" mode - buffer
            self._raw_chunks_buffer.extend(chunk)

    def log_modified_chunk(self, chunk: bytes):
        """
        Appends modified chunk (to client).
        
        In "all" mode: writes immediately to file.
        In "errors" mode: buffers.
        """
        if not self._is_enabled():
            return

        if self._is_immediate_write():
            self._append_modified_chunk_to_file(chunk)
        else:
            # "errors" mode - buffer
            self._modified_chunks_buffer.extend(chunk)
    
    def log_error_info(self, status_code: int, error_message: str = ""):
        """
        Writes error information to file.
        
        Works in both modes (errors and all).
        In "all" mode writes immediately to file.
        In "errors" mode called from flush_on_error().
        
        Args:
            status_code: HTTP error status code
            error_message: Error message (optional)
        """
        if not self._is_enabled():
            return
        
        try:
            # Ensure directory exists
            req_dir = self._current_dir()
            req_dir.mkdir(parents=True, exist_ok=True)

            error_info = {
                "status_code": status_code,
                "error_message": error_message
            }
            error_file = req_dir / "error_info.json"
            with open(error_file, "w", encoding="utf-8") as f:
                json.dump(error_info, f, indent=2, ensure_ascii=False)

            logger.debug(f"[DebugLogger] Error info saved (status={status_code})")
        except Exception as e:
            logger.error(f"[DebugLogger] Error writing error_info: {e}")

    def flush_on_error(self, status_code: int, error_message: str = ""):
        """
        Flushes buffers to files on error.
        
        In "errors" mode: flushes buffers and saves error_info.
        In "all" mode: only saves error_info (data already written).
        
        Args:
            status_code: HTTP error status code
            error_message: Error message (optional)
        """
        if not self._is_enabled():
            return
        
        # In "all" mode data is already written, add error_info and app logs
        if self._is_immediate_write():
            self.log_error_info(status_code, error_message)
            self._write_app_logs_to_file()
            self._clear_app_logs_buffer()
            return
        
        # Check if there's anything to flush
        if not any([
            self._request_body_buffer,
            self._kiro_request_body_buffer,
            self._raw_chunks_buffer,
            self._modified_chunks_buffer
        ]):
            return
        
        try:
            # Ensure per-request directory exists
            req_dir = self._current_dir()
            req_dir.mkdir(parents=True, exist_ok=True)

            # Flush buffers to files
            if self._request_body_buffer:
                self._write_request_body_to_file(self._request_body_buffer)

            if self._kiro_request_body_buffer:
                self._write_kiro_request_body_to_file(self._kiro_request_body_buffer)

            if self._raw_chunks_buffer:
                file_path = req_dir / "response_stream_raw.txt"
                with open(file_path, "wb") as f:
                    f.write(self._raw_chunks_buffer)

            if self._modified_chunks_buffer:
                file_path = req_dir / "response_stream_modified.txt"
                with open(file_path, "wb") as f:
                    f.write(self._modified_chunks_buffer)

            # Save error information
            self.log_error_info(status_code, error_message)

            # Save application logs
            self._write_app_logs_to_file()

            logger.info(f"[DebugLogger] Error logs flushed to {req_dir} (status={status_code})")
            
        except Exception as e:
            logger.error(f"[DebugLogger] Error flushing buffers: {e}")
        finally:
            # Clear buffers after flush
            self._clear_buffers()
    
    def discard_buffers(self):
        """
        Clears buffers without writing to files.
        
        Called when request completed successfully in "errors" mode.
        Also called in "all" mode to save logs of successful request.
        """
        if DEBUG_MODE == "errors":
            self._clear_buffers()
        elif DEBUG_MODE == "all":
            # In "all" mode save logs even for successful requests
            self._write_app_logs_to_file()
            self._clear_app_logs_buffer()
    
    # ==================== Private file writing methods ====================
    
    def _write_request_body_to_file(self, body: bytes):
        """Writes request body to file."""
        try:
            file_path = self._current_dir() / "request_body.json"
            try:
                json_obj = json.loads(body)
                with open(file_path, "w", encoding="utf-8") as f:
                    json.dump(json_obj, f, indent=2, ensure_ascii=False)
            except json.JSONDecodeError:
                with open(file_path, "wb") as f:
                    f.write(body)
        except Exception as e:
            logger.error(f"[DebugLogger] Error writing request_body: {e}")

    def _write_kiro_request_body_to_file(self, body: bytes):
        """Writes Kiro request body to file."""
        try:
            file_path = self._current_dir() / "kiro_request_body.json"
            try:
                json_obj = json.loads(body)
                with open(file_path, "w", encoding="utf-8") as f:
                    json.dump(json_obj, f, indent=2, ensure_ascii=False)
            except json.JSONDecodeError:
                with open(file_path, "wb") as f:
                    f.write(body)
        except Exception as e:
            logger.error(f"[DebugLogger] Error writing kiro_request_body: {e}")

    def _append_raw_chunk_to_file(self, chunk: bytes):
        """Appends raw chunk to file."""
        try:
            file_path = self._current_dir() / "response_stream_raw.txt"
            with open(file_path, "ab") as f:
                f.write(chunk)
        except Exception:
            pass

    def _append_modified_chunk_to_file(self, chunk: bytes):
        """Appends modified chunk to file."""
        try:
            file_path = self._current_dir() / "response_stream_modified.txt"
            with open(file_path, "ab") as f:
                f.write(chunk)
        except Exception:
            pass

    def _write_app_logs_to_file(self):
        """Writes captured application logs to file."""
        try:
            # Get buffer contents
            logs_content = self._app_logs_buffer.getvalue()

            if not logs_content.strip():
                return

            # Ensure directory exists
            req_dir = self._current_dir()
            req_dir.mkdir(parents=True, exist_ok=True)

            file_path = req_dir / "app_logs.txt"
            with open(file_path, "w", encoding="utf-8") as f:
                f.write(logs_content)

            logger.debug(f"[DebugLogger] App logs saved to {file_path}")
        except Exception as e:
            # Don't log error via logger to avoid recursion
            pass


# Global instance
debug_logger = DebugLogger()