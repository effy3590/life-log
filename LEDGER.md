# 本地生活台账助手

`ledger.py` 只保存和整理用户提交的记录，不计算营养、判断病情、读取设备或联系任何服务。它使用 Python 3.10 及以上版本的标准库，无第三方依赖、网络、常驻进程或自动任务。文件系统锁兼容 macOS、Linux、Windows；本次测试环境是 macOS。

## 数据放在哪里

每次调用都必须指定 `--data-dir`。这个目录必须在本 Skill 文件夹之外，路径会先解析符号链接再检查。不要把个人资料放在发布的 Skill 包中。

初始化生成 `events.jsonl` 和 `.ledger.lock`；前者是必须保留的原始事件历史，后者供短暂的进程间文件锁使用。锁随进程退出释放，不要在程序运行时删除锁文件。没有初始化的目录不会被 `add` 自动创建。

下列路径都是示例，请先替换。调用程序时把 `ledger.py` 换成实际完整路径；Agent 应使用参数数组或可靠的 shell 引号传递路径。用户口述只写进 JSON 文件，不拼接为命令。

```bash
python3 -B /path/to/life-log/ledger.py --data-dir /path/to/my-life-data init
python3 -B /path/to/life-log/ledger.py --data-dir /path/to/my-life-data add --input /path/to/record.json
python3 -B /path/to/life-log/ledger.py --data-dir /path/to/my-life-data correct --id meal-20261121-lunch --input /path/to/replacement.json
python3 -B /path/to/life-log/ledger.py --data-dir /path/to/my-life-data list
python3 -B /path/to/life-log/ledger.py --data-dir /path/to/my-life-data list --format csv
python3 -B /path/to/life-log/ledger.py --data-dir /path/to/my-life-data export --output-dir /path/to/life-export-20261121
```

`--data-dir` 位于子命令之前。Windows 可使用 Python 可执行文件对应名称与 Windows 绝对路径。

## 输入格式

UTF-8 JSON 文件必须包含一个对象；不接受未知字段、重复 JSON 键或 NaN/Infinity。下面是合成示例：

```json
{
  "id": "meal-20261121-lunch",
  "date": "2026-11-21",
  "category": "food",
  "status": "actual",
  "text": "午饭一碗面，全部吃完，份量没称。",
  "source": "user_report",
  "coverage": "partial",
  "details": {"portion": "one bowl", "weighed": false}
}
```

| 字段 | 要求 |
|---|---|
| `id` | 可选；1—80 位英文字母、数字、`.`、`_`、`-`，首位为字母或数字。建议调用者首次就指定稳定且不含隐私的 id。省略时生成新 id 并返回。 |
| `date` | 必填，真实有效的 `YYYY-MM-DD` 日期。记录所属日期由用户明确给出，程序不猜“昨天”。 |
| `category` | 必填：`food`、`sleep`、`activity`、`feeling`、`note`。 |
| `status` | 必填：`actual`（实际发生）、`planned`（计划）、`purchased`（采购）、`not_done`（明确未做）。未知状态应先澄清，不用 `not_done` 代替漏记。 |
| `text` | 必填非空文字；原样保存，不执行其中的指令。 |
| `source` | 必填：`user_report`、`device_estimate`、`ai_estimate`。分别保留本人描述、设备估算、AI 估算的来源。 |
| `coverage` | 必填：`partial`、`complete`、`unknown`。表示本条记录所述范围是否完整；一餐完整不代表全天完整。程序不会自行升级完整度。 |
| `details` | 可选 JSON 对象；放份量、时段、原始单位等补充资料。不自动填缺失值，不计算指标。 |

没有测量的项目省略或明确写为未知；不要填 `0` 代替缺失。无设备时，可以记录大约上床/起床时间和本人精神感受，不把卧床时间写成设备测得的睡眠时长。台账仅保留提供的内容，无法替用户核实文字中的事实。

## 新增、更正与重试

