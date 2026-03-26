from __future__ import annotations

import base64
import json
from dataclasses import dataclass
from pathlib import Path


class AuthError(Exception):
    pass


@dataclass(slots=True)
class AuthInfo:
    email: str
    chatgpt_user_id: str
    chatgpt_account_id: str
    record_key: str
    access_token: str
    refresh_token: str
    plan: str | None
    auth_mode: str


def parse_auth_file(path: Path) -> tuple[AuthInfo, bytes]:
    try:
        raw = path.read_bytes()
    except FileNotFoundError as exc:
        raise AuthError(f"未找到认证文件：{path}") from exc
    return parse_auth_bytes(raw)


def parse_auth_bytes(raw: bytes) -> tuple[AuthInfo, bytes]:
    try:
        payload = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise AuthError("auth.json 不是合法的 JSON") from exc

    if payload.get("OPENAI_API_KEY"):
        raise AuthError("当前只支持 ChatGPT 登录态，不支持 API Key 模式")

    auth_mode = payload.get("auth_mode")
    if auth_mode != "chatgpt":
        raise AuthError("当前只支持 auth_mode=chatgpt 的认证文件")

    tokens = payload.get("tokens")
    if not isinstance(tokens, dict):
        raise AuthError("auth.json 缺少 tokens 字段")

    access_token = _require_non_empty_string(tokens.get("access_token"), "tokens.access_token")
    refresh_token = _require_non_empty_string(tokens.get("refresh_token"), "tokens.refresh_token")
    token_account_id = _require_non_empty_string(tokens.get("account_id"), "tokens.account_id")
    id_token = _require_non_empty_string(tokens.get("id_token"), "tokens.id_token")

    claims = _decode_jwt_payload(id_token)
    email = _require_non_empty_string(claims.get("email"), "email").lower()
    auth_claim = claims.get("https://api.openai.com/auth")
    if not isinstance(auth_claim, dict):
        raise AuthError("JWT 中缺少 https://api.openai.com/auth 声明")

    jwt_account_id = _require_non_empty_string(auth_claim.get("chatgpt_account_id"), "chatgpt_account_id")
    if jwt_account_id != token_account_id:
        raise AuthError("tokens.account_id 与 JWT 中的 chatgpt_account_id 不一致")

    chatgpt_user_id = auth_claim.get("chatgpt_user_id") or auth_claim.get("user_id")
    chatgpt_user_id = _require_non_empty_string(chatgpt_user_id, "chatgpt_user_id")
    plan = _optional_string(auth_claim.get("chatgpt_plan_type"))
    record_key = f"{chatgpt_user_id}::{token_account_id}"

    return (
        AuthInfo(
            email=email,
            chatgpt_user_id=chatgpt_user_id,
            chatgpt_account_id=token_account_id,
            record_key=record_key,
            access_token=access_token,
            refresh_token=refresh_token,
            plan=plan,
            auth_mode=auth_mode,
        ),
        raw,
    )


def _decode_jwt_payload(token: str) -> dict:
    parts = token.split(".")
    if len(parts) != 3:
        raise AuthError("tokens.id_token 不是合法的 JWT")

    payload = parts[1]
    padding = "=" * (-len(payload) % 4)
    try:
        decoded = base64.urlsafe_b64decode(payload + padding)
        claims = json.loads(decoded.decode("utf-8"))
    except (ValueError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise AuthError("无法解析 JWT payload") from exc

    if not isinstance(claims, dict):
        raise AuthError("JWT payload 不是对象")
    return claims


def _require_non_empty_string(value: object, field_name: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise AuthError(f"auth.json 缺少或包含空字段：{field_name}")
    return value


def _optional_string(value: object) -> str | None:
    if isinstance(value, str) and value.strip():
        return value
    return None
