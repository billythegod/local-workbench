# 本机运行台

面向本机模型、TTS 和渲染工作：手动核对内存，确认当前进程与监听端口归属，保存前后快照，读取历史系统故障短片段。使用 Python 3 标准库和原生 HTML/CSS/JS，无外部依赖。

父服务器导入 `service.api(method, path, query, body)`，界面由 `/apps/runtime/` 提供。模块不启动服务器、不常驻采样。所有写入只发生在 `data/runtime/` 和本模块验证目录。

## API

| 方法 | `/api/runtime` 后缀 | 参数与返回 |
|---|---|---|
| GET | `/snapshot` 或 `/` | 实时内存、进程摘要、TCP LISTEN、采集证据与指标限制；失败项保留 `unknown`/`null` |
| GET | `/process?pid=123` | 正整数 PID；按需读取 cwd、最多 10 层父进程链，复查启动时间防止 PID 重用误归属 |
| GET | `/incidents` | 仅枚举两个 DiagnosticReports 根目录及各自 Retired 的顶层，读取最新至多 20 个匹配报告，提取时间与原因短片段 |
| GET | `/snapshots` | 最近至多 200 个手动保存快照的元数据与读取错误 |
| GET | `/snapshots/<id>` | 按严格白名单 ID 读取本模块快照；没有任意路径访问 |
| POST | `/snapshots` | `{"name":"长 TTS 前"}`；重新实时采样，原子保存本机 JSON，返回 201 |
| POST | `/preflight` | `{"extra_gib":8,"task":"tts"}`；任务类型 `inference` / `tts` / `render`，返回检查与本次真实快照 |
| POST | `/compare` | `{"before":"<id>","after":"<id>"}`；比较两次保存快照，后一次时间不得早于前一次 |

所有 POST 的浏览器请求带 `Content-Type: application/json` 与 `X-Workbench-Request: 1`。父服务器负责来源与请求大小检查。浏览器导出 JSON 是当前选中的报告；可导出实时采样、开工前检查、保存快照、差异或历史系统报告。动态内容用 `textContent` / DOM 节点渲染，无动态 `innerHTML`。

## 只读命令与指标

- `vm_stat`：使用输出中的页大小换算；空闲物理页、wired、压缩区物理页和压缩前保存页分别显示。
- `sysctl hw.memsize`、`sysctl vm.swapusage`、`sysctl kern.memorystatus_vm_pressure_level`：分别采样，单项失败不伪装成零。
- `memory_pressure -Q`：只查询，不触发内存压力测试。其报告比例与空闲物理页比例口径不同，不参与容量计算。
- `ps -axo pid=,ppid=,rss=,pcpu=,etime=,lstart=,comm=`：不读取完整参数或环境，进程名称额外进行敏感词脱敏；原始进程表不保存。
- `lsof -nP -iTCP -sTCP:LISTEN -Fpcn`：仅查看权限范围内的 TCP LISTEN。cwd 查询限定为经过正整数校验的单个 PID。

每条命令超时 4 秒，执行环境只传固定 PATH 和 C locale，输出上限 2 MiB。服务不发送网络请求、不调用模型、不读取认证配置、不修改服务、不 kill 或 restart 进程。

进程 RSS 包含共享驻留页，不能累加为系统物理占用，也不能与 MLX/Metal 的 unified-memory footprint 混算。端口监听不证明健康、模型已加载或配置生效；cwd 不证明配置被当前进程继承。历史服务端口不会被自动赋予服务身份。

## 开工前检查规则

用当前空闲物理页扣除填写的额外用量，保留 `max(8 GiB, 总物理内存 10%)`。系统 pressure warning / critical、压缩器占用达到总内存 15%、已用交换空间达到总内存 5%，任一条件触发警示。该规则是保守筛查策略，未经容量实验校准。交换空间可能含旧页，单次值不能证明正在严重换页；模型/TTS/渲染瞬时峰值未测量。

已知警示优先显示；关键指标缺失时显示无法判断；全部可采但未触线时写“本次采样暂未触及规则线”，不承诺安全。手动两次快照不能观测中间峰值，也不会自动通知。

## 历史系统报告

只读 `/Library/Logs/DiagnosticReports` 与 `~/Library/Logs/DiagnosticReports` 根目录及各自 `Retired` 子目录顶层的 panic、Jetsam、ResetCounter、Kernel `.ips` / `.panic` / `.log` / `.diag`。四个目录逐一记录覆盖状态，不再递归。跳过符号链接，不读取超过 2 MiB 的正文；原因字段仅保留短片段并脱敏。不把历史原因当成当前原因，不把最大进程当成唯一责任方。

## 命令行与验证

```sh
python3 apps/runtime/service.py snapshot
python3 apps/runtime/service.py process --pid 123
python3 apps/runtime/service.py preflight --extra-gib 8 --task tts
python3 apps/runtime/service.py incidents
python3 -B -m unittest discover -s apps/runtime -p 'test_service.py' -v
```

单测（`test_service.py`）验证解析、工具缺失、超时、unknown 保留、PID 重用、归属复查、阈值与路径白名单；全部使用受控样本，不触发真实重任务。本机采样检查真实命令输出及序列化。浏览器人工验收、容量压测、模型健康探测与发布另行记录，不能以自动检查代替。
