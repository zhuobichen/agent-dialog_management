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
import base64
import glob
import json
import os
import re
import subprocess
import sys
import threading
import time
import webbrowser
from datetime import datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

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


def scan_history():
    """扫描 Claude Code 输入历史（~/.claude/history.jsonl，2025-11 至今）。

    每行记录用户的一次输入：display(内容)、project、sessionId、timestamp。
    早期（2025-11 ~ 2026-05）完整对话已被清理，此文件是唯一残留。
    """
    records = []
    fp = HOME / ".claude" / "history.jsonl"
    if not fp.is_file():
        return records
    with open(fp, encoding="utf-8", errors="replace") as fh:
        for line in fh:
            try:
                d = json.loads(line)
            except Exception:
                continue
            display = (d.get("display") or "").strip()
            if not display:
                continue
            records.append({
                "id": d.get("sessionId") or "",
                "agent": "history",
                "cwd": d.get("project") or "",
                "project": d.get("project") or "",
                "title": display[:120],
                "time": _fmt_ts_ms(d.get("timestamp", 0)),
                "archived": False,
                "_file": str(fp),
            })
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


def _fmt_ts_ms(ms):
    """毫秒时间戳 -> YYYY-MM-DD HH:MM，失败返回 ''"""
    if not ms:
        return ""
    try:
        return datetime.fromtimestamp(ms / 1000).strftime("%Y-%m-%d %H:%M")
    except Exception:
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


def count_processes():
    """统计正在运行的 claude.exe / codex.exe / codex-code-mode-host.exe 进程数"""
    ps = (
        'Get-CimInstance Win32_Process | '
        "Where-Object { $_.Name -match 'claude\\.exe|codex' } | "
        "Group-Object Name | Select-Object Name, Count | Format-Table -HideTableHeaders"
    )
    counts = {}
    try:
        out = subprocess.run(
            ["powershell", "-NoProfile", "-Command", ps],
            capture_output=True, text=True, timeout=20,
        ).stdout
        for line in out.splitlines():
            parts = line.split()
            if len(parts) >= 2 and parts[-1].isdigit():
                counts[parts[0]] = int(parts[-1])
    except Exception:
        pass
    return counts


def live_records(records, minutes):
    """最近 minutes 分钟内有写入、且未归档的会话 = 正在活跃运行的对话。

    注意：Codex 后台 app-server 服务可能定期 touch 旧会话文件，导致 mtime
    误报；调用方应结合 codex 交互进程数判断。
    """
    cutoff = time.time() - minutes * 60
    live = []
    for rec in records:
        if rec.get("archived"):
            continue
        fp = Path(rec.get("_file", ""))
        try:
            if fp.is_file() and fp.stat().st_mtime > cutoff:
                live.append(rec)
        except Exception:
            continue
    return live


def codex_interactive_count():
    """统计正在运行的 Codex 交互会话进程（排除 app-server 后台服务）"""
    ps = (
        "Get-CimInstance Win32_Process | "
        "Where-Object { $_.Name -eq 'codex.exe' } | "
        "Where-Object { $_.CommandLine -notmatch 'app-server' } | "
        "Measure-Object | Select-Object -ExpandProperty Count"
    )
    try:
        out = subprocess.run(
            ["powershell", "-NoProfile", "-Command", ps],
            capture_output=True, text=True, timeout=20,
        ).stdout.strip()
        return int(out) if out.isdigit() else 0
    except Exception:
        return 0


def resume_command(rec):
    """输出在当前 Windows Terminal 窗口新标签启动会话的命令（wt -d + EncodedCommand）。

    经验（2026-08-10 实测）：
    - 用 `wt -w 0 new-tab -d <目录>` 在**当前 WT 窗口**开新标签，不弹独立窗口
    - 启动前先 `Remove-Item Env:CLAUDE_CODE_CHILD_SESSION`，否则 claude 继承当前
      会话的"子会话"标记 → 显示 "Transcript saving is off"，不保存对话记录
    - 命令用 `-EncodedCommand`（base64 UTF-16LE）：**分号在 wt -Command 参数里
      会被拆开**导致 claude 不启动，编码后安全
    """
    wt = r'C:\Users\chenlizhuo\AppData\Local\Microsoft\WindowsApps\wt.exe'
    sid = rec["id"]
    cwd = rec.get("cwd") or "."
    if rec["agent"] == "claude":
        inner = f"claude --dangerously-skip-permissions --resume {sid}"
    else:
        inner = f"codex resume {sid}"
    ps = f"Remove-Item Env:CLAUDE_CODE_CHILD_SESSION -ErrorAction SilentlyContinue; {inner}"
    b64 = base64.b64encode(ps.encode("utf-16-le")).decode()
    return f'"{wt}" -w 0 new-tab -d "{cwd}" powershell -NoExit -EncodedCommand {b64}'


