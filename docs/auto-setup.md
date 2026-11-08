# 一次性配置与自动闭环

这篇文档说明怎么把本仓库自带的自动通道在本机配置起来，然后跑连续多轮：C 规划并派单，W 执行并发布报告，C 等待、审阅、确认，再派下一轮。任务单和报告由通道传递，L 不需要搬文件或粘贴内容；审阅由 C 自己做。

适用范围写在最前面，避免误解：

- 代码状态：随本仓库公开，维护者已在自己的机器上用它跑完一个无害的两轮真实案例（第一轮 `needs_revision`，第二轮 `accepted`，两轮都核对了原生运行记录里的实际模型）。这份包是那次验证之后的脱敏整理。
- 验证范围：维护者当前的 Windows 环境 + WorkBuddy 5.9.3 表结构 + 这一个两轮案例。其它机器、其它版本、复杂业务轮次、离线唤醒都没有验证过。
- 已知支持的软件结构：只有 `workbuddy-5.9.3-automations` 这一个经审阅的 `automations` 表结构。其它版本、其它产品、其它表结构一律拒绝，不做“大概兼容”的猜测。

## 它复用的是什么

不是新造一套协议，而是把本机已经在用的两段能力参数化后公开：

| 原能力 | 现在的公开形式 | 变化 |
| --- | --- | --- |
| 原生一次性派工（写一条 `automations` 定义） | `plan` / `submit` / `status` | 作者机器常量 → 私有配置 |
| 报告事件（publish / wait / ack） | `publish` / `wait` / `ack` | 项目根 → 配置里的 `cooperation_root` |
| 备份目录 ACL 收紧 | `submit` 内部前置步骤 | 机制不变，仍只用 pywin32 + 回读校验 |

## 0. 准备

### 三个目录

先把三类位置分开，后面所有命令都引用这三个变量：

| 变量 | 用途 | 代码里的校验 |
| --- | --- | --- |
| `TRIAD_REPO` | 完整通道仓库，必须含 `tools/` | 配置里的公开源码根要与它完全一致 |
| `PROJECTS_ROOT` | 业务项目根（配置里的 `cooperation_root`） | **必须已存在**且是真实目录；不能等于或位于 `TRIAD_REPO` 内 |
| `TRIAD_PRIVATE` | 私有配置与备份区 | 必须在 `TRIAD_REPO` 与 `PROJECTS_ROOT` 之外 |

业务项目必须是 `PROJECTS_ROOT` 的**直接子目录**，不能是孙目录，也不能位于通道源码目录内。备份根可以还不存在，但它的**父目录**必须已存在；`init` 在写入前会检查这一点，建不出来会在预演阶段就报出来。

```powershell
$TRIAD_REPO    = 'D:\tools\triad-collaboration'
$PROJECTS_ROOT = 'D:\projects'
$TRIAD_PRIVATE = 'D:\triad-private'

New-Item -ItemType Directory -Force -Path $PROJECTS_ROOT, $TRIAD_PRIVATE | Out-Null
New-Item -ItemType Directory -Force -Path "$PROJECTS_ROOT\demo" | Out-Null
New-Item -ItemType Directory -Force -Path "$PROJECTS_ROOT\demo\.agents\skills" | Out-Null
```

不要把通道源码复制进业务目录，那会同时撞上源码边界与项目边界两条校验。

### 其余准备

