# 快速使用教程

这份教程带你从零跑完一轮三角协作：**L（你）定目标，C 规划并独立检查，W 执行并报告**。

两种跑法：

- **自动模式**：用仓库自带的 `tools/triad_bridge`，C 直接派单、等报告、审阅、确认，再派下一轮，你不用搬运内容。需要一次性配置，只在 Windows + WorkBuddy 5.9.3 这一种表结构上验证过。
- **人工模式**：两个独立会话加手工转发，不需要配置。适合先体验流程，或本机条件不满足。

全文示例都是虚构演示数据，不含任何人的真实项目。

## 第 1 步：准备三个目录

自动模式要区分三类位置，先把它们定下来，后面所有命令都引用这三个变量：

| 变量 | 是什么 | 要求 |
| --- | --- | --- |
| `TRIAD_REPO` | 完整通道仓库，解压或 clone 的落点 | 必须包含 `tools/`；不要放进 `PROJECTS_ROOT` |
| `PROJECTS_ROOT` | 业务项目根，C 写任务单的根目录 | **必须已存在**，且是真实目录 |
| `TRIAD_PRIVATE` | 私有配置与备份区 | 必须在 `TRIAD_REPO` 和 `PROJECTS_ROOT` 之外 |

代码里的校验是硬的：`PROJECTS_ROOT` 不存在就直接拒绝；业务项目必须是它的**直接子目录**，不能是孙目录；业务项目也不能放进通道源码目录里。私有配置和备份目录同样不能落在通道源码或 `PROJECTS_ROOT` 里面。

PowerShell（Windows）：

```powershell
$TRIAD_REPO    = 'D:\tools\triad-collaboration'   # 第 2 步解压到这里
$PROJECTS_ROOT = 'D:\projects'                    # 业务项目根
$TRIAD_PRIVATE = 'D:\triad-private'               # 私有配置与备份

New-Item -ItemType Directory -Force -Path $PROJECTS_ROOT, $TRIAD_PRIVATE | Out-Null
New-Item -ItemType Directory -Force -Path "$PROJECTS_ROOT\demo" | Out-Null
New-Item -ItemType Directory -Force -Path "$PROJECTS_ROOT\demo\.agents\skills" | Out-Null
```

bash（只用于建目录，自动通道本身只在 Windows 上验证过）：

```bash
TRIAD_REPO="/path/to/triad-collaboration"   # 第 2 步解压后得到
PROJECTS_ROOT="/path/to/projects"
TRIAD_PRIVATE="/path/to/triad-private"

mkdir -p "$PROJECTS_ROOT/demo/.agents/skills" "$TRIAD_PRIVATE"
```

`demo` 是你的第一个业务子项目。`TRIAD_PRIVATE` 里的备份根如果还不存在，它的**父目录**要先有——`init` 会检查这一点，建不出来会提前告诉你。

不要把通道源码复制到业务目录里，那会撞上它的源码边界校验。通道留在 `TRIAD_REPO`，业务项目只放自己的东西。

## 第 2 步：下载完整仓库

自动模式需要 `tools/`，所以不能只复制技能文件夹。

- 0.2.0 源码压缩包：<https://github.com/SL-9ithub/triad-collaboration/archive/refs/tags/v0.2.0.zip>
- 0.2.0 release 页：<https://github.com/SL-9ithub/triad-collaboration/releases/tag/v0.2.0>
- 用模板建自己的仓库（Use this template）：<https://github.com/SL-9ithub/triad-collaboration>

解压到 `TRIAD_REPO`，确认顶层至少有以下内容：

```text
.agents/skills/triad-collaboration/SKILL.md
tools/triad_bridge_cli.py
tools/triad_bridge/
tools/config.example.json
QUICKSTART.md
```

只把 `.agents/skills/triad-collaboration/` 复制进已有项目，得到的是技能和模板，没有通道。这个区别后面排错也用得上。

## 第 3 步：装 Windows 依赖

Python 3.11+，然后一条命令：

```powershell
python -m pip install -r "$TRIAD_REPO\tools\requirements-windows.txt"
```

只有 pywin32。`submit` 要用它收紧备份目录的 ACL 并回读确认，没有降级路径。

## 第 4 步：准备两个参考任务