# ---------------------------------------------------------------------------
# 监控面板（serve）：HTTP 服务 + 指令发送
# ---------------------------------------------------------------------------

SEND_TASKS = {}          # session_id -> {status, sent_at, message}
SEND_LOCK = threading.Lock()


def _run_send_task(rec, message):
    """后台线程：向会话非交互发送指令（claude --resume -p / codex exec resume）"""
    sid = rec["id"]
    cwd = rec.get("cwd") or "."
    if rec["agent"] == "claude":
        cmd = ["cmd", "/c", "claude", "--resume", sid, "-p", message]
    else:
        cmd = ["cmd", "/c", "codex", "exec", "resume", sid, message]
    try:
        proc = subprocess.run(cmd, cwd=cwd, capture_output=True, text=True,
                              encoding="utf-8", errors="replace", timeout=180)
        ok = proc.returncode == 0
    except Exception:
        ok = False
    with SEND_LOCK:
        SEND_TASKS[sid] = {"status": "done" if ok else "error",
                           "finished_at": now_iso()}


def send_to_session(rec, message):
    """启动后台线程发送指令；同会话已有任务在处理则拒绝"""
    sid = rec["id"]
    with SEND_LOCK:
        t = SEND_TASKS.get(sid)
        if t and t.get("status") == "running":
            return False, "该会话已有指令在处理中"
        SEND_TASKS[sid] = {"status": "running", "sent_at": now_iso(),
                           "message": message[:120]}
    threading.Thread(target=_run_send_task, args=(rec, message), daemon=True).start()
    return True, ""


_STATUS_CACHE = {"ts": 0.0, "data": None}
_STATUS_LOCK = threading.Lock()
_PROCS_CACHE = {"ts": 0.0, "data": None}
_SCAN_CACHE = {"ts": 0.0, "data": None}


def _cached(fn, cache, ttl):
    """带 TTL 的组件缓存：进程统计(15s)、会话扫描(10s)"""
    now = time.time()
    with _STATUS_LOCK:
        if cache["data"] is not None and now - cache["ts"] < ttl:
            return cache["data"]
    data = fn()
    with _STATUS_LOCK:
        cache["ts"] = time.time()
        cache["data"] = data
    return data


def _cached_procs():
    return _cached(lambda: (count_processes(), codex_interactive_count()),
                   _PROCS_CACHE, 15)


def _cached_scan():
    return _cached(scan_all, _SCAN_CACHE, 10)


_CLAUDE_PIDS_CACHE = {"ts": 0.0, "data": None}
_RUNNING_CLAUDE_CACHE = {"ts": 0.0, "data": None}


def _claude_pids():
    """当前运行的 claude.exe 进程 PID 集合"""
    ps = ("Get-CimInstance Win32_Process | "
          "Where-Object { $_.Name -eq 'claude.exe' } | "
          "ForEach-Object { $_.ProcessId }")
    try:
        out = subprocess.run(["powershell", "-NoProfile", "-Command", ps],
                             capture_output=True, text=True, timeout=20).stdout
        return set(int(x) for x in out.split() if x.strip().isdigit())
    except Exception:
        return set()


def _cached_claude_pids():
    return _cached(_claude_pids, _CLAUDE_PIDS_CACHE, 10)


def running_claude_sessions():
    """运行中的 Claude 会话：~/.claude/sessions/<pid>.json ∩ 当前 claude.exe 进程。

    Claude Code 每个运行中会话在 ~/.claude/sessions/ 有一个 <pid>.json，
    记录 sessionId/status（idle/running），据此能列出所有开启中的 Claude 终端。
    """
    pids = _cached_claude_pids()
    if not pids:
        return []
    sdir = HOME / ".claude" / "sessions"
    result = []
    if sdir.is_dir():
        for f in sdir.glob("*.json"):
            try:
                d = json.loads(f.read_text(encoding="utf-8"))
                if d.get("pid") in pids and d.get("sessionId"):
                    result.append({
                        "sessionId": d["sessionId"],
                        "cwd": d.get("cwd", ""),
                        "status": d.get("status", "idle"),
                        "updatedAt": d.get("updatedAt", 0),
                    })
            except Exception:
                continue
    return result


