from __future__ import annotations

import base64
import io
import json
import os
import subprocess
import tempfile
import unittest
from contextlib import redirect_stdout
from datetime import datetime
from pathlib import Path
from unittest import mock

from codex_switch import usage
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


def build_usage_response(
    *,
    primary_window: dict[str, object] | None = None,
    secondary_window: dict[str, object] | None = None,
) -> bytes:
    rate_limit: dict[str, object] = {}
    if primary_window is not None:
        rate_limit["primary_window"] = primary_window
    if secondary_window is not None:
        rate_limit["secondary_window"] = secondary_window
    return json.dumps({"rate_limit": rate_limit}).encode("utf-8")


def _b64(value: dict) -> str:
    raw = json.dumps(value, separators=(",", ":")).encode("utf-8")
    return base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")


def build_curl_result(
    *,
    body: bytes = b"",
    status: int = 200,
    returncode: int = 0,
    stderr: bytes = b"",
) -> subprocess.CompletedProcess[bytes]:
    stdout = body + f"\n{status}".encode("ascii") if returncode == 0 else b""
    return subprocess.CompletedProcess(
        args=["curl"],
        returncode=returncode,
        stdout=stdout,
        stderr=stderr,
    )


def get_curl_header(argv: list[str], name: str) -> str | None:
    for index, item in enumerate(argv[:-1]):
        if item == "-H":
            header = argv[index + 1]
            prefix = f"{name}: "
            if header.startswith(prefix):
                return header[len(prefix) :]
    return None


class ImmediateFuture:
    def __init__(self, result):
        self._result = result

    def result(self):
        return self._result


