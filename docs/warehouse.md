# 可修订许可观测 SQLite 时段事实仓 v1（warehouse 契约）

本模式回答操作问题：**某个调用方明确标识的组件，在一个 UTC 时间窗口内，被观测为
allowed / blocked / unknown 各持续多少秒**。它不做许可证法律判断；状态含义完全沿用
旧单次分析链路（`model.analyze` 对单组件 `id`+`expression` 在给定 `policy` 下的
allowed/blocked 判定）。`component_id` 始终由调用方提供，仓内不推断组件谱系。

三个 CLI 模式互斥，非法组合退出 2：

| 模式 | 参数 | 产物 |
| --- | --- | --- |
| 旧单次分析 | `--source FILE --output REPORT` | 原子发布的旧结构报告，值不变 |
| 观测导入 | `--warehouse DB --observations FILE` | 仅 stdout 输出 JSON 回执；不接受 `--output` |
| 窗口查询 | `--warehouse DB --window FILE --output REPORT` | 原子发布的窗口报告 |

输入文件、数据库、输出文件解析后路径必须两两不同；数据库与报告路径是符号链接一律拒绝。
窗口模式不会创建数据库：文件缺失、非 SQLite 文件或表结构不符都清楚报错且不覆盖原文件。
所有输入沿用严格 JSON：重复 JSON 键、非有限数、未知字段拒绝，单文件 ≤ 8 MB。

## 1. 观测导入文件契约

根对象**恰含** `observations` 一个字段，其值为数组，可空，每批最多 10000 项
（且整个 JSON 仍受 8 MB 上限约束）。

### put 行（恰含 7 个字段）

`event_id`、`revision`、`component_id`、`action`、`effective_at`、`policy`、
`expression`，其中 `action` 必须为字符串 `"put"`。

* 标识符（`event_id`、`component_id`、policy 许可证 id、义务名）全部沿用 `ident`：
  非空、去空白后等于自身、长度 ≤ 128、无控制字符。
* `revision` 为 1..1_000_000_000 的整数；`effective_at` 为 0..4_102_444_800
  （2100-01-01T00:00:00Z）的 UTC Unix 整数秒。布尔值（`true`/`false`）一律拒绝，
  浮点、字符串、null 同样拒绝。
* `policy` 沿用旧规则：最多 200 条，每条恰含 `license`、严格布尔 `allowed`、
  唯一 `obligations`（≤50）；`expression` 沿用旧 SPDX 语法与候选上限
  （WITH 是完整独立政策键，未知许可阻断）。
* 调用方**不能**直接给 `status`：仓内把 `component_id`/`expression` 作为一个组件
  交给现有 `analyze`，所得 allowed/blocked 即本行观测状态。

### retract 行（恰含 4 个字段）

`event_id`、`revision`、`component_id`、`action`，其中 `action` 必须为
`"retract"`。retract 行不携带、也不允许出现 `policy` 或 `effective_at`。

### 版本与事件决议

* 不可变版本键为 `(event_id, revision)`；同一 `event_id` 下 `component_id` 固定，
  冲突整批拒绝（即使两个版本同批到达）。
* 每个事件只有最大 `revision` 生效；最大版本为 retract 时整个事件不提供状态，
  旧版本永不复活。较小版本迟到仍保存，但不覆盖较大版本。
* 同版本幂等：同 `(event_id, revision)` 的规范化业务载荷 JSON 值相等即视为重复
  （对象键顺序、输入空白无关；数组顺序保留；数字按已校验整数比较；retract 无
  policy 与时刻，载荷固定相等）。载荷不同则整批拒绝——**即使该版本已被更大版本
  取代，冲突仍须检出**。
* 新事件的第一条记录可以是 retract；之后同 `component_id` 的更大 put 版本可重新
  生效。同批可含任意顺序的多个版本与重复版本。

### 生效观测的时间线语义

* 生效 put 按 `effective_at` 排列；同组件同一时刻只取 `event_id` 字典序最大的事件。
  该胜者被撤回后，同刻次大事件自动显露。
* 状态自观测时刻延续到下一观测，半开区间 `[a,b)` 的右端不属于前一状态；最早观测
  之前为 unknown；窗口起点之前的最后一个有效观测继续延续进窗口。
* 更正一个时刻会同时修复原位置与新位置的邻接区间；跨 UTC 自然日的移动在两个日的
  分摊中同时生效。
* blocked 是正常业务状态，导入成功退出 0。曾接受过记录的组件持久存在于组件目录；
  全部事件撤回后全程 unknown，不会从查询目录消失。

### 导入回执（stdout，恰含 5 个字段）

```json
{"schema_version": 1, "source_sha256": "...", "generation": 7,
 "inserted": 2, "duplicates": 1}
```

