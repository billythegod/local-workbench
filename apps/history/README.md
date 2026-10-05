# 工作回溯

用已落盘的本机历史找回结论、交付目录和原始出处。原记录只读，索引保存在项目根的 `data/history/`；不上传，不调用模型，不执行历史中的任何指令。模块由项目统一服务器挂载，不独立启动服务。

## 已接入的来源

| 来源 | 读取范围 | 正文口径 | 原始出处 |
|---|---|---|---|
| Codex | `~/.codex/state_5.sqlite` 的显式会话字段；`sessions/`、`archived_sessions/` 的 JSONL；`thread_history_1.sqlite` 的用户/最终回答条目 | 用户需求；明确 `final` / `final_answer`、`task_complete` 或 `thread_turns.final_agent_item_id` 标定的回答 | JSONL 文件与物理行号，或 `thread_items(thread_id,item_id)` |
| Claude Code | `~/.claude/projects/**/*.jsonl`，包含 `subagents` | 用户文本；`end_turn` / `stop_sequence` 终答。工具块与 thinking 不入库 | JSONL 文件与物理行号 |
| OpenCode | `~/.local/share/opencode/opencode.db` 的 `session`、`session_v2`、`message`、`part`、`session_message` | 用户文本；明确 stop / end_turn / completed 的回答 | 数据库路径、表名和主键 |
| ZCode 旧会话 | `~/.zcode/cli/db/db.sqlite` 的 `session/message/part` | 同 OpenCode；中断、长度限制、工具调用等非终答不收录 | 数据库路径、`message.id` |
| Pi | `~/.pi/agent/sessions/**/*.jsonl` | 用户文本；`stopReason=stop` 的回答 | JSONL 文件与物理行号 |
| Hermes | `~/.hermes/state.db` 的显式 id/title/cwd/parent/time 字段及符合条件的消息 | 用户正文；`finish_reason=stop` 且无工具调用的回答 | 数据库路径、`messages.id` |
| ZCode V2 任务目录 | `~/.zcode/v2/tasks-index.sqlite` 的显式目录、标题、状态、provider、日期字段 | **仅元数据，未导入正文**；不读取 `meta_json`、`searchable_text` | 数据库路径、workspace_path 和 task_id |

Gemini Antigravity 历史未导入。本轮未把所有本机文件视作会话来源，也没有读取配置、凭据、环境文件。

## 覆盖与去重

- 每次建立索引均发现指定目录中的全部 `.jsonl` 并刷新可读数据库里的会话元数据。保留有元数据但原日志缺失的会话，缺失记录进入来源清单。
- JSONL 使用二进制流逐行读取。单行最多 8 MiB；超长行继续按有限块跳过，物理行号不变。损坏行、缺少元数据、读取时文件变化分别记录。文件无额外大小限制。
- 每条正文最多保留 65,536 字符，保留前 49,152 与末 16,384 字符，并显示省略标记；中间部分不参与搜索。SQLite 单条可见记录同样设 8 MiB 的读取上限。
- Codex 的 event/response 双份在同会话、同角色、同原文且相邻 15 秒内去重；同一来源中再次提出的相同需求保留。早期没有 phase 的回答须由 `task_complete` 确认，尽量保留原 response 行号。
- Codex SQLite 补导按同会话、角色、正文哈希和出现次数与 JSONL 对齐。数据库已有但 JSONL 没有覆盖的记录才补入。SQLite-only 线程缺少会话元数据时标为**父子关系未知**，默认主会话筛选不显示，选择包含全部会话可见。
- OpenCode 新旧表中的相同会话 ID 合并，用户与终答镜像再按内容和相邻时间去重。来自不同原生来源的独立会话不按相似标题强行合并。
- ZCode V2 任务目录可能与原生会话重叠，因此**单独记录条数，不并入会话总数**，也不假称其正文已覆盖。
- 增量时，未改变的 JSONL 复用索引；变化的文件重新读取。SQLite 来源每次刷新。成功遍历的来源会清除已删除文件的正文；目录访问失败时保留既有结果并报告失败，避免误删。重建先写临时数据库，完成后再替换当前索引。
- 原文本中的常见 API key、token、密码、Bearer/Basic、URL 凭据、私钥块和 credentials 嵌套对象在入库前脱敏，API 输出再次脱敏。规则脱敏不等于人工逐条确认所有自由文本中的敏感信息；原始源文件保持原样。

