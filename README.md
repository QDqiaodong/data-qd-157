# data-qd-157 · SPDX许可证合规路径

SPDX expression policy alternatives and obligation evidence。业务输入、计算和当前边界详见 [数据契约](docs/contract.md)。

## 安装与演示

Python >=3.11，运行和测试仅用标准库；先确认 `python3 --version`。
每仓建立独立环境，构建工具锁版本。首次安装无公共缓存时可能联网；业务运行不访问网络。

```sh
python3 -m venv .venv
.venv/bin/python -m pip install -r requirements-build.lock
.venv/bin/python -m pip install --no-build-isolation --no-deps .
.venv/bin/python -m pip check
.venv/bin/license-planner --source fixtures/input.json --output run/report.json
.venv/bin/license-planner --source fixtures/release.json --output run/release-report.json
.venv/bin/license-planner --source fixtures/release-blocked.json --output run/release-blocked-report.json
.venv/bin/license-planner --source fixtures/dependency-audit.json --output run/dependency-report.json
.venv/bin/license-planner --source fixtures/reconciliation.json --output run/reconciliation-report.json
.venv/bin/python -m unittest discover -s tests -v
```

### 可修订许可观测时段事实仓（warehouse）

三个 CLI 模式互斥。warehouse 模式把调用方提供的版本化观测写入本地 SQLite，持久维护
与当前生效观测等价的组件状态区间，并按 UTC 自然日分摊 allowed/blocked/unknown 秒数；
完整数据契约见 [warehouse 契约](docs/warehouse.md)。

```sh
.venv/bin/license-planner --warehouse run/demo.db \
  --observations fixtures/warehouse-observations.json
.venv/bin/license-planner --warehouse run/demo.db \
  --observations fixtures/warehouse-corrections.json
.venv/bin/license-planner --warehouse run/demo.db \
  --window fixtures/warehouse-window.json --output run/window-report.json
```

导入成功仅向 stdout 输出回执（`schema_version`、`source_sha256`、`generation`、
`inserted`、`duplicates`），不接受 `--output`；窗口查询原子发布报告，查询不会创建
数据库。观测状态由现有 `analyze` 单组件判定计算，调用方不能直接给 status；
`component_id` 始终显式提供，仓内不推断谱系。版本乱序、迟到旧版本、同刻候选回退、
跨日更正与全撤回 unknown 均有测试覆盖；规模用例为 20000 组件 / 200000 版本。

### 离线多源许可清单归并（inventory）

第四条与旧模式互斥的 CLI 链路：调用方在 MANIFEST 中给出 policy 与最多 64 个 NDJSON
分片（claim 与确定性 alias 边），管道按无向别名传递闭包归并跨文件组件身份，输出每个
实体的许可精确交集共识，以及共识为空时不可再删的冲突证据核；不推断身份匹配、不访问
网络、不作法律判断。完整字段、删除核程序、外存设计与 RSS 约束见
[inventory 契约](docs/inventory.md)。

```sh
.venv/bin/license-planner --inventory fixtures/inventory-example/manifest.json \
  --output run/inventory-report.ndjson
```

输出是原子替换的 UTF-8 NDJSON（header + 按实体键排序的 group/member 行）；stdout 仅
打印小型计数回执。分片按原始字节计上限 2 GiB、单行 65536 字节；claim 上限 100 万、
alias 上限 200 万，独立子进程实测峰值 RSS 低于 128 MiB（SQLite 磁盘索引 + 紧凑整数
并查集 + 流式分组/序列化）。任何输入错误退出 2 且旧报告逐字节保留。

### 离线层叠交付物清单审计（layers）

