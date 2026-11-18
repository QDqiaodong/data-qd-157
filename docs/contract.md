# Caller-policy SPDX expression choices v1

软件交付整理者输入policy与components，可选输入release、dependency_graph、reconciliation；层叠交付审计使用独立的 `--layers` 入口（见下）。policy的license为单许可证ID或“ID WITH ExceptionID”，allowed严格布尔、obligations唯一字符串；最多200条。component恰含唯一id及expression；无release且无dependency_graph时最多1000个，有release时最多120个，有dependency_graph且无release时最多30000个。根对象只允许policy、components及可选release、dependency_graph、reconciliation，其他字段均为输入错误。

解析大小写敏感的ID、WITH、括号、AND、OR，优先级WITH>AND>OR，WITH只能紧跟许可证ID。许可证ID语法支持字母数字点加号连字符。不是完整SPDX文档解析、许可证目录认证或法律建议；WITH为完整独立政策键，绝不自动继承基础许可。

AND合并所需许可，OR保留可选集合；每节点最多256集合，AND笛卡尔积最多4096，否则整批拒绝。未知或显式禁止的许可阻断该路径。允许路径义务取并集；逐组件旧selected按义务数最少、许可数最少、许可列表字典序最小选择。逐组件提供所有许可选择、拒绝原因与选中义务。

## 交付批次共享义务计划（release）

release恰含obligation_costs字段，无其他字段。obligation_costs为对象数组，每个对象恰含obligation与cost：obligation沿用ident（policy义务名），cost为0..1000000000的整数，不接受bool。集合必须不重不漏地覆盖allowed=true的全部policy义务（去重后）；未知义务、重复义务、缺项或多余字段均为输入错误。去重后允许义务最多16种，超过则整批拒绝；表达式与每节点候选上限继续有效。

release_plan为每个组件选一个既有allowed许可候选，按四级字典序精确最小化：(1)所选义务并集的成本之和；(2)并集义务种数（零成本义务仍计入）；(3)各组件选中许可数量之和（不同组件的相同许可分别计数）；(4)按组件id升序的许可列表序列，各列表自身升序，按Python字符串/列表字典序比较。同一义务只准备一份材料、成本只计一次；批次结果不是逐组件selected成本之和。候选不被提前丢弃，必须精确最优，不接受局部最优、近似或未证最优的超时答案。

无组件阻断时，result新增release_plan，恰含status、blocked_components、total_cost、obligation_count、license_occurrences、selections、obligations。status为allowed，blocked_components为空列表；三个数值对应最优目标前三项。selections按id排序，每项恰含id、licenses、obligations，展示所选候选。obligations按obligation排序，每项恰含obligation、cost、components；components为实际需要该义务的所选组件id升序去重列表。证据与选择相互一致，每项成本只计一次。空components是合法的allowed空计划：三个数值为0，selections与obligations为空。

有组件无allowed候选时，release_plan.status为blocked，blocked_components为全部阻断组件id升序列表，三个数值为null，selections与obligations为空，不输出部分计划；result.compliant与旧components报告照常给出，属正常业务结果，CLI退出0。

没有release时result结构与值保持原样。有release时旧components的selected仍按旧规则计算，不作为批次计划。

## 供应依赖单点瓶颈审计（dependency_graph）

根对象可选dependency_graph，恰含roots与edges两个字段，无其他字段。roots为唯一组件id数组（可为空）；edges每项恰含dependency与dependent两个字符串字段，表示前者可沿供应边供应到后者。所有roots与边端点必须精确引用既存组件id；重复root、重复有向边（dependency,dependent完全相同）拒绝；自环与有向环合法，不视为输入错误。仅当存在dependency_graph且不存在release时，components上限放宽到30000，edges上限120000；存在release时仍受120组件上限约束。8MB输入上限、重复键与非有限数值等旧约束继续有效。

