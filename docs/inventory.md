# 离线多源许可清单归并管道（inventory）数据契约 v1

`--inventory MANIFEST --output REPORT` 是与旧 `--source/--output`、warehouse
两条链路互斥的第四个 CLI 模式。它只处理调用方已经收集好的确定性证据：claim（某
来源对某组件的一条完整表达式声明）与 alias（调用方明确给出的同一身份边）。管道
**不推断候选身份匹配、不访问网络、不作任何许可证法律判断、不更改任何持久观测事实**，
临时索引仅在本次调用的临时工作区中存在，调用结束（成功或失败）即清理。

## 1. MANIFEST 与路径契约

MANIFEST 是单个严格 JSON 文件，恰含 `policy`、`files` 两个字段，沿用既有严格读取
规则：原始字节上限 8 MB、重复 JSON 键拒绝、`NaN`/`Infinity` 等非有限数拒绝、未知/
缺失字段拒绝。其原始字节 SHA256 进入报告头 `source_sha256`。

* `policy`：与基线逐组件分析完全相同的策略数组（最多 200 条），每条恰含
  `license`、`allowed`（严格布尔）、`obligations`（最多 50、去重）；许可键是单
  ID 或完整 `ID WITH ExceptionID`，WITH 为独立键，绝不继承基础许可。
* `files`：相对 MANIFEST 所在目录的 NDJSON 分片路径数组，最多 64 项，可为空。
  路径必须是非空相对路径，不接受绝对路径、目录、空路径或词法解析后越出清单目录
  的路径（含符号链接解析后越界）；分片必须是已存在的常规文件。数组解析后须互异。
* MANIFEST、任一分片、REPORT 的解析后绝对路径必须两两互异；REPORT 目标若是符号
  链接直接拒绝。

## 2. NDJSON 分片与逐行严格校验

* 必须为 UTF-8；空白行（仅空白字符）忽略；最后一行没有换行合法。
* 单行原始字节上限 65536，**存在行尾时把行尾字节计入**；无行尾的最后一行同样不得
  超过该上限。超长行在流式读取阶段即拒绝，不会被整体读入内存。
* 每条非空行必须是一个 JSON 对象，严格拒绝重复 JSON 键、非有限数、未知/缺失字段、
  错误类型、非法标识符及表达式错误。`kind` 只能是 `claim` 或 `alias`。
* claim 行恰含 `kind`、`source`、`id`、`expression`；alias 行恰含 `kind`、
  `left`、`right`。端点对象恰含 `source`、`id`，两者均沿用 `ident` 规则。
* 身份按 `(source, id)` 二元组比较，绝不拼接字符串；同一局部 id 在不同 source 下
  是不同端点，除非有明确 alias 否则永不自动合并。
* `expression` 沿用既有 `parse`/`alternatives` 的全部语法与候选数量限制（每节点
  256 候选、AND 笛卡尔积 4096、token 上限等）。
* 分片总字节按**原始字节**计（含空白行与行尾），上限 2 GiB；既有 8 MB 整文件上限
  不适用于分片。claim 上限 1,000,000 条，alias 上限 2,000,000 条。

## 3. 输入完整性规则

* 所有 claim 的 `(source,id)` 必须全局唯一；同一端点再次出现（即使 expression 完全
  相同）是整批输入错误。
* 每条 alias 的两个端点最终都必须有 claim；允许前向引用、重复边、自环、环，但全
  部分片读完后仍悬空的引用使整个调用失败（退出 2）。
* 无 claim 且无 alias 的空清单合法，输出仅含 header。

## 4. 实体、共识与冲突核

**实体**：alias 是无向边，其传递闭包（并查集）决定实体；实体键为实体中按
`(source,id)` 升序的最小端点。边的输入方向不影响归并；即使别名在最后一个分片才
出现，先前证据也必须合并到最终实体中。晚到别名把两个原本 allowed 的实体合并后，
若完整交集为空，合并实体为 conflict，原两个实体不再单独发布。

**候选族**：每条 claim 的候选是既有 `alternatives` 生成的完整许可集合族；集合内
许可键按 Python 字符串升序，保留完整 WITH；候选与集合均去重，候选族按许可列表的
字典序排列为唯一列表。`MIT OR MIT` 与 `MIT` 由既有 alternatives 产生相同候选族，
因而等价；**不做包含关系消减**，`{MIT}` 与 `{MIT,Apache-2.0}` 是两个不同完整候选。

**共识**：实体共识是全部成员候选族的**精确交集**——只有某个完整许可集合同时出现
在所有成员的候选族中才算共识。不做集合并集冒充一致，不先按 policy 过滤再求交，不
按逐成员 selected/allowed 状态或两两交集推断。

**策略判定（仅共识非空时）**：对共识候选复用基线逐组件规则——候选中每个键都在
policy 中且 allowed 才进入 `alternatives`（义务取并集，selected 按义务数、许可数、
许可列表字典序最小），否则进入 `rejected`（含 `blocked_or_unknown` 原因）。未列入
policy 的许可键保留在共识中并按未知许可阻断。有允许候选状态为 `allowed`，否则为
`blocked`；共识为空才是 `conflict`。blocked 与 conflict 是正常业务结果，退出 0。