第五条与旧模式互斥的 CLI 链路：`--layers FILE --output REPORT`。只读取调用方给出的
文件元数据，不读取镜像、压缩包或真实文件内容；所有路径在虚拟目录树中解释。按有序
层的 whiteout/opaque 删除、目录与文件安装、硬链接绑定语义重建最终可见文件树，再只对
实际暴露的组件复用单组件 `analyze` 的许可与 selected 规则，并以最终可见文件的声明
SHA256 判定义务材料是否满足。组件身份始终使用调用方给定 id。完整字段、路径契约、
删除/覆盖/链接语义与输出结构见 [layers 契约](docs/contract.md#离线层叠交付物清单审计layers)。

```sh
.venv/bin/license-planner --layers fixtures/layers.json \
  --output run/layers-report.json
```

任何输入错误退出 2 并逐字节保留旧报告；许可阻断或材料缺失是正常业务结果，退出 0。
实现用逐层物化的不可变对象身份（sha256/component/origin 固化）与显式栈链接解析，
10000 级前向硬链接链不依赖递归；规模用例为 128 层、39001 个最终文件、60001 条操作。

本地已有wheel时，安装构建工具可加 `--no-index --find-links <公共wheel缓存目录>`。
修改源码后重新安装。本仓不共享其他仓环境，不依赖Docker、外部账号或持续服务。

输入JSON最多8MB，拒绝重复键、非有限数值和未定义字段；源字节SHA256随结果保留。
输出是可独立读取的JSON；成功后原子替换，输入/计算失败退出2并保留旧报告。
输入和输出不得相同，输出符号链接拒绝。单操作者批处理，不承诺并发发布或断电恢复。
同输入重跑不累加。测试使用临时目录，运行产物在忽略的run/。

src/license_planner/model.py 为业务模型；release.py 为交付批次共享义务的精确求解器；dependency.py 为供应图单点瓶颈审计（即时支配者与支配树子树）；expression.py 为SPDX表达式；reconcile.py 为跨版本谱系对账（最小费用最大流与三分类）；
warehouse.py 为可修订观测的 SQLite 时段事实仓（版本决议、物化状态区间与按日积分，单事务原子提交）；
layers.py 为离线层叠交付物清单审计（虚拟目录树、删除/覆盖/硬链接语义与义务材料可见性）；
common.py 提供受限JSON及报告发布，cli.py 衔接五条互斥数据链路。fixtures 为明确自制样例，代码与样例采用MIT许可。

## 业务与样例

详见 [数据契约](docs/contract.md)。要点：策略与表达式规则同前；可选 release 只含 obligation_costs，
必须不重不漏覆盖 allowed=true 义务的去重集合（最多16种），cost 为 0..1000000000 整数。release 模式最多120个组件。

同一义务只准备一份材料，按四级字典序精确最小化：义务并集成本和、义务种数、各组件许可数之和、
按组件id升序的许可列表序列。结果在 result.release_plan 中给出 selections 与按义务汇总的 obligations 证据；
任一组件被阻断时整个批次 status=blocked，不输出部分计划（CLI 仍退出0）。有 release 时旧 components.selected
仍按旧规则计算，不改变既有输出。无 release 时结果结构与值保持原样。

可选 dependency_graph 恰含 roots 与 edges，边为 dependency→dependent 的供应方向；只在有 dependency_graph
且无 release 时组件上限放宽到30000、边上限120000，release 仍限120组件。重复 root、重复有向边、未知引用或
结构错误退出2并保留旧报告；自环与有向环合法。result.dependency_audit 给出每个可达组件的 immediate_dominator、
停用失联数 lost_count（支配树子树大小）、必经 blocked 组件计数与最近 blocked 支配者；blocked 节点与边不预删，
审计不重新选择许可。多根、循环、自环与旁路均由虚拟入口上的迭代支配者算法精确处理，深链不提高递归上限。

fixtures/input.json 为旧样例（compliant=false）；fixtures/release.json 为 allowed 批次（total_cost=3）；
fixtures/release-blocked.json 为阻断批次；fixtures/dependency-audit.json 与
fixtures/dependency-audit-no-bypass.json 演示旁路如何改变支配关系与 lost_count。成本是调用方提供的工作量数据，不作许可证法律判断。

可选 reconciliation 恰好包含 baseline 与 links：baseline 表示此前版本的 policy/components（复用同一
SPDX 与策略契约，最多1000条，不得嵌套其他分析），links 为 baseline_id→current_id 的候选对应及
0..1000000000 整数 penalty（最多12000条、拒绝 bool/重复配对/未知引用，两侧同名ID不自动配对）。
存在 reconciliation 时当前组件同样最多1000条，且仍受 release 等更小上限约束。result.reconciliation_audit
先精确最大化匹配对数、再精确最小化 penalty 总和，并把每条输入边在**全部**最优匹配上分类为
forced/possible/excluded，另给出两侧必定未配对记录；transitions 只为 forced 边输出两侧既有
allowed/blocked 状态，绝不把歧义配对当作确定迁移。匹配只由候选边与 penalty 决定，不受许可状态影响。

fixtures/reconciliation.json 演示该对账：四组 forced 迁移、一组等成本二选一（legacy-old 的两条边均为
possible 且不进入 transitions），匹配数5、penalty总和6。实现用一次最小费用最大流加零约简费用残余图的
强连通分量精确分类，不对每条边重解匹配，800×800、6100 边的规模用例在单进程内完成。