class RecordingExecutor:
    instances: list["RecordingExecutor"] = []

    def __init__(self, max_workers: int) -> None:
        self.max_workers = max_workers
        self.submissions: list[tuple[object, tuple[object, ...], dict[str, object]]] = []
        self.__class__.instances.append(self)

    def __enter__(self) -> "RecordingExecutor":
        return self

    def __exit__(self, exc_type, exc, tb) -> bool:
        return False

    def submit(self, fn, *args, **kwargs) -> ImmediateFuture:
        self.submissions.append((fn, args, kwargs))
        return ImmediateFuture(fn(*args, **kwargs))


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

    def snapshot_path(self, record_key: str) -> Path:
        return AppPaths.from_env().snapshot_path(record_key)

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

    def test_usage_parses_exact_and_fallback_windows(self) -> None:
        parsed = usage.parse_usage_payload(
            {
                "rate_limit": {
                    "primary_window": {
                        "used_percent": 25.0,
                        "limit_window_seconds": 18000,
                        "reset_at": 1700000000,
                    },
                    "secondary_window": {
                        "used_percent": 10.0,
                        "limit_window_seconds": 604800,
                        "reset_at": 1700100000,
                    },
                }
            }
        )
        self.assertEqual(parsed.five_hour.remaining_percent, 75)
        self.assertEqual(parsed.five_hour.window_minutes, 300)
        self.assertEqual(parsed.weekly.remaining_percent, 90)
        self.assertEqual(parsed.weekly.window_minutes, 10080)

        fallback = usage.parse_usage_payload(
            {
                "rate_limit": {
                    "primary_window": {
                        "used_percent": 40.0,
                        "limit_window_seconds": 7200,
                        "reset_at": 1700200000,
                    },
                    "secondary_window": {
                        "used_percent": 5.0,
                        "limit_window_seconds": 14400,
                        "reset_at": 1700300000,
                    },
                }
            }
        )
        self.assertEqual(fallback.five_hour.remaining_percent, 60)
        self.assertEqual(fallback.five_hour.window_minutes, 120)
        self.assertEqual(fallback.weekly.remaining_percent, 95)
        self.assertEqual(fallback.weekly.window_minutes, 240)

    def test_usage_outputs_table_for_each_managed_account(self) -> None:
        auth_a = build_auth_json(email="a@example.com", user_id="user-a", account_id="acct-a")
        auth_b = build_auth_json(email="b@example.com", user_id="user-b", account_id="acct-b")
        self.write_live_auth(auth_b)
        self.assertEqual(self.run_cli("capture")[0], 0)
        self.write_live_auth(auth_a)
        self.assertEqual(self.run_cli("capture")[0], 0)

        reset_5h = datetime.fromtimestamp(1700000000).strftime("%Y-%m-%d %H:%M")
        reset_week = datetime.fromtimestamp(1700100000).strftime("%Y-%m-%d %H:%M")
        reset_5h_b = datetime.fromtimestamp(1700200000).strftime("%Y-%m-%d %H:%M")
        reset_week_b = datetime.fromtimestamp(1700300000).strftime("%Y-%m-%d %H:%M")

        def fake_run(argv, check, capture_output):
            self.assertFalse(check)
            self.assertTrue(capture_output)
            self.assertEqual(argv[0], "curl")
            account_id = get_curl_header(argv, "ChatGPT-Account-Id")
            if account_id == "acct-a":
                return build_curl_result(
                    body=build_usage_response(
                        primary_window={
                            "used_percent": 25.0,
                            "limit_window_seconds": 18000,
                            "reset_at": 1700000000,
                        },
                        secondary_window={
                            "used_percent": 10.0,
                            "limit_window_seconds": 604800,
                            "reset_at": 1700100000,
                        },
                    ),
                )
            if account_id == "acct-b":
                return build_curl_result(
                    body=build_usage_response(
                        primary_window={
                            "used_percent": 50.0,
                            "limit_window_seconds": 18000,
                            "reset_at": 1700200000,
                        },
                        secondary_window={
                            "used_percent": 35.0,
                            "limit_window_seconds": 604800,
                            "reset_at": 1700300000,
                        },
                    ),
                )
            raise AssertionError(account_id)

        with mock.patch("codex_switch.usage.subprocess.run", side_effect=fake_run):
            code, output = self.run_cli("usage")

        self.assertEqual(code, 0)
        self.assertIn("+", output)
        self.assertIn("| EMAIL", output)
        self.assertIn("EMAIL", output)
        self.assertIn("5H_RESET", output)
        self.assertIn("WEEKLY_RESET", output)
        self.assertLess(output.index("a@example.com"), output.index("b@example.com"))
        self.assertIn("75%", output)
        self.assertIn(reset_5h, output)
        self.assertIn("90%", output)
        self.assertIn(reset_week, output)
        self.assertIn("50%", output)
        self.assertIn(reset_5h_b, output)
        self.assertIn("65%", output)
        self.assertIn(reset_week_b, output)

    def test_usage_prefers_live_auth_without_rewriting_snapshot(self) -> None:
        snapshot_auth = build_auth_json(
            email="live@example.com",
            user_id="user-live",
            account_id="acct-live",
            access_token="snapshot-token",
        )
        self.write_live_auth(snapshot_auth)
        self.assertEqual(self.run_cli("capture")[0], 0)

        live_auth = build_auth_json(
            email="live@example.com",
            user_id="user-live",
            account_id="acct-live",
            access_token="live-token",
        )
        self.write_live_auth(live_auth)

        captured_tokens: list[str | None] = []

        def fake_run(argv, check, capture_output):
            _ = check
            _ = capture_output
            captured_tokens.append(get_curl_header(argv, "Authorization"))
            return build_curl_result(
                body=build_usage_response(
                    primary_window={
                        "used_percent": 20.0,
                        "limit_window_seconds": 18000,
                        "reset_at": 1700400000,
                    }
                ),
            )

        with mock.patch("codex_switch.usage.subprocess.run", side_effect=fake_run):
            code, _output = self.run_cli("usage")

        self.assertEqual(code, 0)
        self.assertEqual(captured_tokens, ["Bearer live-token"])
        self.assertEqual(self.snapshot_path("user-live::acct-live").read_bytes(), snapshot_auth)
        registry = self.load_registry()
        self.assertEqual(registry.active_record_key, "user-live::acct-live")

    def test_usage_uses_curl_directly(self) -> None:
        self.write_live_auth(
            build_auth_json(email="curl@example.com", user_id="user-curl", account_id="acct-curl")
        )
        self.assertEqual(self.run_cli("capture")[0], 0)

        with mock.patch(
            "codex_switch.usage.subprocess.run",
            return_value=build_curl_result(
                body=build_usage_response(
                    primary_window={
                        "used_percent": 15.0,
                        "limit_window_seconds": 18000,
                        "reset_at": 1700700000,
                    },
                    secondary_window={
                        "used_percent": 5.0,
                        "limit_window_seconds": 604800,
                        "reset_at": 1700800000,
                    },
                )
            ),
        ) as mocked_run:
            code, output = self.run_cli("usage")

        self.assertEqual(code, 0)
        self.assertIn("85%", output)
        self.assertIn("95%", output)
        mocked_run.assert_called_once()
        argv = mocked_run.call_args.args[0]
        self.assertIn("curl", argv[0])
        self.assertEqual(get_curl_header(argv, "ChatGPT-Account-Id"), "acct-curl")
        self.assertEqual(get_curl_header(argv, "User-Agent"), "codex-switch")

    def test_usage_uses_thread_pool_for_parallel_queries(self) -> None:
        RecordingExecutor.instances.clear()
        auth_a = build_auth_json(email="a@example.com", user_id="user-a", account_id="acct-a")
        auth_b = build_auth_json(email="b@example.com", user_id="user-b", account_id="acct-b")
        self.write_live_auth(auth_b)
        self.assertEqual(self.run_cli("capture")[0], 0)
        self.write_live_auth(auth_a)
        self.assertEqual(self.run_cli("capture")[0], 0)

        summary = usage.UsageSummary(
            five_hour=usage.UsageWindow(remaining_percent=80, window_minutes=300, reset_at=1700900000),
            weekly=usage.UsageWindow(remaining_percent=90, window_minutes=10080, reset_at=1701000000),
        )

        with (
            mock.patch("codex_switch.service.ThreadPoolExecutor", RecordingExecutor),
            mock.patch("codex_switch.service.fetch_usage_from_auth_bytes", return_value=summary),
        ):
            code, output = self.run_cli("usage")

        self.assertEqual(code, 0)
        self.assertEqual(len(RecordingExecutor.instances), 1)
        executor = RecordingExecutor.instances[0]
        self.assertEqual(executor.max_workers, 2)
        self.assertEqual(len(executor.submissions), 2)
        self.assertIn("a@example.com", output)
        self.assertIn("b@example.com", output)

    def test_usage_tolerates_partial_failures(self) -> None:
        auth_ok = build_auth_json(email="ok@example.com", user_id="user-ok", account_id="acct-ok")
        auth_fail = build_auth_json(email="fail@example.com", user_id="user-fail", account_id="acct-fail")
        self.write_live_auth(auth_fail)
        self.assertEqual(self.run_cli("capture")[0], 0)
        self.write_live_auth(auth_ok)
        self.assertEqual(self.run_cli("capture")[0], 0)

        def fake_run(argv, check, capture_output):
            _ = check
            _ = capture_output
            account_id = get_curl_header(argv, "ChatGPT-Account-Id")
            if account_id == "acct-fail":
                return build_curl_result(returncode=35, stderr=b"boom")
            return build_curl_result(
                body=build_usage_response(
                    primary_window={
                        "used_percent": 30.0,
                        "limit_window_seconds": 18000,
                        "reset_at": 1700500000,
                    }
                ),
            )

        with mock.patch("codex_switch.usage.subprocess.run", side_effect=fake_run):
            code, output = self.run_cli("usage")

        self.assertEqual(code, 0)
        fail_line = next(line for line in output.splitlines() if "| fail@example.com " in line)
        self.assertRegex(fail_line, r"^\| fail@example\.com\s+\|\s+-\s+\|\s+-\s+\|\s+-\s+\|\s+-\s+\|$")
        self.assertIn("提示：fail@example.com 查询失败：网络请求失败：boom", output)
        self.assertIn("ok@example.com", output)
        self.assertIn("70%", output)

    def test_usage_returns_nonzero_when_all_accounts_fail(self) -> None:
        self.write_live_auth(build_auth_json(email="fail@example.com", user_id="user-fail", account_id="acct-fail"))
        self.assertEqual(self.run_cli("capture")[0], 0)

        with mock.patch(
            "codex_switch.usage.subprocess.run",
            return_value=build_curl_result(returncode=35, stderr=b"down"),
        ):
            code, output = self.run_cli("usage")

        self.assertEqual(code, 1)
        self.assertIn("提示：fail@example.com 查询失败：网络请求失败：down", output)

    def test_usage_reports_empty_registry(self) -> None:
        code, output = self.run_cli("usage")
        self.assertEqual(code, 0)
        self.assertIn("当前没有任何已管理账号。", output)

    def test_usage_treats_missing_snapshot_as_row_level_failure(self) -> None:
        auth_ok = build_auth_json(email="ok@example.com", user_id="user-ok", account_id="acct-ok")
        auth_missing = build_auth_json(email="missing@example.com", user_id="user-missing", account_id="acct-missing")
        self.write_live_auth(auth_missing)
        self.assertEqual(self.run_cli("capture")[0], 0)
        self.write_live_auth(auth_ok)
        self.assertEqual(self.run_cli("capture")[0], 0)
        self.snapshot_path("user-missing::acct-missing").unlink()

        def fake_run(argv, check, capture_output):
            _ = argv
            _ = check
            _ = capture_output
            return build_curl_result(
                body=build_usage_response(
                    primary_window={
                        "used_percent": 45.0,
                        "limit_window_seconds": 18000,
                        "reset_at": 1700600000,
                    }
                ),
            )

        with mock.patch("codex_switch.usage.subprocess.run", side_effect=fake_run):
            code, output = self.run_cli("usage")

        self.assertEqual(code, 0)
        self.assertIn("提示：missing@example.com 查询失败：账号快照不存在：", output)
        self.assertIn("ok@example.com", output)


if __name__ == "__main__":
    unittest.main()