def _cached_running_claude():
    return _cached(running_claude_sessions, _RUNNING_CLAUDE_CACHE, 10)


def shutdown_claude(force=False):
    """关闭所有运行中的 Claude 会话（更新 claude 后想全部重启时用），
    并输出恢复清单（新版本 claude --resume 逐个重开）。"""
    sessions = running_claude_sessions()
    if not sessions:
        print("当前没有运行中的 Claude 会话。")
        return
    print(f"检测到 {len(sessions)} 个运行中的 Claude 会话:")
    for i, s in enumerate(sessions, 1):
        print(f"  {i}. {s['sessionId'][:8]}  cwd={s['cwd']}")
    if not force:
        ans = input("\n确认关闭所有 claude 进程？(y/N): ").strip().lower()
        if ans not in ("y", "yes"):
            print("已取消")
            return
    try:
        subprocess.run(["taskkill", "/F", "/IM", "claude.exe"],
                       capture_output=True, timeout=30)
    except Exception as e:
        print(f"关闭进程时出错: {e}")
    print("\n已关闭所有 Claude 会话。\n")
    print("更新后可用以下命令逐个重新打开（新版本 resume，会话内容不丢）:")
    for i, s in enumerate(sessions, 1):
        print(f"  {i}. cd /d \"{s['cwd']}\" && claude --resume {s['sessionId']}")


def tail_messages(rec, n=6):
    """从 JSONL 末尾反向读取最近 n 条 user/assistant 文本（比 load_messages 快，
    只读文件末尾一小段，适合监控面板高频轮询）"""
    fp = Path(rec.get("_file", ""))
    if not fp.is_file():
        return []
    result = []
    try:
        size = fp.stat().st_size
        read_size = min(size, 200 * 1024)   # 最多读末尾 200KB
        with open(fp, "rb") as f:
            f.seek(size - read_size)
            data = f.read(read_size).decode("utf-8", errors="replace")
        for line in reversed(data.splitlines()):
            if not line.strip():
                continue
            try:
                d = json.loads(line)
            except Exception:
                continue
            t = d.get("type")
            if t in ("user", "assistant"):
                m = d.get("message")
                text = text_from_blocks(m.get("content")) if isinstance(m, dict) else ""
                if text:
                    result.append((d.get("timestamp", ""), t, text))
            elif t == "response_item":      # Codex
                p = d.get("payload") or {}
                if p.get("type") == "message" and p.get("role") in ("user", "assistant"):
                    text = text_from_blocks(p.get("content"))
                    if text:
                        result.append((d.get("timestamp", ""), p["role"], text))
            if len(result) >= n:
                break
        result.reverse()
    except Exception:
        pass
    return result


def status_payload():
    """构建 /api/status 数据：活跃会话 + 进程 + 最近消息 + 任务状态（3s TTL 缓存）"""
    with _STATUS_LOCK:
        if _STATUS_CACHE["data"] is not None and time.time() - _STATUS_CACHE["ts"] < 3.0:
            return _STATUS_CACHE["data"]
    data = _build_status()
    with _STATUS_LOCK:
        _STATUS_CACHE["ts"] = time.time()
        _STATUS_CACHE["data"] = data
    return data


def _build_status():
    records = _cached_scan()
    procs, codex_inter = _cached_procs()
    rec_by_id = {r["id"]: r for r in records}
    seen = set()
    sessions = []

    def add_session(sid, agent, title, project, status):
        if sid in seen:
            return
        seen.add(sid)
        rec = rec_by_id.get(sid)
        last = []
        if rec:
            last = [{"role": r, "text": t[:300]} for _, r, t in tail_messages(rec, 6)]
        with SEND_LOCK:
            task = SEND_TASKS.get(sid, {})
        sessions.append({
            "id": sid, "agent": agent, "title": title, "project": project,
            "time": rec["time"] if rec else "", "status": status,
            "last_messages": last, "task": task,
        })

    # 1) 所有运行中的 Claude 会话（含空闲），按进程会话表
    for rs in _cached_running_claude():
        sid = rs["sessionId"]
        rec = rec_by_id.get(sid)
        add_session(sid, "claude",
                    rec["title"] if rec else sid[:8],
                    rec["project"] if rec else rs["cwd"],
                    rs.get("status", "idle"))

    # 2) 最近活跃但未在上面的会话（含 Codex 与遗漏的 Claude）
    for rec in live_records(records, minutes=5):
        add_session(rec["id"], rec["agent"], rec["title"], rec["project"], "recent")

    return {
        "processes": procs,
        "codex_interactive": codex_inter,
        "sessions": sessions,
        "now": now_iso(),
    }


