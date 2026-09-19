"""
Safe logging helpers — never write full secrets to disk or stdout.
"""

from __future__ import annotations

import copy
import json
import logging
import re
from pathlib import Path
from typing import Any

SECRET_KEY_EXACT = {
    "access_token",
    "client_secret",
    "fb_exchange_token",
    "appsecret_proof",
    "app_secret",
    "input_token",
    "password",
    "secret",
    "token",
}

SECRET_KEY_SUFFIXES = (
    "_token",
    "_secret",
    "secret",
)

LOG_DIR_NAME = "logs"
LOG_FILE_NAME = "meta_token_generator.log"

_logger_configured = False


def project_root() -> Path:
    return Path(__file__).resolve().parent


def log_dir() -> Path:
    path = project_root() / LOG_DIR_NAME
    path.mkdir(parents=True, exist_ok=True)
    return path


def log_file_path() -> Path:
    return log_dir() / LOG_FILE_NAME


def mask_secret(value: str | None) -> str:
    """Mask a secret for logs / UI diagnostics."""
    if value is None:
        return "***"
    text = str(value).strip()
    if not text:
        return "***"
    if len(text) < 12:
        return "***"
    return f"{text[:4]}...{text[-4:]}"


def _key_looks_secret(key: str) -> bool:
    k = key.lower().replace("-", "_")
    if k in {"token_type", "type", "expires_in", "data", "paging", "next", "previous"}:
        return False
    if k in SECRET_KEY_EXACT:
        return True
    for frag in SECRET_KEY_SUFFIXES:
        if k.endswith(frag) or frag in k:
            if "token_type" in k:
                return False
            return True
    return False


def sanitize_dict(data: Any) -> Any:
    """Recursively mask secret-looking fields in dict/list structures."""
    if isinstance(data, dict):
        out: dict[str, Any] = {}
        for key, value in data.items():
            key_s = str(key)
            if _key_looks_secret(key_s):
                if isinstance(value, str):
                    out[key_s] = mask_secret(value)
                elif value is None:
                    out[key_s] = "***"
                else:
                    out[key_s] = sanitize_dict(value)
            else:
                out[key_s] = sanitize_dict(value)
        return out
    if isinstance(data, list):
        return [sanitize_dict(item) for item in data]
    if isinstance(data, tuple):
        return [sanitize_dict(item) for item in data]
    if isinstance(data, str):
        if data.startswith(("EAA", "EAAB", "EAAG", "EAAI")) and len(data) > 20:
            return mask_secret(data)
        if "|" in data and re.fullmatch(r"\d+\|.+", data):
            left, _, right = data.partition("|")
            return f"{mask_secret(left)}|{mask_secret(right)}"
        return data
    return data


def sanitize_json_text(payload: Any, *, max_chars: int = 8000) -> str:
    try:
        text = json.dumps(sanitize_dict(payload), ensure_ascii=False, indent=2)
    except (TypeError, ValueError):
        text = str(sanitize_dict(payload))
    if len(text) > max_chars:
        return text[:max_chars] + "\n… [truncated]"
    return text


def setup_logging() -> logging.Logger:
    """Configure file logger once. Returns the app logger."""
    global _logger_configured
    logger = logging.getLogger("meta_token_generator")
    logger.setLevel(logging.DEBUG)
    logger.propagate = False

    if not _logger_configured:
        log_path = log_file_path()
        handler = logging.FileHandler(log_path, encoding="utf-8")
        handler.setLevel(logging.DEBUG)
        handler.setFormatter(
            logging.Formatter(
                fmt="%(asctime)s | %(levelname)s | %(message)s",
                datefmt="%Y-%m-%d %H:%M:%S",
            )
        )
        logger.addHandler(handler)
        _logger_configured = True
        logger.info("LOGGING initialized path=%s", log_path)

    return logger


def get_logger() -> logging.Logger:
    if not _logger_configured:
        return setup_logging()
    return logging.getLogger("meta_token_generator")


def deep_copy_sanitize(data: dict[str, Any] | None) -> dict[str, Any]:
    if not data:
        return {}
    return sanitize_dict(copy.deepcopy(data))
