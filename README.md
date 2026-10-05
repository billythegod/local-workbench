# Local Workbench · 工作工具箱

一个只跑在本机的个人工作台：把散落在各 AI CLI 工具里的历史会话、本机运行状态、交付文件证据接到一个入口里。纯 Python 标准库 + 原生 HTML/CSS/JS，**零第三方依赖、零云端服务、零模型 API**。

如果你日常在多台「AI 结对工具」间切换工作（Codex、Claude Code、OpenCode……），大概也遇到过这三个问题：上周让某个 agent 得出的结论找不到了；开一个重任务前不确定内存够不够；交付出去的目录到底齐不齐、后来改没改过。这个工具箱就是针对它们的三件套。

## 三个应用

| 应用 | 解决的问题 | 能做什么 |
| --- | --- | --- |
| **工作回溯**（`apps/history`） | 跨工具找回旧结论和原始出处 | 把本机 Codex / Claude Code / OpenCode / ZCode / Pi / Hermes 的已落盘历史建成本地 SQLite 索引：关键词搜索、按来源/项目/日期/主会话筛选、每条结果给出「文件 + 物理行号」或「数据库表 + 主键」级出处 |
| **本机运行台**（`apps/runtime`） | 重任务开工前看机器，出问题后留证据 | 手动采样内存/进程/TCP 监听；开工前按预计内存做保守检查；任务前后各留快照并比对差异；读取历史 panic / Jetsam 报告短片段 |
| **交付核验台**（`apps/delivery`） | 成品目录缺件、变更、验收版本混淆 | 只读扫描目录生成文件清单（大小/mtime/SHA-256）+ 有限格式检查（字幕时序、Office 容器、JSON、Markdown 链接、媒体元数据）；对比两个快照的新增/缺失/变化；显式记录人工验收并与快照哈希绑定 |

一条贯穿全部三个应用的原则：**自动检查、人工验收、发布三者分开记录**。程序永远不会因为你点了什么按钮就替你记下「人工验收通过」，也永远不执行发布——人工验收必须显式确认，且绑定具体快照哈希，文件一改即过期。

## 安全与隐私模型

- **数据不出本机**：服务只绑定 `127.0.0.1`；不发网络请求、不调用模型 API、不上传任何数据；所有输出写在项目内 `data/` 目录。
- **来源校验**：Host 精确匹配（堵 DNS rebinding）+ Origin 白名单 + `Sec-Fetch-Site: cross-site` 拒绝 + POST 专用头 `X-Workbench-Request`，跨站表单与恶意页面全部在三道关卡失败。
- **只读外部世界**：历史源只读；被扫描目录只读；进程只观测不控制。路径逐级 `O_NOFOLLOW` 打开，静态资源有后缀白名单 + 目录逃逸检查。
- **入库脱敏**：索引正文与错误消息中的 API key、token、Bearer、私钥块、URL 凭据、credentials 嵌套对象在入库前脱敏，API 输出二次脱敏；源文件保持原样。
- **证据防篡改**（交付核验台）：快照带内容哈希 + 证据哈希双重自校验，损坏即拒用并显式报告；人工验收记录与快照绑定且含自身哈希；`ledger.jsonl` 只追加登记簿与磁盘双向比对，删除或篡改记录会在界面看到红色告警。
- **崩溃安全**：JSON 落盘全部走临时文件 + fsync + 原子替换；索引重建先写临时库，checkpoint 确认后再替换并清理 WAL 伴生文件，失败不丢现有索引。

## 快速开始

macOS + Python 3.10 或更新版本（运行台的数据采集针对 macOS；history 与 delivery 两个模块在其他类 Unix 上也可用）。

```sh
git clone https://github.com/billythegod/local-workbench.git
cd local-workbench
python3 workbench.py open    # 默认地址 http://127.0.0.1:8899/
```

首次打开工作回溯会提示索引未建立，点「增量更新」读取本机历史。默认端口被占用时会自动改用空闲端口；也可以直接双击仓库里的 `启动工作工具箱.command`（macOS）。

常用命令：

```sh
python3 workbench.py open    # 启动并打开浏览器（已有实例则复用）
python3 workbench.py status  # 核对实际监听实例
python3 workbench.py stop    # 仅停止身份核对通过的本工具箱进程
```

服务不开机自启；停止它不会影响机器上其他服务。

## 架构

```
workbench.py          父服务器：/api/<name> 路由、静态托管、来源校验、进程生命周期
apps/_shared.py       模块公共层：脱敏（full/light）、原子写、时间戳、异常→状态码映射
apps/<name>/service.py    模块后端，导出 api(method, path, query, body) -> (status, JSON)
apps/<name>/index.html    模块界面，原生 HTML/CSS/JS，访问 /apps/<name>/
apps/<name>/test_*.py     模块单测（受控夹具，不触碰真实数据）
web/index.html        总览页（iframe 集成三个应用）
data/<name>/          运行数据（索引、快照、清单、登记簿），不进版本库
docs/模块契约.md      父服务器与模块之间的全部约定，含新增模块步骤
```

每个模块保持「厚领域、薄外壳」：父服务器统一承载安全边界与进程管理，模块只实现纯函数式的 `api()` 适配。新增一个模块只需要五步，详见 [docs/模块契约.md](docs/模块契约.md)。

## 测试

四套共 60+ 个单元测试，覆盖服务器边界（来源校验/体积限制/路径白名单）、索引构建与去重、解析故障保留、脱敏、哈希预算、路径逃逸、登记簿与防篡改等：

```sh
python3 -m unittest discover -s tests -v
python3 -m unittest discover -s apps/history -p 'test*.py' -v
python3 -m unittest discover -s apps/runtime -p 'test*.py' -v
python3 -m unittest discover -s apps/delivery -p 'test*.py' -v
```

三个模块也各自提供命令行入口（`python3 apps/<name>/service.py --help`），不启动服务即可使用索引、采样与扫描能力。

## 这个工具故意不做的事

- 不执行发布、不移动或修改被扫描的成品文件；
- 不调用模型 API 做摘要或问答——原始出处不经转述；
- 不常驻监控、不自动轮询、不推送通知——用当前证据判断当前状态；
- 不做多用户、鉴权或远程访问——单人本机工具，127.0.0.1 是安全边界而非缺陷；
- 不替你判断金融事实、代码正确性或内容质量——那是人工验收与专业流程的事。

## 隐私说明

本仓库只包含源码。运行产生的数据（历史索引、运行快照、交付证据）全部落在本地 `data/` 目录，已被 `.gitignore` 排除；历史源文件永远只读。分享或备份本工具箱时请只复制源码，不要连同 `data/` 一起外发——索引和快照里是你自己的工作内容。

## License

[MIT](LICENSE)
