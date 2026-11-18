本目录为原创虚构合成数据，使用MIT许可。没有真实个人、线上实验、生产日志、交易、项目进度或搜索用户信息；不来自外部平台。

- input.json：旧样例（无 release），逐组件报告，compliant=false。
- release.json：allowed 交付批次，三个组件共享 notice 与 reciprocal，total_cost=3。
- release-blocked.json：含被禁/未知许可的阻断组件，批次 status=blocked。
- dependency-audit.json：多根供应图（roots=s,t），含 a↔b 有向环与 d→c 旁路，仅 a 为 blocked；孤立 u 仅在 unreachable，a 的 lost_count=2、c 无支配者。
- dependency-audit-no-bypass.json：在上一样例上删除旁路边 d→c；c 改由 a 支配，a 的 lost_count=3。

