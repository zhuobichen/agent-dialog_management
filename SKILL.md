---
name: agent-dialog_management
description: >
  管理电脑上所有智能体（Claude Code + Codex）的对话记录：统一列出、全文搜索找回
  遗忘/关闭的对话、查看内容、导出 Markdown、备注收藏、一键恢复（新开终端窗口
  继续之前的会话）、查看正在运行的对话。当用户说"我的对话""找到之前那个对话"
  "恢复某次对话""正在运行的对话""看看智能体聊了什么""导出这次对话"等涉及
  历史/进行中对话的查找/管理/恢复场景时使用。
user-invocable: true
---

# 智能体对话管理（agent-dialog_management）

统一管理本机 **Claude Code** 与 **Codex** 的所有对话。核心价值：对话散落在多个目录，
用户记不清哪个会话做了什么，本 skill 帮用户**找到 → 查看 → 恢复**，并支持
备注/收藏/导出。

## 数据源（只读，不联网）

| 来源 | 位置 | 说明 |
|---|---|---|
| Claude Code | `~/.claude/projects/<项目>/*.jsonl` | 会话 ID = 文件名 |
| Codex | `~/.codex/sessions/<Y>/<M>/<D>/rollout-*.jsonl` | 含 Desktop/CLI 会话 |
| Codex 归档 | `~/.codex/archived_sessions/` | 已归档会话，list 中标记 `[归档]` |
| 早期输入历史 | `~/.claude/history.jsonl` | **2025-11 至今**的全部输入记录（早期完整对话已清理，仅剩此处痕迹） |
| 备注/收藏 | `~/.claude/skills/agent-dialog_management/state/notes.json` | skill 自身维护 |

每个会话记录：ID、来源（C=Claude / X=Codex）、真实工作目录、时间、标题（自动取
AI 标题或第一条用户消息）、归档状态。

## 核心命令

脚本路径（在 Bash 中执行，`~` 会自动展开）：

```bash
# 先定义（建议每次执行前定义，避免拼错）
DIALOG="$HOME/.claude/skills/agent-dialog_management/scripts/agent_dialog.py"
# 或直接写绝对路径：
#   C:\Users\chenlizhuo\.claude\skills\agent-dialog_management\scripts\agent_dialog.py
```

| 用户意图 | 执行命令 |
|---|---|
| 列出全部对话 | `python <脚本> list [--agent claude\|codex] [--limit 20]` |
| 搜索找回 | `python <脚本> search <关键词>` |
| 查看内容 | 先 `search`/`list` 拿到短 ID → `python <脚本> show <短ID> [--full]` |
| 恢复对话 | `python <脚本> resume-cmd <短ID>` → 执行输出的命令 |
| 导出 | `python <脚本> export <短ID> --out <路径.md>` |
| 备注 | `python <脚本> note <短ID> <文本>` |
| 收藏/取消 | `python <脚本> star <短ID>` |
| 列出有备注的 | `python <脚本> notes` |
| 查看正在运行的对话 | `python <脚本> running [--minutes 30]` |
| 搜索早期输入历史 | `python <脚本> hist <关键词> [--limit 30]` |
| 启动监控面板 | `python <脚本> serve [--port 8765] [--no-browser]` |
| 关闭所有 Claude 会话 | `python <脚本> shutdown [--yes]`（更新 claude 后重启用，输出恢复清单） |
| 查看数据源 | `python <脚本> paths` |

**ID 支持前缀匹配**：`show 79b26d95` 或完整 UUID 均可。

## 工作流

### 1. 列出/概览
```bash
python <脚本> list --limit 20
```
输出格式：`[C] 短ID 时间 项目 ⭐[归档]` + 缩进标题。Claude Code 列表、Codex 列表
混排按时间倒序。

### 2. 找回"遗忘"的对话
用户描述模糊时先 `search` 关键词：
```bash
python <脚本> search 邮箱
```
返回命中对话 + 上下文片段。用户确认后 `show` 看全文。

