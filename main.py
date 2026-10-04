r"""扫描、列出、批量整理作业文件的小脚本。

用法示例：
    python main.py .                                  # 列出当前文件夹的文件
    python main.py D:/homework -e docx -e pdf         # 只看 docx / pdf
    python main.py rename D:/homework                 # 预览改名，确认后才真的改
    python main.py rename D:/homework --rule '^(?P<sid>\d+)_(?P<name>.+)$=>\g<name>_\g<sid>'
    python main.py archive D:/homework --by term      # 按学期归到子文件夹并生成报告
    python main.py undo D:/homework                   # 撤销上次归档
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
from datetime import datetime
from pathlib import Path

_UNITS = ("B", "KB", "MB", "GB", "TB")
COMMANDS = ("list", "rename", "archive", "undo")
GROUPS = ("term", "category")
JOURNAL_NAME = ".pdd03_last_archive.json"
REPORT_PREFIX = "整理报告-"

# 默认改名规则：「学号_姓名_作业名」-> 「作业名_学号」；作业名里可以再带下划线
DEFAULT_RULES = [(r"(?P<id>[^_]+)_(?P<name>[^_]+)_(?P<homework>.+)", r"\g<homework>_\g<id>")]

Rule = tuple[re.Pattern[str], str]  # (整段匹配的正则, 替换模板)


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
    """递归收集 root 下的文件，exts 非空时只保留扩展名匹配的；日志和报告不算。"""
    files = [
        p
        for p in root.rglob("*")
        if p.is_file() and not p.name.startswith(".") and not p.name.startswith(REPORT_PREFIX)
    ]
    if exts:
        files = [p for p in files if p.suffix.lower() in exts]
    return sorted(files)


def file_key(path: Path) -> str:
    """判断两个路径是否指向同一个文件用的键（Windows 上大小写不敏感）。"""
    return os.path.normcase(str(path))


def display(path: Path, root: Path) -> str:
    """尽量显示相对路径，方便看。"""
    try:
        return str(path.relative_to(root))
    except ValueError:
        return str(path)


def ask_confirm(prompt: str) -> bool:
    """问一句 yes/no，只有明确回答 y/yes 才算确认。"""
    try:
        return input(prompt).strip().lower() in ("y", "yes")
    except EOFError:
        return False


def timestamp() -> str:
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def compile_rule(pattern: str, replace: str) -> Rule:
    """编译一条改名规则，顺便检查替换模板里引用的分组是否真的存在。"""
    try:
        compiled = re.compile(pattern)
    except re.error as exc:
        raise ValueError(f"正则写错了：{pattern}（{exc}）") from exc

    for name in re.findall(r"\\g<([^>]+)>", replace):
        if name not in compiled.groupindex:
            raise ValueError(f"替换模板里的 \\g<{name}> 在正则 {pattern} 里没有对应的分组")
    for number in re.findall(r"\\(\d+)", replace):
        if int(number) > compiled.groups:
            raise ValueError(f"替换模板里的 \\{number} 超出了正则 {pattern} 的分组数量")
    return compiled, replace


def load_rules(cli_rules: list[str], rules_file: Path | None) -> tuple[list[Rule], list[str]]:
    """按「命令行 --rule（在前）> 配置文件 > 内置默认」凑出规则表，返回 (规则, 配置里的扩展名)。"""
    raw: list[tuple[str, str]] = []
    exts: list[str] = []

    if rules_file is not None:
        try:
            config = json.loads(rules_file.read_text(encoding="utf-8"))
        except OSError as exc:
            raise ValueError(f"读不了配置文件 {rules_file}：{exc.strerror}") from exc
        except json.JSONDecodeError as exc:
            raise ValueError(f"配置文件 {rules_file} 不是合法 JSON：{exc}") from exc
        if isinstance(config, list):
            config = {"rules": config}
        if not isinstance(config, dict):
            raise ValueError(f"配置文件 {rules_file} 应该是一个对象，里面有 rules / ext")
        for item in config.get("rules", []):
            if not isinstance(item, dict) or "pattern" not in item or "replace" not in item:
                raise ValueError(f'配置文件里的规则要写成 {{"pattern": "...", "replace": "..."}}：{item!r}')
            raw.append((str(item["pattern"]), str(item["replace"])))
        exts = [normalize_ext(str(e)) for e in config.get("ext", [])]

    for spec in cli_rules:
        if "=>" not in spec:
            raise ValueError(f'规则要写成「正则=>替换模板」的形式，例如 "^(?P<sid>\\d+)_(?P<name>.+)$=>\\g<name>_\\g<sid>"：{spec}')
        pattern, replace = spec.split("=>", 1)
        raw.insert(0, (pattern, replace))  # 命令行规则优先

    if not raw:
        raw = DEFAULT_RULES
    return [compile_rule(pattern, replace) for pattern, replace in raw], exts


def apply_rules(stem: str, rules: list[Rule]) -> tuple[str, int] | None:
    """拿文件名（不含扩展名）依次试规则（正则整段匹配），返回 (新名字, 命中的第几条) 或 None。"""
    for index, (pattern, replace) in enumerate(rules, 1):
        match = pattern.fullmatch(stem)
        if match is not None:
            return match.expand(replace).strip(), index
    return None


def plan_renames(files: list[Path], rules: list[Rule]) -> tuple[list[tuple[Path, Path, str]], list[tuple[Path, str]]]:
    """算出改名计划，返回 (计划, 跳过的文件及原因)；计划每项是 (原路径, 新路径, 备注)。"""
    plans: list[tuple[Path, Path, list[str]]] = []
    skipped: list[tuple[Path, str]] = []
    for path in files:
        result = apply_rules(path.stem, rules)
        if result is None:
            skipped.append((path, "没有规则匹配"))
            continue
        stem, index = result
        if not stem or stem == path.stem:
            skipped.append((path, "新名字和原来一样"))
            continue
        notes = [f"第 {index}/{len(rules)} 条规则"] if len(rules) > 1 else []
        plans.append((path, path.with_name(stem + path.suffix), notes))

    # 重名冲突：不能覆盖已有文件，也不能让两个文件改到同一个名字
    used = {file_key(f) for parent in {p.parent for p, _, _ in plans} for f in parent.iterdir() if f.is_file()}
    used -= {file_key(src) for src, _, _ in plans}  # 反正要改走的文件不占名字
    resolved: list[tuple[Path, Path, str]] = []
    for src, dst, notes in plans:
        candidate, index = dst, 1
        while file_key(candidate) in used:
            candidate = dst.with_name(f"{dst.stem}_{index}{dst.suffix}")
            index += 1
        used.add(file_key(candidate))
        if candidate != dst:
            notes.append("重名，自动加了序号")
        resolved.append((src, candidate, "；".join(notes)))
    return resolved, skipped


def term_of(mtime: float) -> str:
    """按修改时间推算学期：2~7 月算春季学期，8 月到次年 1 月算秋季学期。"""
    moment = datetime.fromtimestamp(mtime)
    if moment.month == 1:
        return f"{moment.year - 1}秋"
    if moment.month >= 8:
        return f"{moment.year}秋"
    return f"{moment.year}春"


def category_of(path: Path) -> str:
    """类别就是扩展名，没有扩展名的归到「其他」。"""
    return path.suffix.lstrip(".").lower() or "其他"


def group_of(path: Path, by: str) -> str:
    return term_of(path.stat().st_mtime) if by == "term" else category_of(path)


def plan_archive(
    files: list[Path], root: Path, by: str
) -> tuple[list[tuple[Path, Path, str]], list[tuple[Path, str]]]:
    """算出归档计划，返回 (计划, 跳过)；计划每项是 (原路径, 新路径, 子文件夹名)。"""
    plans: list[tuple[Path, Path, str]] = []
    skipped: list[tuple[Path, str]] = []
    used: set[str] = set()
    for path in files:
        group = group_of(path, by)
        dst = root / group / path.name
        key = file_key(dst)
        if key == file_key(path):
            skipped.append((path, f"已经在子文件夹「{group}」里"))
        elif key in used or dst.exists():
            skipped.append((path, f"子文件夹「{group}」里已有同名文件"))
        else:
            used.add(key)
            plans.append((path, dst, group))
    return plans, skipped


def write_report(root: Path, lines: list[str]) -> Path:
    """生成整理报告，返回报告文件路径。"""
    path = root / f"{REPORT_PREFIX}{datetime.now():%Y%m%d-%H%M%S}.txt"
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return path


def save_journal(root: Path, by: str, moves: list[tuple[Path, Path]]) -> Path:
    """记下这次归档，供 undo 用。"""
    path = root / JOURNAL_NAME
    data = {
        "time": timestamp(),
        "by": by,
        "moves": [[str(src.relative_to(root)), str(dst.relative_to(root))] for src, dst in moves],
    }
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    return path


def print_skipped(skipped: list[tuple[Path, str]], root: Path) -> None:
    print(f"跳过 {len(skipped)} 个文件：")
    for path, reason in skipped:
        print(f"  {display(path, root)}：{reason}")


def print_plan(plans: list[tuple[Path, Path, str]], skipped: list[tuple[Path, str]], root: Path) -> None:
    """把将要改的名字和跳过的文件打印出来给用户看。"""
    print(f"将要改名 {len(plans)} 个文件：")
    for src, dst, note in plans:
        print(f"  {src.name}  ->  {dst.name}" + (f"  （{note}）" if note else ""))
    if skipped:
        print()
        print_skipped(skipped, root)


def print_archive_plan(plans: list[tuple[Path, Path, str]], skipped: list[tuple[Path, str]], root: Path) -> None:
    print(f"将要移动 {len(plans)} 个文件：")
    for src, dst, _ in plans:
        print(f"  {display(src, root)}  ->  {display(dst, root)}")
    if skipped:
        print()
        print_skipped(skipped, root)


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
    try:
        rules, config_exts = load_rules(args.rule, args.rules)
    except ValueError as exc:
        print(f"错误：{exc}")
        return 2

    exts = {normalize_ext(e) for e in args.ext} or set(config_exts)
    files = collect_files(args.folder, exts)
    if not files:
        print("没有找到文件")
        return 1

    plans, skipped = plan_renames(files, rules)
    if not plans:
        print("没有需要改名的文件")
        if skipped:
            print_skipped(skipped, args.folder)
        return 0

    print_plan(plans, skipped, args.folder)
    if not args.yes and not ask_confirm(f"\n确认执行上面 {len(plans)} 个改名？[y/N] "):
        print("已取消，没有改动任何文件")
        return 0

    for src, dst, _ in plans:
        src.rename(dst)
    print(f"\n已改名 {len(plans)} 个，跳过 {len(skipped)} 个")
    return 0


def run_archive(args: argparse.Namespace) -> int:
    root: Path = args.folder
    files = collect_files(root, {normalize_ext(e) for e in args.ext})
    if not files:
        print("没有找到文件")
        return 1

    plans, skipped = plan_archive(files, root, args.by)
    print_archive_plan(plans, skipped, root)

    counts: dict[str, int] = {}
    for _, _, group in plans:
        counts[group] = counts.get(group, 0) + 1
    way = "按学期" if args.by == "term" else "按类别"
    summary = [f"各子文件夹：{'、'.join(f'{g} {n} 个' for g, n in sorted(counts.items()))}"] if counts else []

    if not plans:
        print("\n没有需要移动的文件")
        return 0

    if not args.yes and not ask_confirm(f"\n确认执行上面 {len(plans)} 个移动？[y/N] "):
        print("已取消，没有改动任何文件")
        return 0

    for src, dst, _ in plans:
        dst.parent.mkdir(parents=True, exist_ok=True)
        src.rename(dst)

    report = write_report(
        root,
        [
            f"整理报告（{timestamp()}）",
            f"文件夹：{root}",
            f"归类方式：{way}",
            "",
            f"移动 {len(plans)} 个文件：",
            *[f"  {display(src, root)}  ->  {display(dst, root)}" for src, dst, _ in plans],
            "",
            f"跳过 {len(skipped)} 个文件：",
            *[f"  {display(path, root)}：{reason}" for path, reason in skipped],
            "",
            *summary,
        ],
    )
    save_journal(root, args.by, [(src, dst) for src, dst, _ in plans])
    print(f"\n已移动 {len(plans)} 个，跳过 {len(skipped)} 个")
    print(f"报告：{report.name}（用 python main.py undo {root} 可以撤销）")
    return 0


def run_undo(args: argparse.Namespace) -> int:
    root: Path = args.folder
    journal = root / JOURNAL_NAME
    if not journal.is_file():
        print(f"没有可撤销的操作（找不到 {JOURNAL_NAME}）")
        return 1

    try:
        data = json.loads(journal.read_text(encoding="utf-8"))
        moves = data["moves"]
    except (OSError, json.JSONDecodeError, KeyError, TypeError) as exc:
        print(f"错误：日志 {JOURNAL_NAME} 读不出来（{exc}）")
        return 2

    plans: list[tuple[Path, Path, str]] = []
    skipped: list[tuple[Path, str]] = []
    for src_rel, dst_rel in moves:
        src, dst = root / src_rel, root / dst_rel  # 现在在 dst，要移回 src
        if not dst.exists():
            skipped.append((dst, "文件不在归档后的位置，可能已经被移动或删除"))
        elif src.exists():
            skipped.append((dst, "原来位置已经有同名文件"))
        else:
            plans.append((dst, src, ""))

    print(f"上次归档（{data.get('time', '时间未知')}）共 {len(moves)} 个文件，现在可以撤销 {len(plans)} 个：")
    for src, dst, _ in plans:
        print(f"  {display(src, root)}  ->  {display(dst, root)}")
    if skipped:
        print()
        print_skipped(skipped, root)

    if not plans:
        print("\n没有可以撤销的文件，日志保留")
        return 0

    if not args.yes and not ask_confirm(f"\n确认把上面 {len(plans)} 个文件移回原位？[y/N] "):
        print("已取消，没有改动任何文件")
        return 0

    for src, dst, _ in plans:
        dst.parent.mkdir(parents=True, exist_ok=True)
        src.rename(dst)
    for folder in {src.parent for src, _, _ in plans}:  # 归档用过的空文件夹顺手删掉
        try:
            folder.rmdir()
        except OSError:
            pass
    journal.unlink()
    print(f"\n已撤销 {len(plans)} 个，跳过 {len(skipped)} 个；日志已删除，不能重复撤销")
    return 0


def add_target_args(parser: argparse.ArgumentParser) -> None:
    """list / rename / archive / undo 共用的参数：文件夹和扩展名过滤。"""
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

    rename_parser = subparsers.add_parser("rename", help="按改名规则批量改名，先预览再确认")
    add_target_args(rename_parser)
    rename_parser.add_argument(
        "--rule",
        action="append",
        default=[],
        metavar="正则=>替换模板",
        help="自定义改名规则，可重复写，写在命令行最左边的先试；正则整段匹配文件名（不含扩展名），"
        "替换模板里用 \\g<分组名> 引用，例如 --rule '(?P<sid>\\d+)_(?P<name>.+)$=>\\g<name>_\\g<sid>'",
    )
    rename_parser.add_argument(
        "--rules",
        type=Path,
        metavar="文件",
        help='从 JSON 配置文件读规则，格式：{"rules": [{"pattern": "...", "replace": "..."}], "ext": ["pdf"]}',
    )
    rename_parser.add_argument("-y", "--yes", action="store_true", help="不询问，直接执行")
    rename_parser.set_defaults(func=run_rename)

    archive_parser = subparsers.add_parser("archive", help="把文件移动到子文件夹并生成整理报告")
    add_target_args(archive_parser)
    archive_parser.add_argument(
        "--by", choices=GROUPS, default="term", help="归类方式：term 按学期（默认，由修改时间推算）、category 按类别（即扩展名）"
    )
    archive_parser.add_argument("-y", "--yes", action="store_true", help="不询问，直接执行")
    archive_parser.set_defaults(func=run_archive)

    undo_parser = subparsers.add_parser("undo", help=f"撤销上次归档（读 {JOURNAL_NAME}）")
    add_target_args(undo_parser)
    undo_parser.add_argument("-y", "--yes", action="store_true", help="不询问，直接执行")
    undo_parser.set_defaults(func=run_undo)

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
