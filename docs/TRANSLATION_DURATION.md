# 译文时长控制：GitHub 调研与中英对照

调研日期：2026-10-15。外部项目仅做资料审阅，未在 Jetson 安装或运行。
项目数字来自已保存的回放音频，未新增语音生成。产品代码没有在本轮调研中改变。

## 结论

应按译文的预测语音时长分配预算，同时保留语义检查和有限变速。
固定中文字符数、英文词数或中英倍率可以作粗估，不能作为逐句硬约束。
这是一项候选设计，尚未验证它能进一步降低 Mozart 的持续输入延迟。

## 已找到的方案

| 项目 | 已确认的做法 | Mozart 可借鉴的部分与限制 |
| --- | --- | --- |
| [Auto-Synced-Translated-Dubs](https://github.com/ThioJoe/Auto-Synced-Translated-Dubs) | 按字幕时间设置目标长度；首遍合成后测量，第二遍调整语速或做时间伸缩。 | 复用目标时长和实测反馈。双遍合成会增加在线等待，不宜直接照搬。 |
| [SSPO](https://github.com/CcQunResearch/SSPO)及[论文](https://aclanthology.org/2025.acl-long.227/) | 按段采样译文，结合合成时长与质量选择偏好样本，再训练时长对齐。 | 用时长约束训练或选择译文。它属于离线训练方法，不能视为当前 0.8B 模型的现成能力。 |
| [IsoChronoMeter 论文](https://aclanthology.org/2024.wmt-1.29.pdf) | 使用 TTS 的时长预测器估计原文和译文时长，并结合翻译质量评价。 | 在生成完整音频前估算时长。[对应仓库](https://github.com/braskai/isochronometer)目前只有 README，未发现可直接复用的实现。 |
| [SeamlessExpressive](https://github.com/facebookresearch/seamless_communication/blob/main/docs/expressive/README.md) | 迁移语速、停顿，并通过 duration factor 调节预测时长。 | 在模型内部控制时长。该参数不是跨语言的通用时长倍率，也不是 PocketTTS 的现有接口。 |

IsoChronoMeter 的英文实验指出：短文本的预测误差较大，不能给极短句设置过紧的时长上限。
该结果只代表论文的预测器和评估条件，不是 Mozart 的误差保证。

## 可用的公开中英配对参考

[Google CVSS](https://github.com/google-research-datasets/cvss)包含中文原声及对应的英文合成音频。
[论文表 3](https://arxiv.org/pdf/2201.03713#page=5)给出如下统计：

| 子集 | 配对条数 | 中文原声总长 | 英文 CVSS-C 总长 | 英文 CVSS-T 总长 |
| --- | ---: | ---: | ---: | ---: |
| 训练 | 7085 | 10.4 h | 8.7 h | 9.3 h |
| 验证 | 4843 | 7.9 h | 6.0 h | 6.5 h |
| 测试 | 4897 | 8.2 h | 5.8 h | 6.3 h |

测试集总时长相除约为 0.71 和 0.77。这是由论文舍入统计值计算的加权总量比，不是逐句中位数。
CVSS-C 使用固定英文音色；CVSS-T 使用跨语言音色迁移。
论文将合成音频更短归因于流利度更高及较少长静音。这里不能推出语言本身的固定倍率。
音色、停顿和模型与 Mozart 不同，公开数据适合作参考和额外验证集。

## Mozart 当前音色的逐段计时

范围：Jetson Orin Nano、Qiqi 参考音色、PocketTTS CPU int8、2 线程、5 步、种子 42、3 个潜在帧。
固定输入为 120 s，共 23 个父字幕和 41 个逻辑播报任务。
中文时长取归档输入 WAV 的采样数，包含该片段中的静音。英文时长取对应原速合成 WAV 的采样数。
这里不统计模型推理或排队等待，也不使用 1.05 倍变速后的长度。

**下表是实际 ASR 与翻译管线的输出，未经人工校对。它是时长样本，不是翻译质量基准。**
源识别和译文仍有错误，不能把这些文本当作正确译文训练标签。
英文文本到其实际音频时长的配对仍可用于同一 TTS 配置的时长标定。

| 序号 | 中文归档时长 | 英文原速时长 | 英文／中文 | 已识别中文 | 实际英文输出 |
| --- | ---: | ---: | ---: | --- | --- |
| 1 | 9.06 s | 11.20 s | 1.24 | 妈妈说暑假考研做助诱拿了大几千完了你凭着这个考研跟你妈妈报了多少钱金币了呀 | Mama said that during the summer vacation, she took a course to help boost the result of the exam and won a large sum of money. You paid with this exam to your mom a lot of gold coins? |
| 2 | 2.56 s | 2.64 s | 1.03 | 七千五千又是大几千 | Seven thousand five thousand is a large number. |
| 3 | 2.56 s | 2.16 s | 0.84 | 你妈妈在家里心想着 | Your mother is thinking about the house. |
| 4 | 2.88 s | 3.20 s | 1.11 | 我儿子在学校里面要考研 | My son is studying for a graduate exam at school. |
| 5 | 8.96 s | 10.24 s | 1.14 | 你妈妈跟龙虾大妈跳广场舞或者说女儿闷的时候人家会比较儿子现在怎么样你妈妈很骄傲地跟人家说 | Your mother is dancing with the lobster older woman in the square dance. Or when the daughter is quiet, the son will be more. Now how is it that your mother is proud to tell the people |
| 6 | 0.96 s | 1.20 s | 1.25 | 我儿子 | My son |
| 7 | 7.04 s | 8.64 s | 1.23 | 现在准备考研准备弄个考个研究生你慢慢一边说一边内心还有点开心自己儿子 | Now preparing for the graduate entrance exam. You speak slowly, and your heart still feels a little bit happy that your son is there. |
| 8 | 11.84 s | 10.40 s | 0.88 | 想上进但是呢你在干嘛你在赌博你要借我网贷你在网恋你们老的都是通过这个博去引钱去掉一线就没东西装逼 | I am working. You are gambling and you are asking me to borrow a loan. You are in a web dating situation where everyone is using this to borrow money. No one has anything to brag about. |
| 9 | 1.28 s | 1.12 s | 0.88 | 这个就是你 | That is you. |
| 10 | 2.88 s | 1.36 s | 0.47 | 充值战备还贷款 | Repay the loan |
| 11 | 6.72 s | 6.64 s | 0.99 | 过几天又年本代地的输回去这个是经典的流程是什么呢骗钱或者借钱 | Soon, the land will be returned to the original owner. This is the classic process of scamming or borrowing money. |
| 12 | 1.60 s | 1.12 s | 0.70 | 然后去充值 | Then recharge. |
| 13 | 2.88 s | 2.40 s | 0.83 | 只是补贴计划可能赢一段时间 | Just the subsidy plan may win for a while. |
| 14 | 8.64 s | 11.28 s | 1.31 | 就会有一天上头上头完全不全部输完之后又开始借钱或者骗钱然后又冲进去然后又补贴计划 | It will come down to one day when the top and the top will not be able to lose everything. After that, they start borrowing money or deceiving it. And then he entered again and then the subsidy plan. |
| 15 | 10.88 s | 12.80 s | 1.18 | 然后可能又赢或者说直接输反正最后的结果一定是输反正就是这个循环一直来一直来这个就是读狗的循环逃不掉的没有一个独狗能逃掉 | Then maybe they will win or lose it all, but the result is the end result. It will definitely lose, for this is the loop. This is reading dogs. No escape from the loop; no single dog can escape. |
| 16 | 8.64 s | 9.20 s | 1.06 | 比如说要上政治小生小课生班什么的又要到了一万块完了因为报精辟你就拿着你让学习 | For example, to enroll in a political class or a small class, you need to pay 10000 yuan. Okay because you're holding on to your learning |
| 17 | 7.04 s | 6.72 s | 0.95 | 要跑研究要政治小朋友身班什么东西的你从通通往到这种事情去骗你妈妈钱 | To run a research project, a political student must have a class. You are going to deceive your mother with money. |
| 18 | 10.88 s | 8.48 s | 0.78 | 你看看读这个真的什么谎都杀得出来一段为了骗钱什么话都杀得出来只要能骗到钱就行他不管对面是谁不管自己 | Do you read this and find out if it's a lie? You can't tell a lie with money. As long as you can get money. He ignores whoever is facing him and ignores himself. |
| 19 | 3.52 s | 2.96 s | 0.84 | 在做了什么事情只要能要到钱只要能翻本 | To earn money, to get money, to recover |
| 20 | 3.20 s | 1.84 s | 0.57 | 钱赢回来所有的谎话 | Qian wins back all the lies. |
| 21 | 1.60 s | 2.00 s | 1.25 | 就可以不当等伤害 | can not be treated as a threat |
| 22 | 2.56 s | 3.76 s | 1.47 | 这个时候妈妈已经有点不相信了 | At this point, the mother has become increasingly doubtful. |
| 23 | 1.82 s | 2.40 s | 1.32 | 妈妈还是怀疑了但是由于我 | Mother still doubted me because of me. |

中文总长为 120.000 s，英文原速总长为 123.760 s。整段时长比为 1.0313。
逐段比值为 0.47–1.47，12/23 段英文更长。逐段比值的中位数为 1.0313，P90 为 1.2944。
按空格分词的英文共 397 词，对应约 3.21 词/s。数字和缩写仍可能一个文本词对应多个发音单位。
这个词速只适合当前样本的初始粗估，不能作为所有音色的常数。

输入 SHA-256：`f1b319995cb2dce9b46cfdb0b1d0bb8829d5cf916a61992a8750733408a4ef84`。
原始回放位于 `rvc-golden/playback-latency-20261015/replays/backlog-library-confirmed-20261015/`。
逐功能、播放完整性与源码证据见[播放延迟报告](../reports/playback-latency-20261015/RESULTS.md)。

## 候选实施顺序

1. 使用当前 41 段英文及合成时长标定预测器。加入音节、数字展开和停顿特征。
2. 按父字幕分组留出验证数据。报告低估误差与长句、数字、否定句的误差。
3. 给翻译器提供源片段时长和可用播放预算，要求自然表达并保留事实。
4. 优先一次生成。只对预测超预算且尚未播出的译文尝试一次重写。
5. 检查数字、人称、否定、因果与分句覆盖。检查失败时保留原候选，并明确记录积压。
6. 保留有限的保音高变速。另测 30 分钟输入的积压趋势、语义覆盖及实际音频。

不能通过删除句子、缩减事实或只统计成功任务来宣称时长控制有效。
实时任务还需考虑尚未到来的输入；离线配音的完整字幕时间线不能直接当作在线可用信息。
