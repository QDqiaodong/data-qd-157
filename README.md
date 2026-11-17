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
.venv/bin/python -m unittest discover -s tests -v
```

本地已有wheel时，安装构建工具可加 `--no-index --find-links <公共wheel缓存目录>`。
修改源码后重新安装。本仓不共享其他仓环境，不依赖Docker、外部账号或持续服务。

输入JSON最多8MB，拒绝重复键、非有限数值和未定义字段；源字节SHA256随结果保留。
输出是可独立读取的JSON；成功后原子替换，输入/计算失败退出2并保留旧报告。
输入和输出不得相同，输出符号链接拒绝。单操作者批处理，不承诺并发发布或断电恢复。
同输入重跑不累加。测试使用临时目录，运行产物在忽略的run/。

src/license_planner/model.py 为业务模型，相关算法模块独立组织；common.py 提供受限JSON及报告发布，
cli.py 衔接数据链路。fixtures 为明确自制样例，代码与样例采用MIT许可。

## 业务与样例

# Caller-policy SPDX expression choices v1
软件交付整理者输入policy与components。policy的license为单许可证ID或“ID WITH ExceptionID”，allowed严格布尔、obligations唯一字符串；最多200条。component恰含唯一id及expression，最多1000个。
解析大小写敏感的ID、WITH、括号、AND、OR，优先级WITH>AND>OR，WITH只能紧跟许可证ID。许可证ID语法支持字母数字点加号连字符。不是完整SPDX文档解析、许可证目录认证或法律建议；WITH为完整独立政策键，绝不自动继承基础许可。
AND合并所需许可，OR保留可选集合；每节点最多256集合，AND笛卡尔积最多4096，否则整批拒绝。未知或显式禁止的许可阻断该路径。允许路径义务取并集；选义务数最少、许可数最少、许可列表字典序最小者。逐组件提供所有许可选择、拒绝原因、选中义务，不承诺跨组件全局义务最小化。
样例core选择MIT与notice，plugin被GPL禁止，tool因WITH完整键未知而阻断，因此compliant=false；业务阻断是正常结果，语法错误才退出2。无网络扫描、源码许可证发现或法律效力认定。
