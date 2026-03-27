from __future__ import annotations

import json
import subprocess
from dataclasses import dataclass
from datetime import datetime

from .auth import AuthError, AuthInfo, parse_auth_bytes


USAGE_ENDPOINT = "https://chatgpt.com/backend-api/wham/usage"
REQUEST_TIMEOUT_SECONDS = 5
WINDOW_MINUTES_5H = 300
WINDOW_MINUTES_WEEKLY = 10080
TIME_FORMAT = "%Y-%m-%d %H:%M"


class UsageError(Exception):
    pass


@dataclass(frozen=True, slots=True)
class UsageWindow:
    remaining_percent: int
    window_minutes: int | None
    reset_at: int | None


@dataclass(frozen=True, slots=True)
class UsageSummary:
    five_hour: UsageWindow | None
    weekly: UsageWindow | None


def fetch_usage_from_auth_bytes(raw: bytes) -> UsageSummary:
    try:
        info, _ = parse_auth_bytes(raw)
    except AuthError as exc:
        raise UsageError(str(exc)) from exc
    return fetch_usage_for_auth(info)


def fetch_usage_for_auth(info: AuthInfo) -> UsageSummary:
    status, body = _fetch_usage_via_curl(info)
    if status != 200:
        raise UsageError(f"usage 接口返回 HTTP {status}")

    try:
        payload = json.loads(body.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise UsageError("usage 接口响应不是合法的 JSON") from exc

    return parse_usage_payload(payload)


def parse_usage_payload(payload: object) -> UsageSummary:
    if not isinstance(payload, dict):
        raise UsageError("usage 接口响应格式不正确")

    rate_limit = payload.get("rate_limit")
    if not isinstance(rate_limit, dict):
        raise UsageError("usage 响应缺少可用额度窗口")

    primary = _parse_window(rate_limit.get("primary_window"))
    secondary = _parse_window(rate_limit.get("secondary_window"))
    five_hour = _select_window(primary, secondary, WINDOW_MINUTES_5H, fallback=primary)
    weekly = _select_window(primary, secondary, WINDOW_MINUTES_WEEKLY, fallback=secondary)

    if five_hour is None and weekly is None:
        raise UsageError("usage 响应缺少可用额度窗口")

    return UsageSummary(five_hour=five_hour, weekly=weekly)


def format_usage_window(window: UsageWindow | None) -> str:
    if window is None:
        return "-"
    return f"{window.remaining_percent}%"


def format_reset_time(window: UsageWindow | None) -> str:
    if window is None or window.reset_at is None:
        return "-"
    return datetime.fromtimestamp(window.reset_at).strftime(TIME_FORMAT)


def _fetch_usage_via_curl(info: AuthInfo) -> tuple[int, bytes]:
    argv = [
        "curl",
        "--silent",
        "--show-error",
        "--location",
        "--connect-timeout",
        str(REQUEST_TIMEOUT_SECONDS),
        "--max-time",
        str(REQUEST_TIMEOUT_SECONDS),
        "--write-out",
        "\n%{http_code}",
        "-H",
        f"Authorization: Bearer {info.access_token}",
        "-H",
        f"ChatGPT-Account-Id: {info.chatgpt_account_id}",
        "-H",
        "User-Agent: codex-switch",
        USAGE_ENDPOINT,
    ]

    try:
        completed = subprocess.run(
            argv,
            check=False,
            capture_output=True,
        )
    except FileNotFoundError as exc:
        raise UsageError("网络请求失败：系统未找到 curl") from exc
    except OSError as exc:
        raise UsageError(f"网络请求失败：{exc}") from exc

    if completed.returncode != 0:
        if completed.returncode == 28:
            raise UsageError("网络请求失败：请求超时")
        stderr = completed.stderr.decode("utf-8", errors="replace").strip()
        detail = stderr or f"curl 退出码 {completed.returncode}"
        raise UsageError(f"网络请求失败：{detail}")

    body, status = _parse_curl_output(completed.stdout)
    return status, body


def _parse_curl_output(output: bytes) -> tuple[bytes, int]:
    text = output.rstrip(b"\r\n")
    try:
        body, status_text = text.rsplit(b"\n", 1)
    except ValueError as exc:
        raise UsageError("usage 接口响应格式不正确") from exc

    try:
        status = int(status_text.decode("ascii", errors="strict").strip())
    except ValueError as exc:
        raise UsageError("usage 接口响应格式不正确") from exc
    return body, status


def _select_window(
    primary: UsageWindow | None,
    secondary: UsageWindow | None,
    target_minutes: int,
    *,
    fallback: UsageWindow | None,
) -> UsageWindow | None:
    for window in (primary, secondary):
        if window is not None and window.window_minutes == target_minutes:
            return window
    return fallback


def _parse_window(payload: object) -> UsageWindow | None:
    if not isinstance(payload, dict):
        return None

    used_percent = _coerce_float(payload.get("used_percent"))
    if used_percent is None:
        return None

    limit_window_seconds = _coerce_int(payload.get("limit_window_seconds"))
    reset_at = _coerce_int(payload.get("reset_at"))
    remaining_percent = _clamp_percent(100.0 - used_percent)
    return UsageWindow(
        remaining_percent=remaining_percent,
        window_minutes=_minutes_from_seconds(limit_window_seconds),
        reset_at=reset_at if reset_at and reset_at > 0 else None,
    )


def _coerce_float(value: object) -> float | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return float(value)
    return None


def _coerce_int(value: object) -> int | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value
    if isinstance(value, float):
        return int(value)
    return None


def _minutes_from_seconds(seconds: int | None) -> int | None:
    if seconds is None or seconds <= 0:
        return None
    return (seconds + 59) // 60


def _clamp_percent(value: float) -> int:
    if value <= 0:
        return 0
    if value >= 100:
        return 100
    return int(value)