审计范围是从任意root沿供应边可达的全部组件（roots本身计入）。对范围内组件x，停用x表示删除x及其相连边，其余root仍独立供给；lost_count(x)是原审计范围中停用后变为不可达的组件数，包含x本身。严格支配者d是满足d≠x且每条从任意root到x的有向路径都经过d的组件；immediate_dominator是离x最近的严格支配者，不存在则为null。多根、循环、自环与旁路必须精确：根之间可互达、根可达另一root等情形均不得把祖先、可达前驱、最短路径节点或强连通分量代表误判为支配者。

有dependency_graph时result新增dependency_audit，恰含roots、unreachable、nodes。roots与unreachable均为组件id升序列表；unreachable是全部组件中不在审计范围者（含未被任何边或root引用的孤立组件）。nodes按id升序且只覆盖可达组件，每项恰含id、immediate_dominator、lost_count、blocked_dominator_count、nearest_blocked_dominator。后两者统计每条root路径都必经的blocked组件（blocked状态只来自旧许可判断），包含x自身：x自身blocked时其nearest即x自身且计数至少1；nearest是离x最近的此类组件id，没有则为null。该计数不是对所有可能路径上的blocked节点求和，也不据此判断整条路径的许可合规；blocked节点及其供应边始终保留在图中，绝不预先删除。空roots时nodes=[]，全部组件列入unreachable。

lost_count、支配关系与阻断证据来自同一张完整供应图的同一可达范围：实现先在虚拟入口（连向所有roots）上用迭代Cooper-Harvey-Kennedy算法求即时支配者，再以支配树子树大小精确给出lost_count（停用x恰好断开所有被x支配的节点及x自身），不逐节点串联修改图，全部遍历均为迭代式，深链不依赖提高递归上限。没有dependency_graph时旧结果结构与值逐字保持；存在时旧components、compliant与可选release_plan的语义和值保持原样，不重新选择许可或优化义务。

## 跨版本组件谱系对账（reconciliation）

根对象可选reconciliation，恰含baseline与links两个字段，无其他字段。baseline表示此前版本，恰含policy与components，复用上面的SPDX与策略契约（policy最多200条、义务去重、allowed严格布尔；component恰含唯一id与expression），但baseline内不允许嵌套release、dependency_graph或reconciliation等任何其他分析，组件最多1000条。links为候选对应关系数组，每项恰含baseline_id、current_id与penalty：两端必须精确引用baseline或当前版本的既有记录id，penalty为0..1000000000整数（拒绝bool），重复的（baseline_id,current_id）配对拒绝，未知引用、结构错误或超限均退出2。当前版本存在reconciliation时组件同样最多1000条，links最多12000条；若同时提供release等既有可选分析，仍受其更小的组件上限（release为120）约束。两侧同名ID绝不自动配对，未提供的边不可使用；边与penalty是调用方数据，不作法律效力或版本同一性认定。

对账匹配是候选边的一个子集，要求每个baseline记录与每个current记录至多出现一次。目标严格两级：先精确最大化配对数，再在最大匹配中精确最小化penalty总和；没有字典序或其他额外择优，许可状态、旧selected及其他分析结果不参与匹配目标。对每条输入边，必须针对全部最优匹配判定：forced为每个最优匹配都包含，possible为至少一个但不全部包含，excluded为没有最优匹配包含；不得只取一份确定匹配并把其边当作forced。两侧记录若在任何最优匹配中都未配对，则分别列入必定未配对集合。

