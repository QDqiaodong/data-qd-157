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
.venv/bin/python -m unittest discover -s tests -v
```

本地已有wheel时，安装构建工具可加 `--no-index --find-links <公共wheel缓存目录>`。
修改源码后重新安装。本仓不共享其他仓环境，不依赖Docker、外部账号或持续服务。

输入JSON最多8MB，拒绝重复键、非有限数值和未定义字段；源字节SHA256随结果保留。
输出是可独立读取的JSON；成功后原子替换，输入/计算失败退出2并保留旧报告。
输入和输出不得相同，输出符号链接拒绝。单操作者批处理，不承诺并发发布或断电恢复。
同输入重跑不累加。测试使用临时目录，运行产物在忽略的run/。

src/license_planner/model.py 为业务模型；release.py 为交付批次共享义务的精确求解器；dependency.py 为供应图单点瓶颈审计（即时支配者与支配树子树）；expression.py 为SPDX表达式；
common.py 提供受限JSON及报告发布，cli.py 衔接数据链路。fixtures 为明确自制样例，代码与样例采用MIT许可。

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
