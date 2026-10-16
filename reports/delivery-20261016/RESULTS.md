# 交付核对

核对日期：2026-10-16，UTC+8。
核对基线为远程 `main` 的 `daf16b1`。
本轮核对发现 8 个已完成提交尚未推送，工作区没有未提交修改。
交付分支为 `codex/runtime-efficiency-20261014`。

## 交付内容

| 提交 | 内容 | 证据 |
| --- | --- | --- |
| `531a18d` | GPU 张量直连、特征并行、按需加载与前端运行状态修复 | [效率报告](../efficiency-20261014/RESULTS.md) |
| `1e4d8e5` | 逐功能延迟测量 | [逐功能报告](../feature-latency-20261014/RESULTS.md) |
| `b76f110`、`91be67b` | 文件转换、字幕事件、取消与首块播放优化 | [延迟报告](../latency-reduction-20261015/RESULTS.md) |
| `df0af3a` | 播放积压控制与称谓漏译修复 | [播放报告](../playback-latency-20261015/RESULTS.md) |
| `174eaf5`、`5edc360` | 开源时长控制方案、论文及中英朗读对照 | [研究说明](../../docs/TRANSLATION_DURATION.md)、[人工朗读数据](../translation-duration-20261015/RESULTS.md) |
| `8d4ee02` | 逐段比例控制、轻量时长模型与验证 | [灵活变速报告](../flexible-tempo-20261015/RESULTS.md) |

本轮补充报告索引和 CI 的原生变速库覆盖。
CI 先运行没有原生变速库的 Python 回归，再构建原生库并运行播放测试。
该顺序覆盖 FFmpeg 子进程路径和进程内 libavfilter 路径。
CI 不加载 Jetson 模型，不能代替 GPU 和真实资产验收。

## 证据核对

灵活变速清单中的 28 个运行时源文件、1 个模型配置和 1 个 C 源文件，共 30 项哈希全部匹配。
本轮没有修改运行时源文件或模型配置。
8 个提交相对基线共涉及 134 个文件，文件当前大小合计为 1,632,501 B。
其中最大文件为 228,906 B；大型原始音频、模型和测试归档没有加入这批变更。

已有回归记录为主机 Python 176/176、Jetson Python 176/176、主机原生 25/25。
这些记录来自灵活变速验证，日期与配置见对应报告。
本轮文档整理不构成新的设备性能测量。

## 设备与待验收项

上一轮已将候选代码交付至 `/home/moyamryia/mozart-efficiency-20261014/candidate`。
对应记录的提交为 `8d4ee0248186661481c91289d4b2636ab54d4920`。
原始应用目录 `/home/moyamryia/Mozart` 没有被覆盖。
本轮 SSH 握手收到 `kex_exchange_identification: Connection closed by remote host`。
因此，本轮不能确认设备当前文件状态或追加同步。

30 分钟连续输入、物理声卡延迟和新增人工听感验收仍未完成。
译文生成阶段的时长预算也尚未实现。
完整待办见 [TODO](../../TODO.md)。
这些项目不属于已完成但未提交的内容，不能随本次 Git 交付标记为通过。