通道靠两个你本人创建、并且成功跑过的任务来确认账号归属。如果现在还没有这样的两个任务，先在 WorkBuddy 的正常界面里各建一个无害的演示任务，各跑一次，再回来用 `discover` 找到它们。本桥不会替你猜 owner，也不该从别人的教程里复制任务 UUID。

客户端登录、权限提示按你本机的正常流程处理。这个流程不需要你把凭据交给 C 或 W。

先只读列出来：

```powershell
python "$TRIAD_REPO\tools\triad_bridge_cli.py" discover --database '<你的 workbuddy.db>' --limit 20
```

输出里有任务 id、名称、状态、创建时间、owner 摘要前 16 位、历史行里出现过的模型 id。它不会替你挑：参考任务要你自己选，两个必须属于同一账号。历史行里出现过某个模型，不代表它现在还能用。

## 第 5 步：init 预演，再落盘

先不带 `--write`，它只读校验、不写文件：

```powershell
python "$TRIAD_REPO\tools\triad_bridge_cli.py" --config "$TRIAD_PRIVATE\triad-config.json" init `
  --cooperation-root $PROJECTS_ROOT --database '<你的 workbuddy.db>' `
  --backup-dir "$TRIAD_PRIVATE\triad-backups" `
  --owner-reference '<任务A的UUID>' --owner-reference '<任务B的UUID>'
```

确认输出无误后加 `--write`。它会写私有配置、建备份目录并把 ACL 收紧到只有你和 SYSTEM、把 profile 摘要和 owner 绑定记进配置。模型默认 `deepseek-v4.1-flash`，要用更强的型号得自己写出来，并且落在 `model_allowlist` 内。

配置必须放在仓库外、也放在 `PROJECTS_ROOT` 外，`init` 会拒绝这两处。备份目录要求是空的专用目录，非空直接拒绝，而且拒绝发生在任何写入之前。

完整参数、拒绝清单和排错表在 [docs/auto-setup.md](docs/auto-setup.md)，这里只走最短路径。

## 第 6 步：把技能复制到业务项目

技能原样在 `TRIAD_REPO` 里，但 C 的会话开在业务项目里，所以要把整个技能文件夹复制过去：

```powershell
Copy-Item -Recurse -Force `
  "$TRIAD_REPO\.agents\skills\triad-collaboration" `
  "$PROJECTS_ROOT\demo\.agents\skills\triad-collaboration"
```

复制后再确认 `$PROJECTS_ROOT\demo\.agents\skills\triad-collaboration\SKILL.md` 存在（资源管理器和 `dir` 默认不显示以点开头的目录，用 `Get-ChildItem -Force` 看）。

`tools/` 留在 `TRIAD_REPO`，不复制。技能可以单独复制，但通道不随技能走；`SKILL.md` 里提到的 `docs/` 与 `tools/` 都指完整安装目录里的路径，不是技能目录自己的相对路径。

## 第 7 步：给 C 一段自动协作启动语

在 `$PROJECTS_ROOT\demo` 里启动 C 的会话，加载 `$triad-collaboration` 技能——能否加载取决于客户端是否支持项目级技能。把下面这段发给它，方括号换成你的实际值：

```text
合作任务，自动模式。我是 L，负责目标、取舍和最终验收；你是 C，负责规划、任务单、派工和独立检查；W 是另一个独立会话或执行器，负责执行和报告。

目标：[用可观察的结果描述本轮要存在什么]
业务目录：[PROJECTS_ROOT 下的那个直接子目录，绝对路径]
通道入口：[TRIAD_REPO 绝对路径]\tools\triad_bridge_cli.py
私有配置：[TRIAD_PRIVATE 绝对路径]\triad-config.json
本轮请求ID：[唯一，例如 demo-20261108-a1]
轮次 nonce：[一行唯一串]
难度：simple
允许修改的范围：[具体文件或目录]
验收标准：[逐条编号，可由文件正文核对]

请按顺序做：plan 预演给我看范围与模型 → submit 派单 → wait（单次最长 55 秒，idle 就继续，累计不超过本轮预算）→ 读到报告后核对 event_sha256、打开实际成果逐条比对 → ack accepted 或 needs_revision → 通过就写下一轮任务单，不通过就开新一轮修正。

停止条件：本轮目标达成、你给出可核对证据之后，交我验收并停止新增派工；只有计划内确实还有未完成的轮次时才派下一轮；验收不通过只开修正轮，不因为“通过”就无限续派。