### 3. 恢复对话（在当前 Windows Terminal 新标签）
```bash
python <脚本> resume-cmd <短ID>
```
脚本会输出一条命令（基于 `wt.exe` + `-EncodedCommand`）：
`"C:\Users\<用户>\AppData\Local\Microsoft\WindowsApps\wt.exe" -w 0 new-tab -d "<cwd>" powershell -NoExit -EncodedCommand <base64>`
编码内容 = `Remove-Item Env:CLAUDE_CODE_CHILD_SESSION; claude --dangerously-skip-permissions --resume <id>`
**执行这条输出**，会在**当前 Windows Terminal 窗口**新开标签页恢复该会话（不弹独立窗口）。
codex 会话用 `codex resume <id>`。

> 经验（2026-08-10）：
> - 用 `wt -w 0 new-tab -d <目录>` 开当前窗口新标签；**不要**用 `Start-Process powershell`（弹独立系统窗口）
> - 启动前必须 `Remove-Item Env:CLAUDE_CODE_CHILD_SESSION`——否则 claude 继承当前会话的
>   "子会话"标记，显示 "Transcript saving is off"，**不保存对话记录**
> - 命令用 `-EncodedCommand`（base64）：**分号在 wt `-Command` 参数里会被拆开**导致 claude 不启动
> - resume 是交互式命令，不能在当前 Claude Code 会话内直接运行

### 3.4 监控面板（多终端同时看）
```bash
python <脚本> serve          # 启动浏览器面板 http://localhost:8765
```
一个页面同时展示所有活跃会话（Claude + Codex）：标题/项目/最近对话（3 秒自动刷新）、
进程统计。**可向任一会话直接发指令**（卡片底部输入框）——内部用 `claude --resume <id> -p`
或 `codex exec resume <id>` 非交互恢复执行，回复自动写回会话，面板刷新可见。
指令处理较慢（取决于模型），面板会显示"AI 处理中…"。端口占用时用 `--port` 换。

### 3.5 搜索早期输入历史（history）
2025-11 ~ 2026-05 的完整对话已被清理，但 `history.jsonl` 保留了你当时**每一次输入**（命令/提问/粘贴）。
用户说"我早期做过 XX"时：
```bash
python <脚本> hist downscaler      # 找回 207 条提到 Downscaler 的历史输入
python <脚本> hist 惠州             # 找回惠州项目相关输入（含 2025-12 的记录）
```
每条结果带：时间 / 项目 / sessionId / 输入内容。若 sessionId 恰好对应 6 月后的完整对话，
可再用 `show`/`resume` 深入。

### 4. 备注 / 收藏
给重要对话加备注便于日后找回：
```bash
python <脚本> note 79b26d95 "简历项目整理，已完成初稿"
python <脚本> star 79b26d95        # 切换收藏
python <脚本> notes                # 列出所有带备注/收藏的
```

### 5. 导出 Markdown
```bash
python <脚本> export 79b26d95 --out "E:/CodeProject/notes/对话.md"
```

## 边界与约定

- **只读用户数据**：扫描/读取 JSONL 从不修改；唯一写入的是本 skill 自己的 `state/notes.json`。
- **Codex 会话管理**（归档/删除）用官方命令：`codex archive <id>` / `codex delete <id>`，
  skill 不做绕过。
- **标题兜底**：无 AI 标题时取第一条用户消息，无消息则显示 `(无标题)`。
- **running 判定**：活跃 = 最近 N 分钟有写入且未归档。Codex 后台 app-server 会 touch
  旧会话文件造成误报，因此当 Codex 无交互进程时，其"活跃"记录会标注为后台服务活动。
- **性能**：99 个对话全文搜索约 1~3 秒；list 秒回。
- 本 skill 用本地 git 管理（`~/.claude/skills/agent-dialog_management/` 内 `git init`），
  只 commit 不 push。
