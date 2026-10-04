"""扫描文件夹，列出所有文件的大小和修改时间，支持按扩展名过滤。

用法示例：
    python main.py .                      # 列出当前文件夹（含子文件夹）的所有文件
    python main.py D:/homework -e docx -e pdf
"""

from __future__ import annotations

import argparse
from datetime import datetime
from pathlib import Path

_UNITS = ("B", "KB", "MB", "GB", "TB")


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


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="扫描文件夹，列出文件的大小和修改时间。")
    parser.add_argument("folder", nargs="?", type=Path, default=Path("."), help="要扫描的文件夹，默认当前目录")
    parser.add_argument(
        "-e",
        "--ext",
        action="append",
        default=[],
        metavar="EXT",
        help="只看这些扩展名，可重复写，例如 -e docx -e pdf",
    )
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    root: Path = args.folder
    if not root.is_dir():
        print(f"错误：{root} 不是文件夹")
        return 1

    exts = {normalize_ext(e) for e in args.ext}
    files = collect_files(root, exts)
    if not files:
        print("没有找到文件")
        return 1

    total = 0
    for path in files:
        stat = path.stat()
        total += stat.st_size
        mtime = datetime.fromtimestamp(stat.st_mtime).strftime("%Y-%m-%d %H:%M:%S")
        print(f"{human_size(stat.st_size):>10}  {mtime}  {path.relative_to(root)}")
    print(f"\n共 {len(files)} 个文件，合计 {human_size(total)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
