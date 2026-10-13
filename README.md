# 智能体对话管理

> 统一管理本机 Claude Code / Codex 的对话记录：查找、查看、导出与一键恢复会话。

## 功能

- 统一列出所有对话
- 全文搜索找回遗忘 / 关闭的对话
- 查看内容、导出 Markdown
- 备注收藏、一键恢复（新开终端继续会话）
- 查看正在运行的对话
- **会话清单 + 整批重开**：`snapshot` 记住此刻开着的、`restore` 一条命令全开回来
  （应对"误关窗口/关机把会话一起带走"——同窗口多标签会连坐）

## 已打开清单（`state/open.json`）

`resume-cmd` / `restore` / `snapshot` / `shutdown` 都会把会话记入清单（最近在前，最多 30 条）。
误关窗口或重启后：

```bash
python scripts/agent_dialog.py restore              # 重开已关闭的（跳过还活着的）
python scripts/agent_dialog.py restore --window new # 每个会话一扇独立 WT 窗口
python scripts/agent_dialog.py open-list            # 看清单，标 运行中/已关闭
```

## 数据源

只读本地数据（`~/.claude/projects/`、`~/.codex/sessions/`），不联网。

## Codex 索引对账（`codex-index`）

Codex 桌面版的 Recents 读 `~/.codex/sqlite/codex-dev.db` 的 `local_thread_catalog`，
而那张表**长期只收录一部分会话**（2026-10-13 实测：盘上 78 个线程、索引仅 27 个）。
漏掉的会话在 Codex 界面里"消失"，但 rollout 文件完好——本 skill 直接读文件，所以照样能开。

```bash
python scripts/agent_dialog.py codex-index        # 索引收录 N / 盘上 M / 缺失 K + 清单
python scripts/agent_dialog.py codex-index --all  # 列全
```

实测结论：**索引缺失不影响打开**，`codex resume <id>` 对不在索引里的会话照样成功。
所以不必去写 Codex 那个库（本 skill 只以 `mode=ro` 读它）。

## 使用

作为 Claude Code skill 使用，详见 `SKILL.md`。
