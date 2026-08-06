#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""agent-dialog_management — 智能体对话管理器（Claude Code + Codex）

统一索引、搜索、查看、恢复电脑上所有智能体对话。

数据源：
  - Claude Code : ~/.claude/projects/<项目编码>/*.jsonl
  - Codex       : ~/.codex/sessions/<Y>/<M>/<D>/rollout-*.jsonl
                  ~/.codex/archived_sessions/rollout-*.jsonl

用法：
  list                    列出全部对话
  search <关键词>          全文搜索对话内容
  show <id>               查看某对话内容
  export <id> [--out 路径]  导出为 Markdown
  note <id> <文本>         给对话添加/更新备注
  star <id>               收藏/取消收藏（切换）
  notes                   列出所有带备注或收藏的对话
  resume-cmd <id>         输出"恢复该对话"的终端命令
  paths                   显示各数据源路径

通用选项：
  --agent claude|codex    只处理某一类
  --limit N               限制条数
  --json                  输出 JSON
"""

import argparse
import glob
import json
import os
import re
import sys
from datetime import datetime
from pathlib import Path

try:
    sys.stdout.reconfigure(encoding="utf-8")
    sys.stderr.reconfigure(encoding="utf-8")
except Exception:
    pass

HOME = Path.home()
CLAUDE_DIR = HOME / ".claude" / "projects"
CODEX_DIR = HOME / ".codex" / "sessions"
CODEX_ARCHIVE_DIR = HOME / ".codex" / "archived_sessions"
SKILL_DIR = Path(__file__).resolve().parent.parent
STATE_DIR = SKILL_DIR / "state"
NOTES_FILE = STATE_DIR / "notes.json"

MAX_TITLE = 60       # 列表里标题显示宽度
SHOW_MSG_LIMIT = 600  # show 时单条消息默认截断宽度


# ---------------------------------------------------------------------------
# 通用工具
# ---------------------------------------------------------------------------

def now_iso():
    return datetime.now().strftime("%Y-%m-%d %H:%M")


def iso_from_mtime(fp):
    try:
        return datetime.fromtimestamp(fp.stat().st_mtime).strftime("%Y-%m-%d %H:%M")
    except Exception:
        return ""


def decode_claude_project(dirname):
    r"""目录编码无法无损还原（- 同时代表 \ 和 _），仅作 cwd 缺失时的兜底。

    E--CodeProject-ABaCaS -> E:\CodeProject\ABaCaS（尽力）
    """
    m = re.match(r"^([A-Za-z])--(.*)$", dirname)
    if m:
        return m.group(1) + ":\\" + m.group(2).replace("-", "\\")
    return dirname


def text_from_blocks(content):
    """从 user/assistant 的 content（str 或 block 列表）提取纯文本"""
    if isinstance(content, str):
        return content.strip()
    if isinstance(content, list):
        parts = []
        for b in content:
            if not isinstance(b, dict):
                continue
            bt = b.get("type")
            if bt in ("text", "input_text", "output_text"):
                t = b.get("text", "")
                if t:
                    parts.append(t)
        return "\n".join(parts).strip()
    return ""


def load_notes():
    if NOTES_FILE.exists():
        try:
            return json.loads(NOTES_FILE.read_text(encoding="utf-8"))
        except Exception:
            return {}
    return {}


def save_notes(notes):
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    NOTES_FILE.write_text(
        json.dumps(notes, ensure_ascii=False, indent=2), encoding="utf-8"
    )


# ---------------------------------------------------------------------------
# 扫描
# ---------------------------------------------------------------------------

def scan_claude():
    records = []
    for f in glob.glob(str(CLAUDE_DIR / "**" / "*.jsonl"), recursive=True):
        fp = Path(f)
        if not fp.is_file():
            continue
        sid = fp.stem
        ai_title = None
        cwd = None
        ts = None
        first_user_text = None
        try:
            with open(fp, encoding="utf-8", errors="replace") as fh:
                for line in fh:
                    try:
                        d = json.loads(line)
                    except Exception:
                        continue
                    t = d.get("type")
                    if t == "ai-title":
                        if not ai_title:
                            ai_title = d.get("aiTitle") or None
                    elif t == "custom-title":
                        ai_title = d.get("title") or ai_title
                    elif t == "user":
                        if cwd is None:
                            cwd = d.get("cwd")
                        if ts is None:
                            ts = d.get("timestamp")
                        if first_user_text is None:
                            m = d.get("message")
                            if isinstance(m, dict):
                                first_user_text = text_from_blocks(m.get("content"))
        except Exception:
            pass
        title = ai_title or first_user_text or "(无标题)"
        # 项目名优先用记录里的真实 cwd；cwd 缺失才回退到目录名编码还原
        project = cwd or decode_claude_project(fp.parent.name)
        records.append(
            {
                "id": sid,
                "agent": "claude",
                "cwd": cwd or "",
                "project": project,
                "title": title,
                "time": _fmt_ts(ts) or iso_from_mtime(fp),
                "archived": False,
                "_file": str(fp),
            }
        )
    return records


def scan_codex():
    records = []
    paths = list(glob.glob(str(CODEX_DIR / "**" / "*.jsonl"), recursive=True))
    paths += list(glob.glob(str(CODEX_ARCHIVE_DIR / "*.jsonl")))
    for f in paths:
        fp = Path(f)
        if not fp.is_file():
            continue
        meta = None
        try:
            with open(fp, encoding="utf-8", errors="replace") as fh:
                for line in fh:
                    try:
                        d = json.loads(line)
                    except Exception:
                        continue
                    if d.get("type") == "session_meta":
                        meta = d.get("payload") or {}
                        break
                    if d.get("type") == "response_item":
                        break  # 无 meta，放弃该文件
        except Exception:
            continue
        if not meta:
            continue
        sid = meta.get("id") or meta.get("session_id") or fp.stem
        title = first_user_text_codex(fp)
        records.append(
            {
                "id": sid,
                "agent": "codex",
                "cwd": meta.get("cwd") or "",
                "project": meta.get("cwd") or "",
                "title": title or "(无标题)",
                "time": _fmt_ts(meta.get("timestamp")) or iso_from_mtime(fp),
                "archived": CODEX_ARCHIVE_DIR in fp.parents,
                "originator": meta.get("originator") or "",
                "_file": str(fp),
            }
        )
    return records


def first_user_text_codex(fp):
    try:
        with open(fp, encoding="utf-8", errors="replace") as fh:
            for line in fh:
                try:
                    d = json.loads(line)
                except Exception:
                    continue
                if d.get("type") == "response_item":
                    p = d.get("payload") or {}
                    if p.get("type") == "message" and p.get("role") == "user":
                        t = text_from_blocks(p.get("content"))
                        if _is_system_context(t):
                            continue
                        # 超长注入性指令（真实用户第一条通常较短）
                        if len(t) > 1500:
                            continue
                        return t
    except Exception:
        pass
    return ""


def _is_system_context(text):
    """跳过 Codex 的开发者注入上下文（app-context / permissions / AGENTS.md 等）"""
    if not text:
        return True
    low = text.lstrip()
    if low.startswith(("<app-context>", "<permissions", "<collaboration_mode>", "# AGENTS.md")):
        return True
    if "AGENTS.md instructions" in text[:200]:
        return True
    return False


def _fmt_ts(ts):
    """ISO 时间戳 -> YYYY-MM-DD HH:MM，失败返回 ''"""
    if not ts:
        return ""
    for fmt in ("%Y-%m-%dT%H:%M:%S.%fZ", "%Y-%m-%dT%H:%M:%SZ", "%Y-%m-%dT%H:%M:%S"):
        try:
            return datetime.strptime(ts, fmt).strftime("%Y-%m-%d %H:%M")
        except Exception:
            continue
    return ""


def scan_all(agent=None):
    records = []
    if agent in (None, "claude"):
        records += scan_claude()
    if agent in (None, "codex"):
        records += scan_codex()
    # 注入备注/收藏状态
    notes = load_notes()
    for r in records:
        n = notes.get(r["id"], {})
        r["note"] = n.get("note", "")
        r["starred"] = n.get("starred", False)
    records.sort(key=lambda r: r["time"], reverse=True)
    return records


def find_record(records, rid):
    rid = rid.strip()
    # 先精确，后前缀（短 ID）
    for r in records:
        if r["id"] == rid:
            return r
    for r in records:
        if r["id"].startswith(rid):
            return r
    return None


# ---------------------------------------------------------------------------
# 消息读取
# ---------------------------------------------------------------------------

def load_messages(rec):
    """返回 [(ts, role, text)]，按时间排序。role: user/assistant"""
    fp = Path(rec.get("_file", ""))
    if not fp.is_file():
        return []
    if rec["agent"] == "claude":
        msgs = _load_claude_messages(fp)
    else:
        msgs = _load_codex_messages(fp)
    msgs.sort(key=lambda x: (x[0], x[3]))
    return [(t, role, text) for t, role, text, _ in msgs]


def _load_claude_messages(fp):
    msgs = []
    with open(fp, encoding="utf-8", errors="replace") as fh:
        for i, line in enumerate(fh):
            try:
                d = json.loads(line)
            except Exception:
                continue
            t = d.get("type")
            if t not in ("user", "assistant"):
                continue
            m = d.get("message") or {}
            role = m.get("role") if isinstance(m, dict) else t
            if role not in ("user", "assistant"):
                continue
            text = text_from_blocks(m.get("content")) if isinstance(m, dict) else ""
            msgs.append((d.get("timestamp", ""), role, text, i))
    return msgs


def _load_codex_messages(fp):
    msgs = []
    with open(fp, encoding="utf-8", errors="replace") as fh:
        for i, line in enumerate(fh):
            try:
                d = json.loads(line)
            except Exception:
                continue
            if d.get("type") != "response_item":
                continue
            p = d.get("payload") or {}
            if p.get("type") != "message":
                continue
            role = p.get("role")
            if role not in ("user", "assistant"):
                continue
            text = text_from_blocks(p.get("content"))
            if role == "user" and _is_system_context(text):
                continue
            msgs.append((d.get("timestamp", ""), role, text, i))
    return msgs


# ---------------------------------------------------------------------------
# 搜索
# ---------------------------------------------------------------------------

def search_all(records, pattern):
    try:
        rx = re.compile(pattern, re.IGNORECASE)
    except re.error as e:
        print(f"无效的正则表达式: {e}", file=sys.stderr)
        sys.exit(2)
    hits = []
    for rec in records:
        if rx.search(rec["title"]):
            hits.append((rec, "标题: " + rec["title"][:120]))
            continue
        msgs = load_messages(rec)
        full = "\n".join(text for _, _, text in msgs)
        m = rx.search(full)
        if m:
            start = max(0, m.start() - 60)
            end = min(len(full), m.end() + 60)
            snippet = full[start:end].replace("\n", " ")
            hits.append((rec, snippet))
    return hits


# ---------------------------------------------------------------------------
# 输出
# ---------------------------------------------------------------------------

def fmt_line(rec):
    src = "C" if rec["agent"] == "claude" else "X"
    flag = " [归档]" if rec.get("archived") else ""
    star = " ⭐" if rec.get("starred") else ""
    title = _truncate(rec["title"].replace("\n", " "), MAX_TITLE)
    return f"[{src}] {rec['id'][:8]} {rec['time']} {rec['project']}{flag}{star}\n     {title}"


def _truncate(s, n):
    return s if len(s) <= n else s[: n - 1] + "…"


def print_records(records, limit=None):
    if limit:
        records = records[:limit]
    if not records:
        print("（没有匹配的对话）")
        return
    for rec in records:
        print(fmt_line(rec))


def print_json(obj):
    print(json.dumps(obj, ensure_ascii=False, indent=2))


# ---------------------------------------------------------------------------
# show / export
# ---------------------------------------------------------------------------

def show_records(rec, full=False):
    msgs = load_messages(rec)
    print(f"来源: {rec['agent']}  会话ID: {rec['id']}")
    print(f"项目: {rec['project']}")
    print(f"时间: {rec['time']}  归档: {'是' if rec.get('archived') else '否'}")
    if rec.get("note"):
        print(f"备注: {rec['note']}")
    print("=" * 70)
    if not msgs:
        print("（无对话内容）")
        return
    for ts, role, text in msgs:
        stamp = (ts or "")[11:19]
        head = "👤 用户" if role == "user" else "🤖 助手"
        print(f"\n--- {head} {stamp} ---")
        if not text:
            print("（无文本，可能是纯工具调用）")
            continue
        body = text if full else _truncate(text, SHOW_MSG_LIMIT)
        print(body)
        if not full and len(text) > SHOW_MSG_LIMIT:
            print(f"… （已截断，共 {len(text)} 字符，用 --full 查看完整）")


def export_markdown(rec, out_path):
    msgs = load_messages(rec)
    lines = []
    lines.append(f"# 对话 {rec['id'][:8]}")
    lines.append("")
    lines.append(f"- **来源**: {rec['agent']}")
    lines.append(f"- **会话 ID**: `{rec['id']}`")
    lines.append(f"- **项目**: `{rec['project']}`")
    lines.append(f"- **时间**: {rec['time']}")
    if rec.get("note"):
        lines.append(f"- **备注**: {rec['note']}")
    lines.append("")
    lines.append("---")
    lines.append("")
    for ts, role, text in msgs:
        if not text:
            continue
        stamp = (ts or "")[11:19]
        if role == "user":
            lines.append(f"> **用户** {stamp}")
        else:
            lines.append(f"**助手** {stamp}")
        lines.append("")
        lines.append(text)
        lines.append("")
    out_path = Path(out_path)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text("\n".join(lines), encoding="utf-8")
    print(f"已导出 {len(msgs)} 条消息 -> {out_path}")
    return out_path


def resume_command(rec):
    """输出 Git Bash 兼容命令：先 cd 到会话原目录，再开新 cmd 窗口恢复。

    已在 Windows/Git Bash 实测可用：`cmd //c "start cmd /k ..."`（不加空标题
    `""`，否则 start 参数解析出错导致拒绝访问）。cd 放在 bash 层，新 cmd 窗口
    继承该 cwd，避免 cmd 嵌套引号冲突。
    """
    target = f"claude --resume {rec['id']}" if rec["agent"] == "claude" \
        else f"codex resume {rec['id']}"
    cwd = rec.get("cwd") or "."
    return f'cd "{cwd}" && cmd //c "start cmd /k {target}"'


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def main():
    ap = argparse.ArgumentParser(prog="agent_dialog", description="智能体对话管理器")
    sub = ap.add_subparsers(dest="cmd", required=True)

    p = sub.add_parser("list", help="列出全部对话")
    p.add_argument("--agent", choices=["claude", "codex"])
    p.add_argument("--limit", type=int)
    p.add_argument("--json", action="store_true")
    p.add_argument("--archived", action="store_true", help="包含已归档对话（默认已含）")

    p = sub.add_parser("search", help="全文搜索")
    p.add_argument("pattern")
    p.add_argument("--limit", type=int)
    p.add_argument("--json", action="store_true")

    p = sub.add_parser("show", help="查看对话")
    p.add_argument("id")
    p.add_argument("--full", action="store_true")

    p = sub.add_parser("export", help="导出 Markdown")
    p.add_argument("id")
    p.add_argument("--out", default=None, help="输出路径")

    p = sub.add_parser("note", help="设置/更新备注")
    p.add_argument("id")
    p.add_argument("text", nargs="+")

    p = sub.add_parser("star", help="收藏/取消收藏")
    p.add_argument("id")

    p = sub.add_parser("notes", help="列出带备注/收藏的对话")
    p.add_argument("--json", action="store_true")

    p = sub.add_parser("resume-cmd", help="输出恢复命令")
    p.add_argument("id")

    p = sub.add_parser("paths", help="显示数据源路径")

    args = ap.parse_args()
    records = scan_all(getattr(args, "agent", None))

    if args.cmd == "paths":
        print("Claude Code 对话: ", CLAUDE_DIR)
        print("Codex 对话:       ", CODEX_DIR)
        print("Codex 归档:       ", CODEX_ARCHIVE_DIR)
        print("备注存储:         ", NOTES_FILE)
        print(f"当前共索引 {len(records)} 个对话")
        return

    if args.cmd == "list":
        if args.json:
            print_json(records)
        else:
            print_records(records, args.limit)
        return

    if args.cmd == "search":
        hits = search_all(records, args.pattern)
        if args.json:
            print_json([{"id": r["id"], "agent": r["agent"], "title": r["title"],
                         "snippet": s} for r, s in hits])
        else:
            print(f"匹配 {len(hits)} 个对话（关键词: {args.pattern}）\n")
            for rec, snippet in hits[: args.limit] if args.limit else hits:
                print(fmt_line(rec))
                print(f"    ↳ {snippet[:140]}")
                print()
        return

    if args.cmd == "notes":
        noted = [r for r in records if r.get("note") or r.get("starred")]
        if args.json:
            print_json([{"id": r["id"], "agent": r["agent"], "title": r["title"],
                         "note": r.get("note", ""), "starred": r.get("starred")} for r in noted])
        elif not noted:
            print("（还没有添加任何备注或收藏）")
        else:
            for r in noted:
                print(fmt_line(r))
                if r.get("note"):
                    print(f"    📝 {r['note']}")
        return

    # 需要 id 的命令
    rec = find_record(records, args.id)
    if not rec:
        print(f"找不到会话: {args.id}", file=sys.stderr)
        print("可用 list 查看全部，或用短 ID 前缀匹配", file=sys.stderr)
        sys.exit(1)

    if args.cmd == "show":
        show_records(rec, full=args.full)
    elif args.cmd == "export":
        out = args.out or f"{rec['id'][:8]}_{rec['agent']}.md"
        export_markdown(rec, out)
    elif args.cmd == "note":
        notes = load_notes()
        entry = notes.setdefault(rec["id"], {})
        entry["note"] = " ".join(args.text)
        entry["ts"] = now_iso()
        save_notes(notes)
        print(f"已备注 {rec['id'][:8]}: {entry['note']}")
    elif args.cmd == "star":
        notes = load_notes()
        entry = notes.setdefault(rec["id"], {})
        entry["starred"] = not entry.get("starred", False)
        entry["ts"] = now_iso()
        save_notes(notes)
        print(f"{'已收藏' if entry['starred'] else '已取消收藏'} {rec['id'][:8]}")
    elif args.cmd == "resume-cmd":
        print(resume_command(rec))


if __name__ == "__main__":
    main()
