"""扫描、列出、批量整理作业文件的小脚本。

用法示例：
    python main.py .                            # 列出当前文件夹的文件
    python main.py D:/homework -e docx -e pdf   # 只看 docx / pdf
    python main.py rename D:/homework           # 预览改名，确认后才真的改
"""

from __future__ import annotations

import argparse
import os
import sys
from datetime import datetime
from pathlib import Path

_UNITS = ("B", "KB", "MB", "GB", "TB")
COMMANDS = ("list", "rename", "archive", "undo")


def human_size(size: int) -> str:
    """把字节数变成人类可读的大小，例如 1536 -> '1.5 KB'。"""
    value = float(size)
    for unit in _UNITS:
        if value < 1024 or unit == _UNITS[-1]:
            return f"{size} B" if unit == "B" else f"{value:.1f} {unit}"
        value /= 1024
    raise AssertionError("unreachable")


def normalize_ext(ext: str) -> str:
    """把用户写的扩展名统一成小写的 '.xxx' 形式（接受 pdf / .pdf / *.pdf）。"""
    ext = ext.strip().lower().lstrip("*")
    return ext if ext.startswith(".") else f".{ext}"


def collect_files(root: Path, exts: set[str]) -> list[Path]:
    """递归收集 root 下的文件，exts 非空时只保留扩展名匹配的。"""
    files = [p for p in root.rglob("*") if p.is_file()]
    if exts:
        files = [p for p in files if p.suffix.lower() in exts]
    return sorted(files)


def file_key(path: Path) -> str:
    """判断两个路径是否指向同一个文件用的键（Windows 上大小写不敏感）。"""
    return os.path.normcase(str(path))


def ask_confirm(prompt: str) -> bool:
    """问一句 yes/no，只有明确回答 y/yes 才算确认。"""
    try:
        return input(prompt).strip().lower() in ("y", "yes")
    except EOFError:
        return False


def renamed_name(path: Path) -> str | None:
    """「学号_姓名_作业名.ext」->「作业名_学号.ext」；不符合格式时返回 None。"""
    parts = path.stem.split("_")
    if len(parts) < 3 or not all(part.strip() for part in parts):
        return None
    student_id, homework = parts[0], "_".join(parts[2:])
    return f"{homework}_{student_id}{path.suffix}"


def plan_renames(files: list[Path]) -> tuple[list[tuple[Path, Path, str]], list[tuple[Path, str]]]:
    """算出改名计划，返回 (计划, 跳过的文件及原因)；计划每项是 (原路径, 新路径, 备注)。"""
    plans: list[tuple[Path, Path, str]] = []
    skipped: list[tuple[Path, str]] = []
    for path in files:
        new_name = renamed_name(path)
        if new_name is None:
            skipped.append((path, "文件名不是「学号_姓名_作业名」"))
        elif new_name == path.name:
            skipped.append((path, "新名字和原来一样"))
        else:
            plans.append((path, path.with_name(new_name), ""))

    # 重名冲突：不能覆盖已有文件，也不能让两个文件改到同一个名字
    used = {file_key(f) for parent in {p.parent for p, _, _ in plans} for f in parent.iterdir() if f.is_file()}
    used -= {file_key(src) for src, _, _ in plans}  # 反正要改走的文件不占名字
    resolved: list[tuple[Path, Path, str]] = []
    for src, dst, _ in plans:
        candidate, index = dst, 1
        while file_key(candidate) in used:
            candidate = dst.with_name(f"{dst.stem}_{index}{dst.suffix}")
            index += 1
        used.add(file_key(candidate))
        resolved.append((src, candidate, "重名，自动加了序号" if candidate != dst else ""))
    return resolved, skipped


def print_skipped(skipped: list[tuple[Path, str]]) -> None:
    print(f"跳过 {len(skipped)} 个文件：")
    for path, reason in skipped:
        print(f"  {path.name}：{reason}")


def print_plan(plans: list[tuple[Path, Path, str]], skipped: list[tuple[Path, str]]) -> None:
    """把将要改的名字和跳过的文件打印出来给用户看。"""
    print(f"将要改名 {len(plans)} 个文件：")
    for src, dst, note in plans:
        print(f"  {src.name}  ->  {dst.name}" + (f"  （{note}）" if note else ""))
    if skipped:
        print()
        print_skipped(skipped)


def run_list(args: argparse.Namespace) -> int:
    files = collect_files(args.folder, {normalize_ext(e) for e in args.ext})
    if not files:
        print("没有找到文件")
        return 1

    total = 0
    for path in files:
        stat = path.stat()
        total += stat.st_size
        mtime = datetime.fromtimestamp(stat.st_mtime).strftime("%Y-%m-%d %H:%M:%S")
        print(f"{human_size(stat.st_size):>10}  {mtime}  {path.relative_to(args.folder)}")
    print(f"\n共 {len(files)} 个文件，合计 {human_size(total)}")
    return 0


def run_rename(args: argparse.Namespace) -> int:
    files = collect_files(args.folder, {normalize_ext(e) for e in args.ext})
    if not files:
        print("没有找到文件")
        return 1

    plans, skipped = plan_renames(files)
    if not plans:
        print("没有需要改名的文件")
        if skipped:
            print_skipped(skipped)
        return 0

    print_plan(plans, skipped)
    if not args.yes and not ask_confirm(f"\n确认执行上面 {len(plans)} 个改名？[y/N] "):
        print("已取消，没有改动任何文件")
        return 0

    for src, dst, _ in plans:
        src.rename(dst)
    print(f"\n已改名 {len(plans)} 个，跳过 {len(skipped)} 个")
    return 0


def add_target_args(parser: argparse.ArgumentParser) -> None:
    """list / rename 共用的参数：文件夹和扩展名过滤。"""
    parser.add_argument("folder", nargs="?", type=Path, default=Path("."), help="要处理的文件夹，默认当前目录")
    parser.add_argument(
        "-e",
        "--ext",
        action="append",
        default=[],
        metavar="EXT",
        help="只看这些扩展名，可重复写，例如 -e docx -e pdf",
    )


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="扫描、列出、批量整理作业文件的小脚本。")
    subparsers = parser.add_subparsers(dest="command")

    list_parser = subparsers.add_parser("list", help="列出文件的大小和修改时间")
    add_target_args(list_parser)
    list_parser.set_defaults(func=run_list)

    rename_parser = subparsers.add_parser("rename", help="按「作业名_学号.ext」批量改名，先预览再确认")
    add_target_args(rename_parser)
    rename_parser.add_argument("-y", "--yes", action="store_true", help="不询问，直接执行")
    rename_parser.set_defaults(func=run_rename)

    return parser


def main(argv: list[str] | None = None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    # 不带子命令时按需求 1 的老用法当成 list，例如 python main.py D:/homework -e pdf
    if not argv:
        argv = ["list"]
    elif argv[0] not in COMMANDS and argv[0] not in ("-h", "--help"):
        argv = ["list", *argv]

    args = build_parser().parse_args(argv)
    if not args.folder.is_dir():
        print(f"错误：{args.folder} 不是文件夹")
        return 1
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