1. Python 3.11+。
2. Windows 上安装依赖：`python -m pip install -r "$TRIAD_REPO\tools\requirements-windows.txt"`（只有 pywin32）。
3. 找到你自己的 WorkBuddy 数据库文件（`workbuddy.db`）。**不要从别人的教程里抄路径**，也不要在不确定的文件上做任何写操作。
4. 把技能文件夹复制到业务项目，C 的会话要能加载到它：

   ```powershell
   Copy-Item -Recurse -Force "$TRIAD_REPO\.agents\skills\triad-collaboration" `
     "$PROJECTS_ROOT\demo\.agents\skills\triad-collaboration"
   ```

   `tools/` 不复制，始终用 `TRIAD_REPO` 下的绝对路径调用。技能可以单独复制，但通道不随技能走；`SKILL.md` 里提到的 `docs/`、`tools/` 都指完整安装目录中的路径，不是技能目录自身的相对路径。

所有命令都用绝对路径调用，可以在任意业务目录下执行，不需要改 `PYTHONPATH` 或 `PATH`：

```powershell
$BRIDGE = "$TRIAD_REPO\tools\triad_bridge_cli.py"
$CONFIG = "$TRIAD_PRIVATE\triad-config.json"
python $BRIDGE --config $CONFIG <命令> ...
```

配置**必须**放在仓库外、也要放在 `cooperation_root` 外。`init` 会拒绝把配置写进这两处。

## 1. 只读发现，选两个参考任务

还没绑定本机时，直接指向数据库文件：

```powershell
python $BRIDGE discover --database '<你的 workbuddy.db 绝对路径>' --limit 20
```

绑定之后再查，换成配置文件：

```powershell
python $BRIDGE --config $CONFIG discover --limit 20
```

两种模式都是只读（`mode=ro`），输出任务 id、名称、状态、创建时间、owner 摘要前 16 位、历史行里出现过的模型 id。未绑定模式只看库结构是否等于已审阅的 profile，不检查账号绑定；绑定模式会额外标出哪些任务属于你已绑定的账号。一次只给它一个输入，同时给 `--config` 和 `--database` 会直接报 `discover_ambiguous_input`。

`discover` 不替你挑任务，也不替你判断“最新可用模型”。参考任务要你自己从自己创建、且已成功跑过的任务里选两个，且两个属于同一账号；历史行里出现过的模型不等于现在还能用，工具不会自动挑最新，也不会把强模型自动设成默认。

如果还没有两个自己创建并跑成功的任务，先在 WorkBuddy 的正常界面里各建一个无害的演示任务、各跑一次，再回来用 `discover` 找这两个。本桥不替你猜 owner，也不要从别人的教程或截图里复制任务 UUID——那是别人的账号标识。客户端登录与权限提示按本机正常流程处理，不需要把凭据交给任何人。

## 2. 绑定本机（默认只预演）

```powershell
python $BRIDGE --config $CONFIG init `
  --cooperation-root $PROJECTS_ROOT `
  --database '<你的 workbuddy.db 绝对路径>' `
  --backup-dir 'D:\<private-area>\triad-backups' `
  --owner-reference '<任务A的UUID>' --owner-reference '<任务B的UUID>'
```

默认是预演：只读校验、不写任何文件。确认后加 `--write` 才落盘，并做三件事：写入私有配置（已存在且内容不同则拒绝，不静默覆盖）、准备私有备份目录并用 pywin32 收紧 ACL 再回读确认只有当前用户与 SYSTEM、把 profile 摘要与 owner 完整 SHA256 绑定进配置。

模型默认都是 `deepseek-v4.1-flash`：`--model-simple` 决定轻量档，`--model-hard` 不填就继承同一个型号。要用更强的模型得明确写出来，并且配置里必须同时在 `model_allowlist` 内。历史行里见过的型号不会被自动采用。

`--backup-dir` 的要求：目录不存在就新建，存在就必须是空目录，非空目录一律拒绝，而且拒绝发生在任何 ACL 或配置写入之前。重复执行同一个 `init` 只做只读核对，不会再给目录重设 ACL。所以备份根请专门给它一个目录，不要指向已经放着别的东西的位置。

`init` 会拒绝的情形：表结构与 profile 不符（缺列、多列、类型/default 变化、CHECK 或生成列不同、多出唯一索引）、表上出现 trigger、两个参考任务缺失/未确认/已删除/不同 owner、路径落进公开源码或 `cooperation_root`、配置路径已有不同内容、备份目录非空。

profile 摘要不是账号隐私：它只是软件表定义的摘要，同一版本软件在不同账号机器上相同。手工把未知结构的摘要写进配置也不会被接受，支持范围只由仓库里经审阅的 profile 决定，没有 `--force` 之类的绕过手段。

## 3. 派一轮

```powershell
python $BRIDGE --config $CONFIG plan `
  --project "$PROJECTS_ROOT\demo" --id demo-001 --name '演示第一轮' `
  --task '第01轮_C任务单.md' --report '第01轮_W报告.md' --nonce '<本轮唯一码>' `
  --difficulty simple --difficulty-reason '<为什么判为简单>'
```

`plan` 只读：不改库、不建备份、不写配置。确认无误后把 `plan` 换成 `submit`。

`submit` 的顺序是固定的：