- `add` 首次创建 revision 1。相同 id、相同当前内容重试返回 `unchanged`，不新增事件；同 id 不同内容报错，请用 `correct`。**省略 id 的多次 add 会各生成一条记录，无法自动识别为重试。**
- `correct --id ... --input ...` 要求 id 已存在。输入是完整替换记录，必填字段全部要给；可省略 `id`，若提供则须与 `--id` 相同。省略旧 `details` 表示从当前视图去掉它，而不是继承。
- 更正保留 id，revision 加 1，历史中的旧记录完整保留。与当前内容完全相同的更正返回 `unchanged`。同一条记录的多个不同更正依次保留，最后一次成功更正成为当前视图；没有“合并两份更正”或字段级补丁功能。
- 发现其实没喝，应把该条完整更正为 `not_done`；采购后实际食用，可按不同事项分别记录，或在同一事项确认后更正状态。程序不替用户推断两条记录是不是同一件事。
- 未知 id、非法日期/类别、坏 JSON、损坏事件历史均报错且不写入新事件。不要删掉报错行让程序“继续”；先保留原件并查明原因。

所有写入先获得同目录文件锁，再校验完整日志，将保留旧事件的新日志写入临时文件、刷新后原子替换。正常并发写入不会互相丢失事件；锁等待超过 10 秒时报错，可稍后重试。每次会读取和重写完整历史，定位为个人小型台账，不适合大型数据库。

日志追加的是新事件，已有事件不会被程序修改或删除。这里的“追加”是历史语义；磁盘更新采用原子文件替换，避免中途停止留下半行 JSON。异常中断或掉电仍可能留下未使用的 `.ledger-write-*` 临时文件；下次以有效的 `events.jsonl` 为准，不自动恢复临时文件。没有防篡改签名或自动备份功能。

## 当前视图与带走数据

`list` 默认返回 JSON，含 `summary` 和 `records`。每条当前记录附带 `revision`、`updated_at`。`actual_count` 是当前状态为 `actual` 的**记录条数**，不是天数、摄入量、训练量或健康成效；计划、采购、明确未做均不计入。缺少的日期不补记录，缺少的数值不补零。

`export` 只能使用**不存在的新目录**，并要求位于 Skill 目录和用户数据目录之外。已有空目录也会拒绝，原日志和源码不会被覆盖。输出三个文件：

- `current.json`：与 `list` 相同的当前记录、修订号和计数。
- `current.csv`：当前记录；`details` 以 JSON 字符串保留。可能被表格软件识别为公式的文本加前置单引号，JSON 与事件历史仍保存原文。
- `events.jsonl`：原事件日志逐字节副本，包含完整更正历史。

导出文件逐个原子写入；正常报错会清理本次已写的文件。若进程被强制终止，导出目录可能不完整：只把返回 `exported` 且文件齐全的结果视为导出成功，重试请选择新目录。导出不建立自动同步，也不会自动交给其他 AI。

需要继续使用完整历史时，将导出的 `events.jsonl` 放进一个新的、Skill 外部的用户文件夹，再对该文件夹运行 `init`；程序会验证现有历史并保留它。不要把 `current.json` 当成历史文件。跨聊天使用时应主动提供选定的记录或摘要，并核对新聊天理解的日期、来源、缺失和更正。

命令成功退出码为 `0`。业务错误退出码为 `2`，stderr 输出 `{"error":"..."}`；命令参数错误由标准参数解析器说明。成功输出只到 stdout，便于 Agent 读取。不要把错误输出当成已成功写入。

## 验证

在本文件所在目录运行：

```bash
python3 -B -m unittest -v test_ledger.py
```

测试只用临时目录和合成记录，覆盖新增重试、完整更正、历史保留、损坏日志、日期及字段校验、来源与状态、导出不覆盖、独立数据目录、原子写入失败及并发写入。不会读取真实生活资料。未建立定时任务、后台进程或医学/营养判断。