实际覆盖以 `data/history/job.json`、API `/status` 和 `/manifest` 为准。会话继续追加时计数会变化。自动检查、真实源读取、浏览器验收分别记录，不能由单元测试推断人工验收已完成。

## 命令行

在项目根目录执行：

```sh
python3 apps/history/service.py index --mode incremental
python3 apps/history/service.py index --mode rebuild
python3 apps/history/service.py search '视频 验收' --limit 10
python3 apps/history/service.py search 部署 --source codex --include-subagents
python3 apps/history/service.py search '修复' --project '/path/to/your/project' --from 2026-10-01 --to 2026-10-02
python3 apps/history/service.py status
python3 -m unittest discover -s apps/history -p test_history.py -v
```

日期按北京时间的日界查询，持久化时间统一为 UTC。关键词以空格分隔，最多 8 个，各词均须在正文里命中；另行返回标题/目录匹配的元数据条目。中文直接匹配，ASCII 与英文大小写不敏感，`%`、`_`、引号是普通检索字符。查询使用参数化 SQL，不执行原文。

## 父服务器 API 合同

`service.py` 导出：

```python
api(method, path, query, body) -> (http_status, JSON可序列化对象)
```

`path` 为 `/api/history` 后缀，以 `/` 开头；`query` 为 `parse_qs` 形式，`body` 为 dict。静态界面是 `apps/history/index.html`，访问 `/apps/history/`。

| 请求 | 用途 |
|---|---|
| `GET /status` | 当前索引计数、单独的任务目录计数、最近任务状态、done/total/current、失败列表和来源报告 |
| `GET /search` | 参数 `q/source/project/from/to/main/role/page/limit`；返回正文结果与片段、原始出处及另外的元数据匹配 |
| `GET /sessions/<URL编码的会话key>` | 按时间展示用户/终答；支持 `page/limit`，`anchor=<消息ID>` 自动定位所在页 |
| `GET /stats` | 各来源、最近项目、高频需求；可用相同来源/项目/日期/主会话筛选 |
| `GET /manifest` | 来源文件清单与覆盖计数；支持 `source/page/limit/limited=1` |
| `POST /index` | JSON `{"mode":"incremental"}` 或 `{"mode":"rebuild"}`；返回 202 后异步执行，`GET /status` 读取进度；已有任务返回 409 |

`source` 可选 `codex/claude/opencode/pi/hermes/zcode/zcode_tasks`；`main=1` 为默认主会话，`main=all` 包含子 agent 和关系未知条目，`main=sub` 仅明确的子 agent。`role=user/assistant` 过滤正文。页码从 1 开始，每页最多 100 条。

浏览器所有 POST 均带 `Content-Type: application/json` 与 `X-Workbench-Request: 1`。来源校验和仅绑定 127.0.0.1 由父服务器负责。索引写入通过进程锁和文件锁排他；状态写入使用原子替换。GET 只查本地索引，不隐式读取整个历史。界面以 DOM 文本节点显示历史，原文 HTML/脚本不会执行。

高频需求是对**已索引用户消息**执行公开的关键词分类，允许一条消息命中多类，不代表独立需求数量或完整工作量。最近目录按会话更新时间排序。原生历史是否还能在对应应用里继续执行，不在此工具的验证范围内。

## 交付文件

- `service.py`：适配器、索引器、CLI、API。
- `index.html`：搜索、筛选、上下文、复制出处、来源清单与索引进度。
- `test_history.py`：受控夹具下的单元测试（只在 `fixtures/` 子目录生成测试数据，不触碰真实来源）。

索引失败时可先看 `/status` 的 `job.errors`；损坏或缺失日志看 `/manifest?limited=1`。修复原始记录或恢复其可读性后，再显式增量更新。模块不会修复或改写历史来源。
