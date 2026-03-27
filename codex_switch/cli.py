from __future__ import annotations

import argparse
from typing import Sequence

from .service import CliError, CodexSwitchService


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="codex-switch", description="管理和切换本机 Codex 账号快照")
    subparsers = parser.add_subparsers(dest="command", required=True)

    subparsers.add_parser("capture", help="把当前 live auth.json 纳入管理")
    subparsers.add_parser("login", help="运行 codex login 后收录当前账号")

    subparsers.add_parser("list", help="列出已管理账号")
    subparsers.add_parser("current", help="显示当前 live auth.json 对应的账号")
    subparsers.add_parser("usage", help="查询已管理账号的 5 小时和周额度")

    switch_parser = subparsers.add_parser("switch", help="切换到目标账号")
    switch_parser.add_argument("query", nargs="?", help="按邮箱精确/模糊匹配")

    remove_parser = subparsers.add_parser("remove", help="删除受管账号")
    remove_parser.add_argument("query", nargs="?", help="按邮箱精确/模糊匹配")
    return parser


def _render_table(headers: list[str], rows: list[list[str]]) -> list[str]:
    widths = [len(header) for header in headers]
    for row in rows:
        for index, cell in enumerate(row):
            widths[index] = max(widths[index], len(cell))

    rendered = ["  ".join(header.ljust(widths[index]) for index, header in enumerate(headers))]
    for row in rows:
        rendered.append("  ".join(cell.ljust(widths[index]) for index, cell in enumerate(row)))
    return rendered


def _render_grid_table(headers: list[str], rows: list[list[str]]) -> list[str]:
    widths = [len(header) for header in headers]
    for row in rows:
        for index, cell in enumerate(row):
            widths[index] = max(widths[index], len(cell))

    def border() -> str:
        return "+" + "+".join("-" * (width + 2) for width in widths) + "+"

    def render_row(cells: list[str]) -> str:
        return "| " + " | ".join(cell.ljust(widths[index]) for index, cell in enumerate(cells)) + " |"

    rendered = [border(), render_row(headers), border()]
    for row in rows:
        rendered.append(render_row(row))
    rendered.append(border())
    return rendered


def run(argv: Sequence[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    service = CodexSwitchService()
    try:
        match args.command:
            case "capture":
                record = service.capture()
                print(f"已收录账号：{record.email}")
            case "login":
                record = service.login()
                print(f"登录并收录成功：{record.email}")
            case "list":
                rows, unmanaged_live = service.list_accounts()
                if not rows:
                    print("当前没有任何已管理账号。")
                else:
                    rendered = _render_table(
                        ["EMAIL", "PLAN", "SHORT_KEY", "FLAGS"],
                        [[row["email"], row["plan"], row["short_key"], row["flags"]] for row in rows],
                    )
                    for line in rendered:
                        print(line)
                if unmanaged_live:
                    print(f"提示：当前 live 账号 {unmanaged_live} 尚未纳入管理。")
            case "current":
                current = service.current()
                print(f"邮箱：{current['email']}")
                print(f"套餐：{current['plan']}")
                print(f"短 key：{current['short_key']}")
                print(f"是否受管：{current['managed']}")
                print(f"是否 registry 当前活动账号：{current['active']}")
            case "usage":
                result = service.usage()
                if not result.rows:
                    print("当前没有任何已管理账号。")
                    return 0
                rendered = _render_grid_table(
                    ["EMAIL", "5H", "5H_RESET", "WEEKLY", "WEEKLY_RESET"],
                    [
                        [
                            row["email"],
                            row["five_hour"],
                            row["five_hour_reset"],
                            row["weekly"],
                            row["weekly_reset"],
                        ]
                        for row in result.rows
                    ],
                )
                for line in rendered:
                    print(line)
                for failure in result.failures:
                    print(f"提示：{failure}")
                return 0 if result.has_success else 1
            case "switch":
                record = service.switch(query=args.query)
                print(f"已切换到：{record.email}")
                print("提示：如果 Codex CLI 或 App 已在运行，请手动重启后再使用。")
            case "remove":
                record = service.remove(query=args.query)
                print(f"已删除账号：{record.email}")
            case _:
                parser.error("未知命令")
    except CliError as exc:
        print(f"错误：{exc}", flush=True)
        return 1
    return 0
