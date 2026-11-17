# Caller-policy SPDX expression choices v1
软件交付整理者输入policy与components。policy的license为单许可证ID或“ID WITH ExceptionID”，allowed严格布尔、obligations唯一字符串；最多200条。component恰含唯一id及expression，最多1000个。
解析大小写敏感的ID、WITH、括号、AND、OR，优先级WITH>AND>OR，WITH只能紧跟许可证ID。许可证ID语法支持字母数字点加号连字符。不是完整SPDX文档解析、许可证目录认证或法律建议；WITH为完整独立政策键，绝不自动继承基础许可。
AND合并所需许可，OR保留可选集合；每节点最多256集合，AND笛卡尔积最多4096，否则整批拒绝。未知或显式禁止的许可阻断该路径。允许路径义务取并集；选义务数最少、许可数最少、许可列表字典序最小者。逐组件提供所有许可选择、拒绝原因、选中义务，不承诺跨组件全局义务最小化。
样例core选择MIT与notice，plugin被GPL禁止，tool因WITH完整键未知而阻断，因此compliant=false；业务阻断是正常结果，语法错误才退出2。无网络扫描、源码许可证发现或法律效力认定。
