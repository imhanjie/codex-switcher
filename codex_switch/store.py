from __future__ import annotations

import base64
import json
from dataclasses import asdict, dataclass, field
from datetime import datetime
from pathlib import Path
from tempfile import NamedTemporaryFile


SCHEMA_VERSION = 1
MAX_BACKUPS = 5


@dataclass(slots=True)
class AccountRecord:
    record_key: str
    email: str
    plan: str | None
    chatgpt_user_id: str
    chatgpt_account_id: str
    auth_mode: str


@dataclass(slots=True)
class Registry:
    schema_version: int = SCHEMA_VERSION
    active_record_key: str | None = None
    accounts: list[AccountRecord] = field(default_factory=list)

    def find(self, record_key: str) -> AccountRecord | None:
        for account in self.accounts:
            if account.record_key == record_key:
                return account
        return None

    def remove(self, record_key: str) -> AccountRecord | None:
        for index, account in enumerate(self.accounts):
            if account.record_key == record_key:
                return self.accounts.pop(index)
        return None


@dataclass(frozen=True, slots=True)
class AppPaths:
    codex_home: Path
    switcher_home: Path
    live_auth_path: Path
    registry_path: Path
    accounts_dir: Path
    backups_dir: Path

    @classmethod
    def from_env(cls) -> "AppPaths":
        import os

        codex_home = Path(os.environ.get("CODEX_HOME", Path.home() / ".codex")).expanduser()
        switcher_home = Path(
            os.environ.get("CODEX_SWITCHER_HOME", Path.home() / ".codex-switcher")
        ).expanduser()
        return cls(
            codex_home=codex_home,
            switcher_home=switcher_home,
            live_auth_path=codex_home / "auth.json",
            registry_path=switcher_home / "registry.json",
            accounts_dir=switcher_home / "accounts",
            backups_dir=switcher_home / "backups",
        )

    def ensure(self) -> None:
        self.codex_home.mkdir(parents=True, exist_ok=True)
        self.switcher_home.mkdir(parents=True, exist_ok=True)
        self.accounts_dir.mkdir(parents=True, exist_ok=True)
        self.backups_dir.mkdir(parents=True, exist_ok=True)

    def snapshot_path(self, record_key: str) -> Path:
        return self.accounts_dir / f"{encode_record_key(record_key)}.auth.json"


def encode_record_key(record_key: str) -> str:
    encoded = base64.urlsafe_b64encode(record_key.encode("utf-8")).decode("ascii")
    return encoded.rstrip("=")


def short_key(record_key: str) -> str:
    return encode_record_key(record_key)[:10]


def load_registry(paths: AppPaths) -> Registry:
    try:
        data = json.loads(paths.registry_path.read_text("utf-8"))
    except FileNotFoundError:
        return Registry()
    except json.JSONDecodeError as exc:
        raise ValueError("registry.json 不是合法的 JSON") from exc

    accounts = [
        AccountRecord(
            record_key=item["record_key"],
            email=item["email"],
            plan=item.get("plan"),
            chatgpt_user_id=item["chatgpt_user_id"],
            chatgpt_account_id=item["chatgpt_account_id"],
            auth_mode=item.get("auth_mode", "chatgpt"),
        )
        for item in data.get("accounts", [])
    ]
    return Registry(
        schema_version=int(data.get("schema_version", SCHEMA_VERSION)),
        active_record_key=data.get("active_record_key"),
        accounts=accounts,
    )


def save_registry(paths: AppPaths, registry: Registry) -> None:
    paths.ensure()
    payload = {
        "schema_version": SCHEMA_VERSION,
        "active_record_key": registry.active_record_key,
        "accounts": [asdict(account) for account in registry.accounts],
    }
    write_text_atomic(paths.registry_path, json.dumps(payload, ensure_ascii=False, indent=2) + "\n")


def write_text_atomic(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with NamedTemporaryFile("w", encoding="utf-8", dir=path.parent, delete=False) as handle:
        handle.write(text)
        temp_path = Path(handle.name)
    temp_path.replace(path)


def write_bytes_atomic(path: Path, data: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with NamedTemporaryFile("wb", dir=path.parent, delete=False) as handle:
        handle.write(data)
        temp_path = Path(handle.name)
    temp_path.replace(path)


def bytes_equal(left: bytes, right_path: Path) -> bool:
    try:
        return right_path.read_bytes() == left
    except FileNotFoundError:
        return False


def create_backup(paths: AppPaths, live_bytes: bytes) -> Path:
    paths.ensure()
    timestamp = datetime.now().strftime("%Y%m%d-%H%M%S-%f")
    backup_path = paths.backups_dir / f"auth.json.bak.{timestamp}"
    write_bytes_atomic(backup_path, live_bytes)
    prune_backups(paths)
    return backup_path


def prune_backups(paths: AppPaths) -> None:
    backups = sorted(paths.backups_dir.glob("auth.json.bak.*"))
    overflow = len(backups) - MAX_BACKUPS
    if overflow <= 0:
        return
    for old_path in backups[:overflow]:
        old_path.unlink(missing_ok=True)