有reconciliation时result新增reconciliation_audit，恰含matched_count、minimum_penalty、links、always_unmatched_baseline、always_unmatched_current、transitions。matched_count与minimum_penalty为空匹配时为0。links按（baseline_id,current_id）升序，保留每条输入原边及penalty，每项恰含baseline_id、current_id、penalty、classification，classification只能是forced、possible或excluded。always_unmatched_baseline与always_unmatched_current各按id升序，列出在所有最优匹配中都未配对的记录（与“仅在部分最优解中未配对”不同）。transitions只为forced边输出，每项恰含baseline_id、current_id、baseline_status、current_status，按两侧ID排序，且包含全部forced边（即使两侧状态相同）；两个状态各自只来自对应版本既有的逐组件SPDX allowed/blocked判断，baseline的allowed仍按“是否存在全部许可在baseline policy中allowed的候选”确定，WITH为独立键、未知许可阻断。歧义配对不得作为确定的许可证状态迁移输出。空links合法：matched_count与minimum_penalty为0，links与transitions为空，两侧全部记录进入必定未配对列表。相同最优解集合的输入置换（links、policy、components顺序）必须产生完全相同的reconciliation_audit。

实现以一次整数最小费用最大流求出某个最优匹配，再在零约简费用残余图上以强连通分量判定每条边的三分类：最优匹配之间恰好对应零费用残余环上的交换，因此无需逐边重新求解。该分类只依赖候选边与penalty，并在单进程内对两侧各1000记录、12000边精确完成。没有reconciliation时旧结果结构与值逐字保持；有reconciliation时当前版本原有分析字段与值（compliant、components、可选release_plan、dependency_audit）均保持不变。

## 离线层叠交付物清单审计（layers）

第五条互斥 CLI 链路 `--layers FILE --output REPORT`。FILE 恰含 policy、components、materials、layers；只处理调用方给出的文件元数据，不读取镜像、压缩包、符号链接或任何真实文件内容，所有路径只在虚拟目录树中解释。源字节 SHA256 与原子发布沿用旧链路；输入/计算失败退出 2 并逐字节保留旧 REPORT，许可阻断或材料缺失是正常业务结果、退出 0。

policy 完全沿用基线严格规则（原子 ID 或 "ID WITH Exception"、allowed 严格布尔、义务唯一，最多200条）。components 每项恰含 id 与 expression；id 唯一、沿用 ident；expression 必须按既有 parse/alternatives 校验，**即使该组件最终未暴露**，非法表达式仍是整批输入错误。最多10000个组件。调用方给定 id 是唯一组件身份，不推断、不归并。

materials 每项恰含 obligation 与 sha256：obligation 沿用 ident，sha256 为64位小写十六进制；至多20000项；同一 (obligation,sha256) 重复拒绝；同一义务可有多个摘要，不要求覆盖全部义务。SHA 是调用方声明的数据，本功能不证明它对应真实文件内容。

layers 为有序数组，最多256层、合计最多100000条操作。每层恰含 id（ident、全局唯一）与 entries。entries 按 kind 严格确定字段：dir 为 {kind,path}，file 为 {kind,path,sha256,component}，hardlink 为 {kind,path,target}，whiteout 与 opaque 均为 {kind,path}；file.sha256 遵守上述格式，component 为 null 或 components 中的精确 id；目录与链接不携带组件或摘要。所有数组允许为空，空层合法。

path 与 target 为规范相对 POSIX 路径：非空、最多1024字符、最多128段、以 `/` 分隔；禁止前后斜杠、空段、`.`、`..`、反斜杠与控制字符；大小写敏感且不作 Unicode 归一化。根目录隐含存在且不可被操作。每层至多一个写入条目（dir/file/hardlink）占用同一 path，至多一个删除条目（whiteout/opaque）占用同一 path，两类可同路。禁止同层 file 或 hardlink 写入路径同时又是其他写入路径的祖先；dir 可以作为祖先。entries 的排列不影响业务结果，layers 的顺序具有业务含义。输入总量仍受8MB限制，重复键、非有限数与未知/缺失字段均拒绝。

### 层语义

每层先按**层前**目录树判定全部 whiteout/opaque 的合法性与删除集合并一次生效，再安装本层 dir 与 file，最后解析本层 hardlink。删除标记只作用于进入该层前已存在的内容：同层新文件不会被同层标记删除；opaque 只清除目录的继承子孙，保留目录本身。