class MonitorHandler(BaseHTTPRequestHandler):
    def _send(self, code, body, ctype):
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _send_json(self, obj):
        self._send(200, json.dumps(obj, ensure_ascii=False).encode("utf-8"),
                   "application/json; charset=utf-8")

    def do_GET(self):
        path = urlparse(self.path).path
        if path == "/api/status":
            self._send_json(status_payload())
        elif path == "/":
            self._send(200, INDEX_HTML.encode("utf-8"), "text/html; charset=utf-8")
        else:
            self.send_error(404)

    def do_POST(self):
        path = urlparse(self.path).path
        if path == "/api/send":
            try:
                length = int(self.headers.get("Content-Length", 0))
                body = json.loads(self.rfile.read(length).decode("utf-8"))
            except Exception:
                self._send_json({"ok": False, "error": "请求解析失败"})
                return
            sid = (body.get("session_id") or "").strip()
            msg = (body.get("message") or "").strip()
            if not sid or not msg:
                self._send_json({"ok": False, "error": "缺少 session_id 或 message"})
                return
            rec = find_record(scan_all(), sid)
            if not rec:
                self._send_json({"ok": False, "error": "会话不存在"})
                return
            ok, err = send_to_session(rec, msg)
            self._send_json({"ok": ok, "error": err, "session_id": rec["id"]})
        else:
            self.send_error(404)

    def log_message(self, fmt, *args):
        pass  # 静默，避免刷屏


def serve(port=8765, open_browser=True):
    """启动监控面板 HTTP 服务"""
    try:
        server = ThreadingHTTPServer(("127.0.0.1", port), MonitorHandler)
    except OSError as e:
        print(f"端口 {port} 被占用或不可用: {e}\n用 --port 换一个端口")
        sys.exit(1)
    # 同步预热缓存：首次数据构建需几秒（扫描+进程查询），完成后首请求即秒回
    print("正在构建初始数据（首次约几秒）...")
    try:
        _build_status()
    except Exception as e:
        print(f"预热数据构建失败（不影响服务）: {e}")
    print(f"AI 会话监控面板已启动: http://localhost:{port}  (Ctrl+C 停止)")
    if open_browser:
        webbrowser.open(f"http://localhost:{port}")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\n已停止监控面板")


