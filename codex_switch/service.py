from __future__ import annotations

import subprocess
import sys
from dataclasses import dataclass

from .auth import AuthError, AuthInfo, parse_auth_file
from .store import (
    AccountRecord,
    AppPaths,
    Registry,
    bytes_equal,
    create_backup,
    load_registry,
    save_registry,
    short_key,
    write_bytes_atomic,
)


class CliError(Exception):
    pass


@dataclass(slots=True)
class SyncResult:
    live_info: AuthInfo
    live_bytes: bytes
    managed: bool
    changed: bool


class CodexSwitchService:
    def __init__(self, paths: AppPaths | None = None) -> None:
        self.paths = paths or AppPaths.from_env()
        self.paths.ensure()

    def capture(self) -> AccountRecord:
        registry = self._load_registry()
        live_info, live_bytes = self._parse_live_auth()
        record = self._upsert_account(registry, live_info)
        self._write_snapshot(live_info.record_key, live_bytes)
        registry.active_record_key = live_info.record_key
        save_registry(self.paths, registry)
        return record

    def login(self) -> AccountRecord:
        try:
            completed = subprocess.run(["codex", "login"], check=False)
        except FileNotFoundError as exc:
            raise CliError("未找到 `codex` 命令，请先安装 Codex CLI 并确保它在 PATH 中。") from exc
        if completed.returncode != 0:
            raise CliError(f"`codex login` 执行失败，退出码：{completed.returncode}")
        return self.capture()

    def list_accounts(self) -> tuple[list[dict[str, str]], str | None]:
        registry, sync = self._load_and_sync_required_command()
        rows: list[dict[str, str]] = []
        for account in sorted(registry.accounts, key=lambda item: (item.email, item.record_key)):
            flags: list[str] = []
            if sync.live_info.record_key == account.record_key:
                flags.append("live")
            if registry.active_record_key == account.record_key:
                flags.append("active")
            rows.append(
                {
                    "email": account.email,
                    "plan": account.plan or "-",
                    "short_key": short_key(account.record_key),
                    "flags": ",".join(flags) if flags else "-",
                }
            )
        unmanaged_live = None
        if sync.live_info.record_key and not sync.managed:
            unmanaged_live = sync.live_info.email
        return rows, unmanaged_live

    def current(self) -> dict[str, str]:
        registry, sync = self._load_and_sync_required_command()
        managed_account = registry.find(sync.live_info.record_key)
        return {
            "email": sync.live_info.email,
            "plan": sync.live_info.plan or "-",
            "record_key": sync.live_info.record_key,
            "short_key": short_key(sync.live_info.record_key),
            "managed": "yes" if managed_account else "no",
            "active": "yes" if registry.active_record_key == sync.live_info.record_key else "no",
        }

    def switch(self, query: str | None = None, stdin_is_tty: bool | None = None) -> AccountRecord:
        registry, _ = self._load_and_sync_required_command()
        if not registry.accounts:
            raise CliError("当前没有任何已管理账号，请先运行 `codex-switch capture` 或 `codex-switch login`。")

        matches = self._resolve_accounts(registry, query)
        target = self._choose_account(matches, stdin_is_tty=stdin_is_tty)

        snapshot_path = self.paths.snapshot_path(target.record_key)
        try:
            snapshot_bytes = snapshot_path.read_bytes()
        except FileNotFoundError as exc:
            raise CliError(f"账号快照不存在：{snapshot_path}") from exc

        live_exists = self.paths.live_auth_path.exists()
        live_bytes = self.paths.live_auth_path.read_bytes() if live_exists else b""
        if live_exists and live_bytes != snapshot_bytes:
            create_backup(self.paths, live_bytes)

        write_bytes_atomic(self.paths.live_auth_path, snapshot_bytes)
        registry.active_record_key = target.record_key
        save_registry(self.paths, registry)
        return target

    def remove(self, query: str | None = None, stdin_is_tty: bool | None = None) -> AccountRecord:
        registry, _ = self._load_and_sync_required_command()
        if not registry.accounts:
            raise CliError("当前没有任何已管理账号可删除。")

        matches = self._resolve_accounts(registry, query)
        target = self._choose_account(matches, stdin_is_tty=stdin_is_tty)

        removed = registry.remove(target.record_key)
        if removed is None:
            raise CliError("待删除账号不存在。")
        self.paths.snapshot_path(target.record_key).unlink(missing_ok=True)
        if registry.active_record_key == target.record_key:
            registry.active_record_key = None
        save_registry(self.paths, registry)
        return target

    def _load_and_sync_required_command(self) -> tuple[Registry, SyncResult]:
        registry = self._load_registry()
        sync = self._sync_live_auth(registry)
        if sync.changed:
            save_registry(self.paths, registry)
        return registry, sync

    def _load_registry(self) -> Registry:
        try:
            return load_registry(self.paths)
        except ValueError as exc:
            raise CliError(str(exc)) from exc

    def _sync_live_auth(self, registry: Registry) -> SyncResult:
        live_info, live_bytes = self._parse_live_auth()
        account = registry.find(live_info.record_key)
        changed = False
        if account is not None:
            if self._update_account_record(account, live_info):
                changed = True
            snapshot_path = self.paths.snapshot_path(live_info.record_key)
            if not bytes_equal(live_bytes, snapshot_path):
                self._write_snapshot(live_info.record_key, live_bytes)
                changed = True
            if registry.active_record_key != live_info.record_key:
                registry.active_record_key = live_info.record_key
                changed = True
        return SyncResult(
            live_info=live_info,
            live_bytes=live_bytes,
            managed=account is not None,
            changed=changed,
        )

    def _parse_live_auth(self) -> tuple[AuthInfo, bytes]:
        try:
            return parse_auth_file(self.paths.live_auth_path)
        except AuthError as exc:
            raise CliError(str(exc)) from exc

    def _write_snapshot(self, record_key: str, raw_bytes: bytes) -> None:
        write_bytes_atomic(self.paths.snapshot_path(record_key), raw_bytes)

    def _upsert_account(self, registry: Registry, live_info: AuthInfo) -> AccountRecord:
        existing = registry.find(live_info.record_key)
        if existing is None:
            record = AccountRecord(
                record_key=live_info.record_key,
                email=live_info.email,
                plan=live_info.plan,
                chatgpt_user_id=live_info.chatgpt_user_id,
                chatgpt_account_id=live_info.chatgpt_account_id,
                auth_mode=live_info.auth_mode,
            )
            registry.accounts.append(record)
            return record

        self._update_account_record(existing, live_info)
        return existing

    def _update_account_record(self, account: AccountRecord, live_info: AuthInfo) -> bool:
        changed = False
        for field_name, new_value in (
            ("email", live_info.email),
            ("plan", live_info.plan),
            ("chatgpt_user_id", live_info.chatgpt_user_id),
            ("chatgpt_account_id", live_info.chatgpt_account_id),
            ("auth_mode", live_info.auth_mode),
        ):
            if getattr(account, field_name) != new_value:
                setattr(account, field_name, new_value)
                changed = True
        return changed

    def _resolve_accounts(self, registry: Registry, query: str | None) -> list[AccountRecord]:
        accounts = sorted(registry.accounts, key=lambda item: (item.email, item.record_key))
        if query is None:
            return accounts

        normalized = query.strip().lower()
        if not normalized:
            return accounts

        def exact_email() -> list[AccountRecord]:
            return [item for item in accounts if item.email.lower() == normalized]

        def fuzzy_email() -> list[AccountRecord]:
            return [item for item in accounts if normalized in item.email.lower()]

        for matcher in (exact_email, fuzzy_email):
            matches = matcher()
            if matches:
                return matches
        raise CliError(f"没有匹配到账号：{query}")

    def _choose_account(self, matches: list[AccountRecord], stdin_is_tty: bool | None = None) -> AccountRecord:
        if len(matches) == 1:
            return matches[0]
        tty = sys.stdin.isatty() if stdin_is_tty is None else stdin_is_tty
        if not tty:
            raise CliError("匹配到多个账号，当前不是交互终端，请提供更精确的查询。")

        emails = [account.email for account in matches]
        plans = [account.plan or "-" for account in matches]
        keys = [short_key(account.record_key) for account in matches]
        email_width = max(len("EMAIL"), *(len(value) for value in emails))
        plan_width = max(len("PLAN"), *(len(value) for value in plans))

        for index, _account in enumerate(matches, start=1):
            email = emails[index - 1]
            plan = plans[index - 1]
            key = keys[index - 1]
            print(
                f"{index}. "
                f"{email.ljust(email_width)}  "
                f"{plan.ljust(plan_width)}  "
                f"{key}"
            )
        raw = input("请输入序号：").strip()
        if not raw.isdigit():
            raise CliError("输入无效，请输入数字序号。")
        selected = int(raw)
        if selected < 1 or selected > len(matches):
            raise CliError("输入超出范围。")
        return matches[selected - 1]