* `source_sha256` 是本次输入**原始字节**的 SHA-256。
* 持久 `generation` 初值 0；一次事务接受至少一条此前不存在的
  `(event_id,revision)` 才加 1。纯重复批次与空批次不加；接受迟到旧版本仍加。
* `inserted` 为本批新增的唯一版本数；`duplicates = 数组总行数 - inserted`
  （同批内部相同版本的重复行也计入 duplicates）。
* 任何校验或写入错误：退出 2，整批回滚，已接受版本、组件归属、区间与 generation
  保持原样。

## 2. 窗口查询文件契约

根对象**恰含** `components`、`start`、`end`。

* `components`：至多 1000 个不重复的精确组件 id，可空；每个 id 必须已存在于仓库
  组件目录，未知 id 退出 2。
* `0 <= start < end <= 4_102_444_800`，整数秒、拒绝布尔；窗口长度 ≤ 366 天
  （31_622_400 秒）。

报告恰含 `schema_version=1`、`source_sha256`（窗口文件原始字节摘要）、`generation`
（查询只读事务读到的代次）、`result`。`result` 恰含：

* `start`、`end`：回显输入。
* `components`：按 id 升序，每项恰含 `id`、`allowed_seconds`、`blocked_seconds`、
  `unknown_seconds`，三者为精确整数且总和恰为 `end-start`。
* `days`：覆盖与窗口相交的每个 UTC 自然日，按 `YYYY-MM-DD` 升序；每项恰含 `day`
  与三种秒数字段，汇总**所有被选组件**在该日与窗口交集内的时长；每天三项合计
  = 组件数 × 该日交集秒数。
* 空 `components` 仍输出覆盖窗口的全部日期，三项均为 0。unknown 绝不并入
  allowed/blocked；时长按状态区间积分，不按扫描记录条数计数。

## 3. SQLite 存储设计与一致性义务

```
schema_meta(key TEXT PRIMARY KEY, value TEXT)   -- schema_version、generation
components(component_id TEXT PRIMARY KEY)       -- 持久组件目录，只增不删
observations(event_id, revision, component_id, action, effective_at,
             policy_json, expression, status,
             PRIMARY KEY(event_id, revision)) WITHOUT ROWID
intervals(component_id, start_ts, end_ts, status,
          PRIMARY KEY(component_id, start_ts)) WITHOUT ROWID
```

* `observations` 是只追加的版本事实表，是唯一保存历史的表；一个版本写入后不再
  更新或删除。`policy_json` 保存规范 JSON（键排序、紧凑分隔），它同时是同版本
  幂等/冲突判等的载荷组成。
* `intervals` 是与当前生效观测等价的物化状态时间线：每个组件的区间互不重叠、首尾
  相接，最后一条 `end_ts IS NULL` 开放延伸；零时长段与相邻同状态段被合并，因此
  每行都代表真实状态秒数。窗口查询只消费 `intervals`、`components` 与
  `schema_meta`，**绝不回读 `observations` 重演版本历史**（`EXPLAIN QUERY PLAN`
  只显示按主键检索 intervals / components）。
* 一次导入在单个 `BEGIN IMMEDIATE` 事务内完成：版本决议 → 事实插入 → 组件目录
  upsert → 受影响组件区间重算 → generation 更新，最后一次 COMMIT。不存在“先提交
  事实、再另一次提交区间”的窗口；失败整体 ROLLBACK。
* **局部更新**：受影响集合 = 本批涉及事件归属的组件，且仅当确有新版本插入时才
  重算。重算只读该组件自己的事实行并整体替换该组件的区间行；其他组件的版本表与
  区间表既不遍历也不改写。纯重复批次不触碰任何派生数据、不增加 generation。
* 区间重算与事实决议使用同一套规则（最大 revision、retract 清空事件、同刻最大
  event_id、半开邻接与同状态合并），所以旧时段的保持、新时段的形状与按日汇总三者
  由同一份当前胜者集合导出：区间是胜者集合的无损分段表示，日汇总是区间与窗口/UTC
  日界的裁剪积分，任一层都不另存可能漂移的副本。
* 查询在只读事务（WAL 下 `mode=ro` 连接 + `BEGIN`）内读取固定快照；期间写事务
  提交不影响进行中的查询，新事务才观察到新 generation。

测试中的独立 oracle 从 `observations` 原始事实重新决议赢家并按秒积分，不导入任何
生产区间维护或聚合代码；随机乱序历史、批次置换与 20k/200k 规模数据均要求与 oracle
逐组件、逐日一致。故障注入钩子在“事实已写、区间未算”处抛错，验证事实、代次、
组件目录与查询值全部回滚到导入前。

## 4. 命令行样例

```sh
license-planner --warehouse run/demo.db \
  --observations fixtures/warehouse-observations.json
license-planner --warehouse run/demo.db \
  --observations fixtures/warehouse-corrections.json
license-planner --warehouse run/demo.db \
  --window fixtures/warehouse-window.json --output run/window-report.json
```