不要拿 W 的"已完成"当验收通过。报告固定后不覆盖，修正写进新轮次。如果命令报错或结果不确定，先用同一个请求ID跑 status，不换 ID 重投，不自动恢复数据库备份。遇到登录、授权或权限提示按本机正常流程处理，不要假设始终无人介入。
```

## 第 8 步：C 在自己那侧走完一轮

命令的实际形态：

```text
plan   预演：范围、模型、任务单字节数，不写库
submit 派单：只读核查 → 备份并回读 ACL → 一个写事务只 INSERT 一条 ACTIVE 一次性定义 → 写回执
wait   有界等待 W 发布报告，拿到 event_sha256
审阅   C 打开实际成果逐条核对，不看"已完成"
ack    记录本轮结论：accepted 或 needs_revision
接续   计划内还有工作才写下一轮任务单（新 id、新 nonce）；不通过就开修正轮
```

各命令的完整参数、`status` 返回什么、失败怎么分类处理，见 [docs/auto-setup.md](docs/auto-setup.md)。这里只说两件容易搞错的事：

- **`submit` 的失败分两段。** 事务提交之前失败，确实不会新增行；事务已经提交、只剩写回执时失败，就不能再说"没留下新任务"。这时用同一个 id 跑 `status` 看实际情况：`registered` 说明行已经写进去了，`definition.status` 是它在库里的当前状态，`report_exists` 说明 W 的报告有没有落地。不要重投，也不要用备份去恢复整库。
- **请求模型不等于实际执行模型。** 实际型号要到那次原生运行记录里核对，`status` 返回的 `definition.recorded_model` 是库里记下的型号，不是运行证据。

## 第 9 步：这一轮的验收仍然是你

C 给出"已验证 / 未通过 / 未验证"逐条结论，你打开实际成果自己读一遍，确认它符合你最初的要求，而不是符合 C 改过的版本。C 写了"未验证"的地方，你要么自己补查，要么明确接受这个缺口。

## 人工模式：不配置也能跑

不需要任何依赖和配置，代价是你要在两个会话之间转发。适合第一次体验流程，或者本机条件不满足自动模式的边界。

### 1. 准备两个独立会话

| 会话 | 角色 | 用哪个 AI | 干什么 |
| --- | --- | --- | --- |
| 会话 1 | C | 你日常的主力 AI | 规划、写任务单、独立检查成果 |
| 会话 2 | W | 你当前可用的免费 Agent | 按任务单执行，交成果和报告 |

两个会话必须分开，否则执行的人和检查的人是同一个，独立检查就失效了。客户端支持项目级技能时输入 `$triad-collaboration` 加载；不支持就把 [prompts/start-cn.md](prompts/start-cn.md) 里“发给 C”的整段话贴进去。

人工模式同样需要把技能文件夹放进工作项目，路径与第 6 步一样，只是不需要 `tools/`。

### 2. 发启动语

`prompts/start-cn.md` 里有 C 和 W 两份可以直接复制的启动语。下面是填好示例内容的一份，你可以整段用，只改项目目录：

```text
合作任务。按三角协作方式推进：我是 L，负责目标、取舍和最终验收；你是 C，负责规划、写任务单、派工和独立检查；W 是另一个独立会话，负责执行和报告。

本次目标：把三条虚构待办整理成 deliverables/todo.md 一页清单。
项目目录：[你的工作目录]          例：triad-demo/
允许读写：只允许新建 deliverables/todo.md，其他文件不改。
输入（虚构演示数据）：
  1. 整理书桌 — 截止 2026-03-07
  2. 归还图书 — 截止 2026-03-08
  3. 备份照片 — 截止 2026-03-08
验收标准（逐条编号，全部可读文件正文核对）：
  ① 三条待办的名称与日期和输入一致。只核对这两项：允许添加复选框、`截止` 之类的标签和分隔符；
  ② 按截止日期升序，03-07 在 03-08 前面；
  ③ 每项以 "- [ ] " 开头（标准 Markdown 复选框）；
  ④ 只含一行标题"# 待办清单"与三条待办，可有空行，无额外解释。
协作方式：人工传递，我负责把你的任务单转给 W，再把 W 的报告和成果带回来。
预算与限制：本轮 15 分钟内；不联网，不装依赖，不动项目目录以外的文件。