- whiteout：删除继承的同路文件/目录及整个继承子树；目标不存在为无操作。
- opaque：保留该目录、移除全部继承子孙；目标不存在为无操作；若层前该 path 是普通文件（含已绑定硬链接）则整批输入错误。
- 重叠删除按层前树取并集后幂等生效，不因其他标记先执行而改变合法性。
- 安装 dir：同路继承目录继续合并，同路继承文件被目录替换。
- 安装 file/hardlink：同路继承目录连同子树被替换，同路继承文件被覆盖。
- 写入路径缺失的父目录自动建立；若父路径是继承文件，必须有本层显式 dir 写入替换它，否则整批报错。

hardlink 的 target 在该层完成全部 dir/file 写入后的命名空间中解释：指向本层另一 hardlink 时沿该层定义解析（允许前向引用与任意深度合法链），最终必须到达普通文件对象——本层 file、继承文件或已绑定链接均可。自环、环、缺失目标、目录目标均为输入错误。解析不依赖递归上限：实现使用显式栈加记忆化，10000 级前向链为线性时间。硬链接绑定该层完成时目标文件对象；之后覆盖或删除目标路径不改变已绑定链接，也不得把尚未解出的本层链接当作旧对象（本层链接路径的旧绑定在解析前先被撤换）。

### 输出

输出 JSON 根恰含 schema_version=1、source_sha256（FILE 原始字节摘要）与 result。result 恰含 compliant、files、components、obligations。

- files 仅列最终可见普通文件路径（含硬链接绑定），按 path 升序，每项恰含 path、sha256、component、origin；不输出目录或删除标记。origin 恰含 layer 与 path，分别为创建该对象的 file 条目的层 id 与路径；相同 sha256 的独立 file 条目仍是不同对象，硬链接继承同一 origin。
- components 仅列仍暴露且 component 为该 id 的组件，按 id 升序，每项恰含 id、status、selected、paths；status/selected 与该组件单独调用基线 analyze 的对应值完全一致（selected 规则不变，不重新优化许可选择），paths 为其暴露路径升序列表，blocked 时 selected=null。
- obligations 只列暴露 allowed 组件 selected 要求的义务，按 obligation 升序，每项恰含 obligation、components、satisfied、paths：components 为实际要求它的组件 id 升序列表；paths 为所有最终可见文件中摘要命中该义务任一声明材料的路径升序列表；satisfied 等于 paths 非空；无声明材料时 paths=[]。一个材料文件可支持多个义务，一个义务可由多个文件满足，不消耗容量。
- compliant 当且仅当暴露组件全部 allowed 且所需义务全部 satisfied；空暴露集合时为 true 且 components/obligations 均为空，其他不关联组件的可见文件仍照常列出。

被遮蔽对象、已删除路径与从未暴露的 blocked 组件不参与许可或义务检查，也不从 rejected 或历史文件虚构义务。组件暴露、许可状态与材料可见性必须来自同一个最终目录树。

### 实现与身份不变量

`layers.py` 采用逐层物化对象身份：每条 file 条目在安装时构造一个不可变对象（sha256、component、origin 固化），目录节点只保存指向该对象的引用。守卫不变量：对象字段创建后永不改变；路径节点在 file/hardlink 写入时整体替换而非原地修改；删除目录只分离整棵子树，不影响仍被其他路径绑定的对象。等价的替代方案是保留 (对象 id, 创建层) 的层版本不可变引用并由链接复制该二元组；两种方案的正确性义务相同（契约只观测层末路径上的对象），本实现选择前者以避免每次读取多一层间接。硬链接旧路径的绑定在解析前撤换，保证本层前向链不误读继承对象。

规模验收（`tests/test_layers_scale.py`）：128 层、39001 个最终文件、60001 条操作、5.7MB 合法输入、10000 级前向硬链接链、5000 条保留旧对象的链接，单进程 CLI 完成并核对可证明的暴露路径、origin 与材料满足结果。

## 样例与错误