1. 只读核查：profile 摘要、无 trigger、账号绑定、id 未登记、报告不存在。
2. 解析回执路径 `.cw/native-dispatch/`，确认它仍在项目内；被 junction 或符号链接引到项目外时在这里就停下。
3. 在线一致性备份 + `quick_check` + ACL 回读。
4. 开启写事务，事务内再查一遍结构、trigger、账号，然后只 `INSERT` 一条 `ACTIVE` / `once` / `default` 权限的任务定义，提交事务。
5. 原子写回执。

**失败的分界线在第 4 步提交那一刻。** 第 1 到第 4 步之间任何一处失败，事务没提交，不会新增行。第 4 步已提交、只在第 5 步写回执时失败，库里那条定义还在，这种情况不能描述成“没留下新任务”，也不要重投同一个 id：先跑 `status`。回执丢了不影响登记状态，任务照原样在库里。

不会写 sessions、automation_runs、认证、权限或别的行；不设周期任务，不装登录启动项。`plan` 与 `submit` 输出的 `task_bytes` 是任务单本身的大小，`prompt_bytes` 是加上头部之后的总大小。

### 查状态

`status` 只读，只回这一条任务自己的登记情况，参数与 `plan` 相同：

```powershell
python $BRIDGE --config $CONFIG status `
  --project "$PROJECTS_ROOT\demo" --id demo-001 --name '演示第一轮' `
  --task '第01轮_C任务单.md' --report '第01轮_W报告.md' --nonce '<本轮唯一码>' `
  --difficulty simple --difficulty-reason '<为什么判为简单>'
```

实际输出里这几个字段有用：

| 字段 | 含义 |
| --- | --- |
| `registered` | 这个 id 在库里有没有登记 |
| `definition` | 没登记时为 `null`；登记后含 `status`、`schedule_type`、`next_run_at`、`last_run_at`、`permission_mode`、`recorded_model` |
| `report_exists` | 本轮报告文件在不在 |
| `selected_model` | 配置里按难度选出的模型，是请求值不是运行证据 |

`definition.recorded_model` 是库里记下的型号，同样不能当成“实际执行模型”；那要看原生运行记录。

## 4. W 执行并发布，C 等待与确认

W 在自己的会话里跑任务、写报告，然后发布事件（W 自己跑，C 不代跑）：

```powershell
python $BRIDGE --config $CONFIG publish `
  --project "$PROJECTS_ROOT\demo" --id demo-001 `
  --report '第01轮_W报告.md' --nonce '<本轮唯一码>'
```

C 侧有界等待，拿到后审阅，再确认：

```powershell
python $BRIDGE --config $CONFIG wait --project '...' --id demo-001 --report '第01轮_W报告.md' --nonce '<码>' --timeout 55
python $BRIDGE --config $CONFIG ack  --project '...' --id demo-001 --report '第01轮_W报告.md' --nonce '<码>' --event-sha256 '<上一步返回>' --decision accepted
```

`wait` 单次 1–55 秒，硬上限就在这个参数上。它可以在 W 还没写报告时就启动：事件不存在就继续等，到点返回 `idle`；事件已经存在但报告缺失或摘要不符，则直接报错，不会把这两种情况混成 `idle`。`publish` 和 `ack` 要求报告已经存在。

事件绑定身份 + nonce + 报告摘要 + 事件摘要；报告一旦发布就被摘要锁定，改了会拒绝；同一个 id 换内容不会覆盖旧事件。`ack` 只记录“C 已看过哪个事件”，不代替 C 判断成果。

## 5. 连续多轮

```text
C: submit(第 N 轮)
   wait(55s) → idle 就继续 wait（累计不超过本轮预算）
   → report_ready: 读报告、核对 event_sha256、核对产出文件
   → ack(accepted | needs_revision)
   → 写第 N+1 轮任务单（新 id、新 nonce）→ submit