INDEX_HTML = r"""<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>AI 会话监控面板</title>
<style>
  :root{--bg:#0f172a;--card:#1a2438;--line:#2c3a52;--txt:#e2e8f0;--muted:#94a3b8}
  *{box-sizing:border-box;margin:0;padding:0}
  body{background:linear-gradient(160deg,#0b1222,#101b33);color:var(--txt);
       font-family:"Segoe UI","Microsoft YaHei",sans-serif;padding:24px;min-height:100vh}
  .top{max-width:1400px;margin:0 auto 20px}
  h1{font-size:1.5rem;background:linear-gradient(90deg,#38bdf8,#a78bfa);-webkit-background-clip:text;background-clip:text;color:transparent}
  .stats{display:flex;gap:18px;margin-top:10px;font-size:.9rem;color:var(--muted);flex-wrap:wrap}
  .stats b{color:#fff}
  .grid{max-width:1400px;margin:0 auto;display:grid;grid-template-columns:repeat(auto-fill,minmax(380px,1fr));gap:16px}
  .card{background:var(--card);border:1px solid var(--line);border-radius:14px;padding:14px;display:flex;flex-direction:column;gap:8px}
  .chead{display:flex;align-items:center;gap:8px}
  .ctitle{font-weight:600;font-size:.95rem;flex:1;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
  .ag{font-size:.68rem;padding:2px 8px;border-radius:4px;font-weight:600}
  .ag.c{background:rgba(56,189,248,.15);color:#38bdf8}
  .ag.x{background:rgba(167,139,250,.15);color:#a78bfa}
  .dot{width:9px;height:9px;border-radius:50%;background:#34d399;flex-shrink:0}
  .st{font-size:.7rem;color:var(--muted);border:1px solid var(--line);border-radius:4px;padding:0 6px;white-space:nowrap}
  .st.idle{color:#94a3b8}
  .st.live{color:#34d399}
  .cmeta{font-size:.75rem;color:var(--muted)}
  .cmsgs{background:#0d1626;border-radius:10px;padding:8px;max-height:220px;overflow-y:auto;display:flex;flex-direction:column;gap:6px}
  .msg{font-size:.82rem;line-height:1.5;display:flex;gap:6px;align-items:flex-start}
  .msg .who{flex-shrink:0}
  .msg.user{color:#7dd3fc}
  .msg.assistant{color:#cbd5e1}
  .task-run{color:#fbbf24;font-size:.8rem}
  .task-err{color:#f87171;font-size:.8rem}
  .cinput{display:flex;gap:8px}
  .cinput input{flex:1;background:#0d1626;border:1px solid var(--line);border-radius:8px;color:var(--txt);
       padding:8px 10px;font-size:.85rem;outline:none}
  .cinput input:focus{border-color:#38bdf8}
  .cinput button{background:linear-gradient(90deg,#0e7490,#1d4ed8);border:none;border-radius:8px;
       color:#fff;padding:8px 14px;font-size:.85rem;cursor:pointer}
  .cinput button:hover{opacity:.9}
  .empty{text-align:center;color:var(--muted);padding:60px;font-size:1rem}
  .hidden{display:none}
  .muted{color:var(--muted)}
</style>
</head>
<body>
<div class="top">
  <h1>AI 会话监控面板</h1>
  <div class="stats">
    <span>Claude 进程: <b id="nClaude">-</b></span>
    <span>Codex 交互: <b id="nCodex">-</b></span>
    <span>活跃会话: <b id="nSess">-</b></span>
    <span id="refreshed">-</span>
  </div>
</div>
<div class="grid" id="grid"></div>
<div id="empty" class="empty hidden">当前无活跃会话</div>
<script>
async function load(){
  try{
    const r = await fetch('/api/status');
    const d = await r.json();
    document.getElementById('nClaude').textContent = d.processes['claude.exe']||0;
    document.getElementById('nCodex').textContent = d.codex_interactive||0;
    document.getElementById('nSess').textContent = d.sessions.length;
    document.getElementById('refreshed').textContent = '刷新: '+d.now;
    render(d.sessions);
  }catch(e){}
}
function esc(s){return (s||'').replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;');}
function render(sessions){
  const grid = document.getElementById('grid');
  document.getElementById('empty').classList.toggle('hidden', sessions.length>0);
  grid.innerHTML='';
  sessions.forEach(s=>{
    const card=document.createElement('div');card.className='card';
    const badge=s.agent==='claude'?'<span class="ag c">Claude</span>':'<span class="ag x">Codex</span>';
    const msgs=(s.last_messages.map(m=>'<div class="msg '+m.role+'"><span class="who">'+(m.role==='user'?'👤':'🤖')+'</span><span>'+esc(m.text)+'</span></div>').join(''))||'<div class="muted">（暂无对话内容）</div>';
    const task=s.task.status==='running'?'<div class="task-run">⏳ AI 处理中…</div>':(s.task.status==='error'?'<div class="task-err">⚠️ 指令执行出错</div>':'');
    const live=(s.status==='running'||s.status==='recent');
    const stTxt=s.status==='running'?'运行中':(s.status==='recent'?'活跃':'空闲');
    const dot='<span class="dot" style="background:'+(live?'#34d399':'#94a3b8')+'"></span>';
    const st='<span class="st '+(live?'live':'idle')+'">'+stTxt+'</span>';
    card.innerHTML='<div class="chead"><div class="ctitle" title="'+esc(s.title)+'">'+esc(s.title)+'</div>'+badge+st+dot+'</div>'+
      '<div class="cmeta">'+esc(s.project)+' · '+s.time+'</div>'+
      '<div class="cmsgs">'+msgs+'</div>'+task+
      '<div class="cinput"><input placeholder="向此会话发指令…" data-id="'+s.id+'"><button data-id="'+s.id+'">发送</button></div>';
    grid.appendChild(card);
  });
  grid.querySelectorAll('button').forEach(b=>b.onclick=()=>send(b.dataset.id,b.previousElementSibling.value));
  grid.querySelectorAll('input').forEach(i=>i.onkeydown=e=>{if(e.key==='Enter')send(i.dataset.id,i.value);});
}
async function send(id,msg){
  if(!msg||!msg.trim())return;
  try{
    await fetch('/api/send',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({session_id:id,message:msg})});
  }catch(e){}
  load();
}
load();
setInterval(load,3000);
</script>
</body>
</html>
"""


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

    p = sub.add_parser("hist", help="搜索早期输入历史（history.jsonl，2025-11至今）")
    p.add_argument("pattern")
    p.add_argument("--limit", type=int, default=30)
    p.add_argument("--json", action="store_true")

    p = sub.add_parser("running", help="查看正在运行的对话（进程 + 最近活跃会话）")
    p.add_argument("--minutes", type=int, default=30, help="活跃判定窗口（分钟，默认30）")
    p.add_argument("--json", action="store_true")

    p = sub.add_parser("serve", help="启动多终端会话监控面板（浏览器）")
    p.add_argument("--port", type=int, default=8765, help="端口（默认8765）")
    p.add_argument("--no-browser", action="store_true", help="不自动打开浏览器")

    p = sub.add_parser("shutdown", help="关闭所有运行中的 Claude 会话并输出恢复清单")
    p.add_argument("--yes", action="store_true", help="跳过确认直接关闭")

    p = sub.add_parser("paths", help="显示数据源路径")

    args = ap.parse_args()
    records = scan_all(getattr(args, "agent", None))

    if args.cmd == "paths":
        print("Claude Code 对话: ", CLAUDE_DIR)
        print("Codex 对话:       ", CODEX_DIR)
        print("Codex 归档:       ", CODEX_ARCHIVE_DIR)
        print("早期输入历史:     ", HOME / ".claude" / "history.jsonl")
        print("备注存储:         ", NOTES_FILE)
        print(f"当前共索引 {len(records)} 个对话")
        return

    if args.cmd == "serve":
        serve(port=args.port, open_browser=not args.no_browser)
        return

    if args.cmd == "shutdown":
        shutdown_claude(force=args.yes)
        return

    if args.cmd == "hist":
        hist = scan_history()
        try:
            rx = re.compile(args.pattern, re.IGNORECASE)
        except re.error as e:
            print(f"无效的正则表达式: {e}", file=sys.stderr)
            sys.exit(2)
        hits = [r for r in hist if rx.search(r["title"])]
        hits.sort(key=lambda r: r["time"], reverse=True)
        if args.json:
            print_json([
                {"time": r["time"], "project": r["project"], "session": r["id"],
                 "text": r["title"]} for r in hits[: args.limit]
            ])
        else:
            print(f"在 history.jsonl 中匹配 {len(hits)} 条输入记录（关键词: {args.pattern}）")
            print("（history.jsonl 覆盖 2025-11 至今；早期无完整对话，仅剩这些输入痕迹）\n")
            for r in hits[: args.limit]:
                sess = f" session={r['id'][:8]}" if r["id"] else ""
                print(f"[{r['time']}] {r['project']}{sess}")
                print(f"    {r['title'][:90]}")
            if len(hits) > args.limit:
                print(f"\n… 还有 {len(hits) - args.limit} 条，用 --limit 增加（当前 {args.limit}）")
        return

    if args.cmd == "running":
        counts = count_processes()
        live = live_records(records, args.minutes)
        n_claude = counts.get("claude.exe", 0)
        n_codex = counts.get("codex.exe", 0)
        n_host = counts.get("codex-code-mode-host.exe", 0)
        n_codex_inter = codex_interactive_count()
        codex_apps_only = n_codex > 0 and n_codex_inter == 0
        if args.json:
            print_json({
                "processes": counts,
                "codex_interactive_sessions": n_codex_inter,
                "live_sessions": [
                    {"id": r["id"], "agent": r["agent"], "title": r["title"],
                     "time": r["time"], "project": r["project"]} for r in live
                ],
                "window_minutes": args.minutes,
            })
            return
        print(f"=== 正在运行的智能体对话 ===")
        print(f"Claude Code: {n_claude} 个 claude.exe 会话窗口")
        if n_codex_inter:
            print(f"Codex: {n_codex_inter} 个交互会话进程 (+ {n_codex} codex.exe / {n_host} host)")
        elif codex_apps_only:
            print(f"Codex: 无交互对话（{n_codex} 个 codex.exe 均为 app-server 后台服务）")
        else:
            print("Codex: 无进程")
        print(f"\n最近 {args.minutes} 分钟内有活动写入的会话（Claude 可靠，Codex 可能受后台服务影响）:")
        for rec in live:
            mark = ""
            if codex_apps_only and rec["agent"] == "codex":
                mark = "  ← 后台服务 touch，非用户对话"
            print(fmt_line(rec) + mark)
        if not live:
            print("（无）")
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
