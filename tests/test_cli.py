from __future__ import annotations

import base64
import io
import json
import os
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from unittest import mock

from codex_switch.cli import run
from codex_switch.store import AppPaths, load_registry


def build_auth_json(
    *,
    email: str,
    user_id: str,
    account_id: str,
    access_token: str = "access-token",
    refresh_token: str = "refresh-token",
    plan: str | None = "team",
) -> bytes:
    header = _b64({"alg": "RS256", "typ": "JWT"})
    payload = _b64(
        {
            "email": email,
            "https://api.openai.com/auth": {
                "chatgpt_account_id": account_id,
                "chatgpt_user_id": user_id,
                "chatgpt_plan_type": plan,
            },
        }
    )
    token = f"{header}.{payload}.sig"
    data = {
        "auth_mode": "chatgpt",
        "OPENAI_API_KEY": None,
        "tokens": {
            "id_token": token,
            "access_token": access_token,
            "refresh_token": refresh_token,
            "account_id": account_id,
        },
        "last_refresh": "2026-03-27T00:00:00Z",
    }
    return (json.dumps(data) + "\n").encode("utf-8")


def build_api_key_auth() -> bytes:
    data = {"auth_mode": "apikey", "OPENAI_API_KEY": "sk-test"}
    return (json.dumps(data) + "\n").encode("utf-8")


def _b64(value: dict) -> str:
    raw = json.dumps(value, separators=(",", ":")).encode("utf-8")
    return base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")


class CodexSwitchCliTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmpdir = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmpdir.cleanup)
        root = Path(self.tmpdir.name)
        self.codex_home = root / "codex-home"
        self.switcher_home = root / "switcher-home"
        self.codex_home.mkdir()
        self.switcher_home.mkdir()
        self.env = mock.patch.dict(
            os.environ,
            {
                "CODEX_HOME": str(self.codex_home),
                "CODEX_SWITCHER_HOME": str(self.switcher_home),
            },
            clear=False,
        )
        self.env.start()
        self.addCleanup(self.env.stop)

    def run_cli(self, *args: str) -> tuple[int, str]:
        buf = io.StringIO()
        with redirect_stdout(buf):
            code = run(args)
        return code, buf.getvalue()

    def write_live_auth(self, data: bytes) -> None:
        (self.codex_home / "auth.json").write_bytes(data)

    def load_registry(self):
        return load_registry(AppPaths.from_env())

    def test_capture_creates_registry_and_snapshot(self) -> None:
        self.write_live_auth(build_auth_json(email="ONE@example.com", user_id="user-1", account_id="acct-1"))
        code, output = self.run_cli("capture")
        self.assertEqual(code, 0)
        self.assertIn("已收录账号", output)

        registry = self.load_registry()
        self.assertEqual(registry.active_record_key, "user-1::acct-1")
        self.assertEqual(len(registry.accounts), 1)
        self.assertEqual(registry.accounts[0].email, "one@example.com")
        self.assertEqual(len(list(self.switcher_home.joinpath("accounts").glob("*.auth.json"))), 1)

    def test_capture_upsert_refreshes_snapshot(self) -> None:
        self.write_live_auth(build_auth_json(email="one@example.com", user_id="user-1", account_id="acct-1"))
        self.assertEqual(self.run_cli("capture")[0], 0)

        updated = build_auth_json(
            email="one@example.com",
            user_id="user-1",
            account_id="acct-1",
            access_token="new-access",
            plan="pro",
        )
        self.write_live_auth(updated)
        self.assertEqual(self.run_cli("capture")[0], 0)

        registry = self.load_registry()
        self.assertEqual(registry.accounts[0].plan, "pro")
        snapshot_path = AppPaths.from_env().snapshot_path("user-1::acct-1")
        self.assertEqual(snapshot_path.read_bytes(), updated)

    def test_login_success_captures_account(self) -> None:
        self.write_live_auth(build_auth_json(email="login@example.com", user_id="user-2", account_id="acct-2"))
        with mock.patch("codex_switch.service.subprocess.run") as mocked_run:
            mocked_run.return_value.returncode = 0
            code, _ = self.run_cli("login")
        self.assertEqual(code, 0)
        registry = self.load_registry()
        self.assertEqual(registry.accounts[0].email, "login@example.com")
        mocked_run.assert_called_once_with(["codex", "login"], check=False)

    def test_login_failure_does_not_write_registry(self) -> None:
        self.write_live_auth(build_auth_json(email="login@example.com", user_id="user-2", account_id="acct-2"))
        with mock.patch("codex_switch.service.subprocess.run") as mocked_run:
            mocked_run.return_value.returncode = 7
            code, output = self.run_cli("login")
        self.assertEqual(code, 1)
        self.assertIn("执行失败", output)
        self.assertEqual(self.load_registry().accounts, [])

    def test_switch_rewrites_live_auth_creates_backup_and_sets_active(self) -> None:
        auth_a = build_auth_json(email="a@example.com", user_id="user-a", account_id="acct-a")
        auth_b = build_auth_json(email="b@example.com", user_id="user-b", account_id="acct-b")
        self.write_live_auth(auth_a)
        self.assertEqual(self.run_cli("capture")[0], 0)
        self.write_live_auth(auth_b)
        self.assertEqual(self.run_cli("capture")[0], 0)

        self.write_live_auth(auth_a)
        code, output = self.run_cli("switch", "b@example.com")
        self.assertEqual(code, 0)
        self.assertIn("手动重启", output)
        self.assertEqual((self.codex_home / "auth.json").read_bytes(), auth_b)
        backups = list((self.switcher_home / "backups").glob("auth.json.bak.*"))
        self.assertEqual(len(backups), 1)
        registry = self.load_registry()
        self.assertEqual(registry.active_record_key, "user-b::acct-b")

    def test_switch_query_matching_and_non_tty_ambiguity(self) -> None:
        self.write_live_auth(build_auth_json(email="alpha-main@example.com", user_id="user-1", account_id="acct-1"))
        self.assertEqual(self.run_cli("capture")[0], 0)
        self.write_live_auth(build_auth_json(email="alpha-side@example.com", user_id="user-2", account_id="acct-2"))
        self.assertEqual(self.run_cli("capture")[0], 0)

        with mock.patch("sys.stdin.isatty", return_value=False):
            code, output = self.run_cli("switch", "alpha")
        self.assertEqual(code, 1)
        self.assertIn("多个账号", output)

        code, output = self.run_cli("switch", "alpha-side@example.com")
        self.assertEqual(code, 0)
        self.assertIn("已切换到", output)
        self.assertIn("alpha-side@example.com", output)

    def test_sync_live_auth_updates_managed_snapshot(self) -> None:
        initial = build_auth_json(email="sync@example.com", user_id="user-1", account_id="acct-1", access_token="old")
        self.write_live_auth(initial)
        self.assertEqual(self.run_cli("capture")[0], 0)

        refreshed = build_auth_json(email="sync@example.com", user_id="user-1", account_id="acct-1", access_token="new")
        self.write_live_auth(refreshed)
        code, _ = self.run_cli("list")
        self.assertEqual(code, 0)
        snapshot_path = AppPaths.from_env().snapshot_path("user-1::acct-1")
        self.assertEqual(snapshot_path.read_bytes(), refreshed)

    def test_list_output_uses_space_padded_columns_instead_of_tabs(self) -> None:
        self.write_live_auth(build_auth_json(email="short@example.com", user_id="user-1", account_id="acct-1"))
        self.assertEqual(self.run_cli("capture")[0], 0)
        self.write_live_auth(
            build_auth_json(email="much.longer.email@example.com", user_id="user-2", account_id="acct-2")
        )
        self.assertEqual(self.run_cli("capture")[0], 0)

        code, output = self.run_cli("list")
        self.assertEqual(code, 0)
        self.assertNotIn("\t", output)
        self.assertTrue(output.splitlines()[0].startswith("EMAIL"))

    def test_remove_deletes_snapshot_and_clears_active_pointer_only(self) -> None:
        auth_a = build_auth_json(email="a@example.com", user_id="user-a", account_id="acct-a")
        auth_b = build_auth_json(email="b@example.com", user_id="user-b", account_id="acct-b")
        self.write_live_auth(auth_a)
        self.assertEqual(self.run_cli("capture")[0], 0)
        self.write_live_auth(auth_b)
        self.assertEqual(self.run_cli("capture")[0], 0)
        self.assertEqual(self.run_cli("switch", "b@example.com")[0], 0)

        code, output = self.run_cli("remove", "b@example.com")
        self.assertEqual(code, 0)
        self.assertIn("已删除账号", output)
        self.assertEqual((self.codex_home / "auth.json").read_bytes(), auth_b)
        registry = self.load_registry()
        self.assertIsNone(registry.active_record_key)
        self.assertEqual(len(registry.accounts), 1)
        self.assertFalse(AppPaths.from_env().snapshot_path("user-b::acct-b").exists())

    def test_current_reports_unmanaged_live_auth(self) -> None:
        managed = build_auth_json(email="managed@example.com", user_id="user-1", account_id="acct-1")
        self.write_live_auth(managed)
        self.assertEqual(self.run_cli("capture")[0], 0)

        unmanaged = build_auth_json(email="other@example.com", user_id="user-2", account_id="acct-2")
        self.write_live_auth(unmanaged)
        code, output = self.run_cli("current")
        self.assertEqual(code, 0)
        self.assertIn("是否受管：no", output)
        self.assertIn("邮箱：other@example.com", output)

    def test_invalid_auth_is_rejected_for_required_sync_commands(self) -> None:
        self.write_live_auth(build_api_key_auth())
        for command in (("list",), ("current",), ("switch",), ("remove",)):
            code, output = self.run_cli(*command)
            self.assertEqual(code, 1)
            self.assertIn("不支持 API Key 模式", output)

    def test_malformed_auth_is_rejected(self) -> None:
        self.write_live_auth(b"{bad json")
        code, output = self.run_cli("capture")
        self.assertEqual(code, 1)
        self.assertIn("不是合法的 JSON", output)

    def test_invalid_registry_is_rejected_cleanly(self) -> None:
        self.write_live_auth(build_auth_json(email="one@example.com", user_id="user-1", account_id="acct-1"))
        (self.switcher_home / "registry.json").write_text("{bad json", encoding="utf-8")
        code, output = self.run_cli("list")
        self.assertEqual(code, 1)
        self.assertIn("registry.json 不是合法的 JSON", output)

    def test_old_registry_with_label_field_is_still_readable(self) -> None:
        self.write_live_auth(build_auth_json(email="legacy@example.com", user_id="user-1", account_id="acct-1"))
        legacy_registry = {
            "schema_version": 1,
            "active_record_key": "user-1::acct-1",
            "accounts": [
                {
                    "record_key": "user-1::acct-1",
                    "email": "legacy@example.com",
                    "label": "legacy",
                    "plan": "team",
                    "chatgpt_user_id": "user-1",
                    "chatgpt_account_id": "acct-1",
                    "auth_mode": "chatgpt",
                }
            ],
        }
        (self.switcher_home / "registry.json").write_text(json.dumps(legacy_registry), encoding="utf-8")
        snapshot_path = AppPaths.from_env().snapshot_path("user-1::acct-1")
        snapshot_path.parent.mkdir(parents=True, exist_ok=True)
        snapshot_path.write_bytes(build_auth_json(email="legacy@example.com", user_id="user-1", account_id="acct-1"))

        code, output = self.run_cli("list")
        self.assertEqual(code, 0)
        self.assertIn("legacy@example.com", output)


if __name__ == "__main__":
    unittest.main()