**冲突证据核（core）**：仅 conflict 实体输出。从实体最终的全体成员开始，按端点
**降序**逐个尝试移除；只有剩余成员（以当前尚保留成员为准，而非最初全体或某个临时
子组）的候选族交集仍为空，才永久移除。排序只依据 `(source,id)`，与文件位置和读取
顺序无关。空成员族不视为冲突。保留下来的端点升序输出为 `core`，其性质可独立复核：
core 自身候选族交集为空，且删除其中任一成员后交集非空；不要求最小基数核，禁止抽样、
任意对子或某次并查集合并轨迹代替规定结果。实现用候选出现计数把逐次试删等价为线性
过程，规则与字面试删完全一致。

## 5. 报告行结构（原子发布的 UTF-8 NDJSON）

首行是 header，恰含 `kind="header"`、`schema_version=1`、`source_sha256`（MANIFEST
原始字节 SHA256）、`files`（按规范化相对路径字符串升序，每项恰含 `path`、`sha256`，
摘要覆盖分片全部原始字节）。

之后每个实体先输出一个 group 行，再紧随该实体每个成员的一条 member 行：

* group 恰含 `kind="group"`、`entity`、`member_count`、`status`、`selected`、
  `alternatives`、`rejected`、`core`。`entity` 与 `core` 的每个端点都恰含
  `source`、`id`。`alternatives`/`rejected`/`selected` 复用基线逐组件结果的对象形状
  与排序规则，只处理共识候选。allowed/blocked 时 `core=[]`；blocked 时
  `selected=null`；conflict 时 `selected=null` 且两份候选列表均为空。
* member 恰含 `kind="member"`、`entity`、`ref`、`expression`；`expression` 保留输入
  JSON 字符串值；成员按端点升序。实体按 entity 端点升序。空清单仅输出 header。
* 每行使用 `sort_keys=True`、`ensure_ascii=False`、`allow_nan=False`、
  `separators=(',',':')` 序列化并以 LF 结束。完整报告不输出到 stdout；成功时 stdout
  只打印一个小型完成回执（groups/claims/aliases/shards/shard_bytes 计数），其大小不
  随报告规模增长。
* 报告先写入同目录临时文件并 fsync，全部身份、重复 claim、悬空引用与许可校验通过后
  才以一次 `os.replace` 原子替换。任何输入错误退出 2 且旧 REPORT 逐字节保留，临时
  报告与临时工作区均被释放，不会留下可被当作成功数据产品的半份报告。

## 6. 规模与外存设计

硬指标：至多 1,000,000 claim、2,000,000 alias、2 GiB 分片、单行 65536 字节；单进程
峰值 RSS 不超过 128 MiB，可使用临时磁盘，不得把内存工作集转移到另一进程。

实现方式（`src/license_planner/inventory.py`）：

1. **分片流式读取**：1 MiB 分块扫描，按行硬截断，任何超长行都不会整体入内存；字节
   上限按原始字节累计。
2. **磁盘索引**：端点、claim、边与重组后的实体成员表都在临时目录的 SQLite 中
   （page cache 受限、mmap 关闭、大排序/哈希连接溢出到磁盘）。
3. **紧凑并查集**：Python 侧唯一常驻索引是 `array('i')` 整数数组（parent+size），
   支撑前向引用、重复边、自环、环与最终实体重映射，内存 O(端点数) 字节而非 Python
   对象。
4. **流式分组计算**：实体共识按组流式求交，core 两遍流式扫描（计数 + 确定删除），
   单组只持有该表达式至多 256 个候选；报告逐组直接序列化到临时文件，任何时候都不
   组装全量 member/group 数组或巨型候选字典。
5. **稳定输出**：所有次序由端点二元组与候选列表的 Python 字典序决定，与文件/行
   顺序无关；置换分片与行顺序，除 header 摘要外业务输出逐字节相同。

## 7. 验收样例与测试

* 规范三证据样例：成员端点 `(s,a)`=`MIT OR Apache`、`(s,b)`=`MIT OR BSD`、
  `(s,c)`=`Apache OR BSD`，跨分片晚到别名连成一个实体：两两交集非空而整体
  conflict，core 恰含 a、b、c；再加 `(s,z)`=`MIT OR Apache OR BSD` 冗余成员，按
  规定删除后 core 仍恰含前三条，`status=conflict`、`selected=null`、
  `alternatives=[]`、`rejected=[]`。
* 另含共识非空但许可未列入 policy 的 blocked 实体，以及存在允许共识候选的 allowed
  实体，防止 conflict 与 blocked 混用。见 `fixtures/inventory-example/`。
* 测试：
  - `tests/test_inventory_oracle.py`：独立小规模 oracle（自建无向可达分组、独立
    表达式枚举、整组求交、字面逐次删除核），含 120 组随机差分场景，覆盖两两相容/
    整体矛盾、冗余证据、多个冲突核的确定删除次序、非首成员决定实体键、同名异源、
    分隔字符碰撞、WITH、未知许可、重复 claim、悬空 alias、重复边、自环、空分片、
    坏 UTF-8、重复键、越界路径、输出/输入重合、符号链接边界与文件/行置换。
  - `tests/test_inventory_cli.py`：CLI 传输、严格校验、路径契约、符号链接、旧报告
    逐字节保留、写临时报告后替换前失败注入及临时工作区释放。
  - `tests/test_inventory_scale.py`：固定种子生成 200,000 claim、400,000 alias、
    >128 MiB 的 8 分片数据（多 source、前向引用、同名异源、2 万成员大实体、晚到环、
    冲突核、blocked 与 allowed 实体），在独立子进程实测 `ru_maxrss` 峰值 RSS
    （Linux 单位 KiB）并核对可证明的组数、共识与 core。