```

约束（靠约定，不靠守护进程）：

- 每轮业务轮次用新 id、新 nonce；已发布报告不原地覆盖。
- **停止条件写进启动语，不靠临场判断**：本轮目标达成、C 给出可核对证据后，交 L 验收并停止新增派工；只有计划内确实还有未完成的轮次时才派下一轮；验收不通过只开修正轮。不因为“这轮通过了”就继续无限续派。
- 建议 3 轮内收尾；`wait` 的 55 秒上限是刻意设计，外层超时打断也不会留下半开状态。
- 没有可用权限、遇到登录或授权提示时，按本机正常流程处理，不能承诺全程无人介入。
- 工具不提供常驻后台、不写登录启动项。C 空闲时的唤醒能力属于本机已有的另一套机制，**本次公开适配器先交付“C 持续在线”这一种模式**，没有随包提供唤醒适配器。

## 6. 出错怎么恢复

| 报出 | 含义 | 怎么做 |
| --- | --- | --- |
| `unsupported_schema` | 表缺失，或缺/多列、类型/default 有变 | 停手核对版本；不要自己改库结构 |
| `schema_ddl_mismatch` | 列看起来一样，但建表语句里有额外的 CHECK、生成列或别的约束 | 不接受；确认软件版本后重新审阅 profile |
| `unexpected_unique_index` | 表上多出 profile 之外唯一索引，写入语义已变 | 不接受；先查清索引来源 |
| `unsupported_profile` | 配置里的 profile 名不在仓库内的已审阅清单 | 用当前 profile，不要自造名字 |
| `schema_pin_mismatch` | 软件升级或换了数据库 | 重新 `init` 预演并审阅，确认后 `--write` |
| `unexpected_automation_trigger` | 表上出现 trigger | 不接受该桥，交人判断来源 |
| `config_source_root_mismatch` | 配置里的公开源码目录与实际代码目录不一致 | 用实际目录重写配置，别只改字符串绕过 |
| `receipt_path_escapes_project` | `.cw/native-dispatch` 被链接指到项目外 | 不写库；先处理该链接 |
| `backup_dir_not_empty_use_new_directory` | 备份根已有内容 | 换一个新的专用目录，不要清理原有内容 |
| `config_exists_with_different_content` | 同一路径已有不同配置 | 先确认哪份是对的，不要覆盖 |
| `discover_ambiguous_input` / `discover_requires_config_or_database` | 发现命令给了两个输入或没给输入 | 绑定前用 `--database`，绑定后用 `--config`，只给一个 |
| `owner_reference_not_verified` | 参考任务缺失/未确认/已删除 | 用 `discover` 重选你自己的两个任务 |
| `owner_reference_conflict` | 两个参考任务不同账号 | 重选同一账号下的两个 |
| `owner_binding_mismatch` | 两个参考任务被换到别的账号（即使彼此一致） | 拒绝；重新审阅账号再 `init` |
| `task_already_registered_use_status` | 同 id 已登记 | 只用 `status` 查，不换 id 重投 |
| `existing_id_content_conflict` | 同 id 内容不同 | 新轮次用新 id，旧记录不动 |
| `report_already_exists_do_not_replay` | 报告已存在，说明这轮跑过 | 查回执与原报告，不重放 |
| `backup_acl_unavailable` / `backup_acl_unverified` | pywin32 缺失或 ACL 读回不符 | 不写库；修好环境后重试同一 id |
| `backup_integrity_failed` | 备份 `quick_check` 失败 | 不写库；检查磁盘后重试同一 id |

统一原则：**投递结果不确定时先查原 id**；**不自动恢复整库备份**（备份仅供人工核对，恢复是高风险操作，需另行明确同意）；**不复制、不改动别人的凭据或权限**。

## 7. 兼容边界

- 只在 Windows 上验证过；`submit` 依赖 Windows ACL 回读，其它平台没有降级路径。
- 只支持 `workbuddy-5.9.3-automations` 这一种表结构，且要求该表没有 trigger、没有额外的唯一索引。
- 需要 WorkBuddy 的本地调度器按原生方式执行任务；桥只登记定义，不执行任务、不伪造运行记录。
- 请求模型 ≠ 实际执行模型：实际型号必须在该次原生运行记录里核对。
- 免费/额度情况随平台和账号变化，本仓库不做“长期免费”的承诺。

## 8. 离线自测

测试只用合成数据，不读真实数据库：

```powershell
$env:TRIAD_BRIDGE_TEST_ROOT = 'D:\scratch\triad-tests'   # 必须显式给出，且不能在仓库内
python -B -m unittest discover -s tools\tests -t tools\tests
```

样本目录默认保留，方便跑完回看。要清理时单独一步：

```powershell
python tools\tests\cleanup_test_root.py          # 只列出会删什么
python tools\tests\cleanup_test_root.py --yes    # 确认后删除 case-* 目录
```

清理脚本先逐个核对目标再列清单：只有仍解析为自己选定根目录直接子目录的真实目录才会被列入。盘根、仓库内的路径、junction、符号链接和 reparse point 一律跳过并给出原因，`--yes` 删除前还会再核对一次，中途发现变化就停下。默认永远只列清单。