请先写第01轮任务单，包含唯一请求ID、成果位置、逐条验收标准和你会怎么检查。验收标准照抄我上面四条，不要改写成更容易达成的替代条件；如果某条做不到，记"未验证"，不要放宽。

W 回报后请亲自打开成果核对。看不到文件就写"未验证"并说明缺什么材料。不要拿 W 的"已完成"当验收通过。
```

### 3. 传任务单、收报告

消息流向是固定的四段：

```text
L 发需求 → C 出任务单 → L 粘给 W → W 交成果+报告 → L 把两者交回 C → C 独立检查
```

W 的成果至少要能看到文件正文。只回“已完成”的，退回重做。C 如果只收到转述，就写“未验证”，不能给通过。

### 4. 看 C 的结论，自己验收

示例成果长这样（虚构数据）：

```markdown
# 待办清单

- [ ] 整理书桌 — 截止：2026-03-07
- [ ] 归还图书 — 截止：2026-03-08
- [ ] 备份照片 — 截止：2026-03-08
```

对照输入：三条名称与日期对得上，多出来的 `- [ ] `、`—`、`截止：` 属于第 ① 条明确允许的附加符号，行间空行被第 ④ 条允许。

C 的检查应该是这个粒度，每条对应一次真实动作：

| # | 标准 | C 的动作 | 结论写法 |
| --- | --- | --- | --- |
| ① | 名称与日期一致 | 打开文件逐行比对三个名称、两个日期 | 已验证 / 未通过 |
| ② | 03-07 在 03-08 前面 | 读三行的顺序 | 已验证 / 未通过 |
| ③ | 每行以 `- [ ] ` 开头 | 逐行看前缀，数出 3 个 | 已验证 / 未通过 |
| ④ | 只有一行标题加三条待办 | 通读全文，确认除空行外无多余文字 | 已验证 / 未通过 |

然后你自己打开文件读一遍。这一步不能由 C 或 W 代签。

## 常见问题

**`$triad-collaboration` 没反应**

先确认客户端支持项目级技能、业务项目里有 `.agents/skills/triad-collaboration/SKILL.md`（点目录可能不显示）、会话开在这个项目里。都正常还不行就走人工模式。

**`init` 报 `config_cooperation_root_inside_source` 或 `unexpected_project_boundary`**

`PROJECTS_ROOT` 落在通道源码目录里，或者业务项目不是它的直接子目录。按第 1 步的三个目录重新分开，不要把源码复制到业务目录。

**`init` 报 `backup_dir_not_empty_use_new_directory`**

备份根要一个专用空目录。换一个新的，不要清理原有内容。

**`discover` 报 `unsupported_schema` 或 `schema_ddl_mismatch`**

你的软件版本或表结构和已审阅的 profile 不一致。通道只认 `workbuddy-5.9.3-automations`，其它一律拒绝，没有 `--force`。不要自己改库结构绕过。

**`submit` 报 `task_already_registered_use_status`**

这个 id 已经登记过。用 `status` 查它现在的状态，不换 id 重投。

**`submit` 之后没看到回执，但也不确定有没有写进去**

先 `status`。`registered` 为真说明行已经写进去了，别再来一次。回执丢了不影响任务的登记状态，也不要用备份恢复整库——恢复是高风险操作，需要单独确认。

**C 读不到 W 的文件（人工模式）**

两个会话通常不在同一个文件系统视图里。让 W 把成果完整正文贴出来，或把两个会话放到同一个共享目录用相对路径沟通。

**没有通道还能继续吗**

能。人工模式本来就是完整闭环，代价是你转发。通道省的是搬运，不改变角色分工。

## 这套东西验证到哪

自动通道在维护者的 Windows + WorkBuddy 5.9.3 环境上跑完了一个无害的两轮案例，包括模型核对、独立审阅和续接。离线测试用合成 fixture 覆盖了结构/账号/路径守卫、单行写入、备份与 ACL 回读、事件闭环和跨目录 CLI。

没有验证过的：其它软件版本、其它机器、复杂业务轮次、空闲唤醒、跨设备协作、陌生用户从零配置的实际耗时。遇到这些情况按人工模式走，或者先按 [docs/auto-setup.md](docs/auto-setup.md) 的兼容边界自行确认。

省额度的效果没有定量数据。规划和验收仍在主力模型上完成，总量是否更省取决于任务本身。