样例input.json中core选择MIT与notice，plugin被GPL禁止，tool因WITH完整键未知而阻断，因此compliant=false。release.json为allowed批次（core与lib共享notice，tool需reciprocal，total_cost=3）。release-blocked.json含阻断组件，批次status=blocked。dependency-audit.json为多根、含a↔b环与d→c旁路的供应图，仅a为blocked：a的immediate_dominator为null且lost_count=2，b的immediate_dominator与nearest_blocked_dominator均为a，c因旁路而无支配者，孤立u仅出现在unreachable，CLI退出0。dependency-audit-no-bypass.json删除d→c旁路后，c的immediate_dominator与nearest_blocked_dominator变为a，a的lost_count变为3。reconciliation.json演示跨版本谱系：core-old→core、plugin-old→plugin、vendor-old→vendor、ghost→retired为forced，legacy-old对legacy-a/legacy-b为等成本二选一故两条均possible且不进入transitions；匹配数为5、penalty总和为6，四组forced迁移展示allowed与blocked状态变化（含两侧同为allowed/blocked的情形）。layers.json演示四层镜像式交付：第一层交付需notice的bin/a与材料doc/n，第二层把bin/b硬链接到bin/a、doc/saved硬链接到doc/n，第三层whiteout bin/a并把doc/n覆盖为错误摘要（此时仅bin/b暴露、notice由doc/saved满足），第四层opaque doc并写错误材料（bin/b仍暴露、notice缺失、compliant=false）；从未暴露的retired组件为blocked，不进入components/obligations。layers-recreated.json证明同层whiteout后重建同路文件不被标记删除。业务阻断是正常结果；语法错误、未知引用、重复root/边/配对/层/路径、成本或罚分输入错误或超限才退出2并逐字节保留旧报告。

源字节SHA256随结果保留，输入JSON最多8MB、拒绝重复键与非有限数值及未定义字段；输出成功后原子替换。无网络扫描、源码许可证发现或法律效力认定；成本为调用方提供的工作量数据，不作许可证法律判断。

## 可修订许可观测 SQLite 时段事实仓（warehouse）

另有两个与旧单次分析互斥的 CLI 模式：`--warehouse DB --observations FILE` 导入可修订观测，`--warehouse DB --window FILE --output REPORT` 输出按 UTC 自然日分摊的 allowed/blocked/unknown 秒数。其完整字段、版本决议、事务与存储契约见 [warehouse.md](warehouse.md)。要点：`(event_id,revision)` 为不可变版本键，仅每事件最大 revision 生效，最大版本为 retract 时事件不提供状态，旧版本不复活；同组件同 effective_at 取 event_id 字典序最大者，撤回后次大者显露；状态半开 `[a,b)` 延续，最早观测之前与全撤回之后均为 unknown 且组件不消失。事实、组件目录、物化区间与 generation 在单个 SQLite 事务内提交，失败整批回滚；窗口查询只读持久区间与组件目录，不重演版本历史。

## 离线多源许可清单归并（inventory）

另有第四个互斥 CLI 模式 `--inventory MANIFEST --output REPORT`：MANIFEST 恰含 policy 与 files（相对清单目录的 NDJSON 分片，最多 64 个），分片每行为 claim（source/id/expression）或 alias（left/right 端点对象）。身份按 `(source,id)` 二元组比较，无向别名传递闭包决定实体（键为最小端点）；实体共识是全部成员完整候选族的精确交集，不做并集、不先按 policy 过滤；共识为空才是 conflict，并按端点降序逐试删除给出不可再删证据核，blocked（许可未知/禁止但共识非空）与 conflict 严格区分。报告为原子替换的 UTF-8 NDJSON（header 摘要 + 排序 group/member 行）。分片总字节按原始字节计上限 2 GiB、单行 65536 字节、claim 100 万、alias 200 万，单进程峰值 RSS 不超过 128 MiB；完整契约、删除核定义、外存设计与验收证据见 [inventory.md](inventory.md)。
