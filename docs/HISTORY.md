# 历史重写说明

2026-11-03 对仓库做了一次历史瘦身。本文记录原因、范围、结果与旧提交号的映射。
旧提交在报告中仍有引用，查证时按[映射表](#被引用提交的旧号与新号)换算，
或按[恢复方法](#恢复旧历史)取回完整旧历史。

## 原因与范围

`.git` 曾达 1015 MB，其中 922 MB 是已废弃或与本项目无关的对象。
以下路径已从全部历史中移除：

| 路径 | 内容 | 大小 |
| --- | --- | --- |
| `reference/EBF_ZYNQ70xx_BTB_CORE/` | Zynq 核心板 PCB 参考设计，与 Mozart 无关 | 123 MB |
| `preprocessor/native/rnnoise/src/rnnoise_data{,_little}.c` | RNNoise 生成数据，现由 `preprocessor/assets/rnnoise_default.rnnb` 提供 | 103 MB |
| `pre/`、`preprocessor/build/` | 早期误提交的构建产物 | 123 MB |
| `rvc-golden/liuying-testaudio-polish/` | 已过期的试听音频 | 33 MB |
| `rvc-golden/output/preprocessor-sample-mp4-30s-fp16-mozart-backend.wav` 等 4 个输出 | 未被 manifest 或报告引用的后端输出 | 21 MB |
| `reports/pre-asr-checkpoint/host/source-config-snapshot.tar.gz` | 配置快照压缩包 | 10 MB |

同时取消跟踪 4 个文件（文件仍保留在工作区）：3 个未被引用的后端输出 wav 与
`reports/xasr-streaming-20261029/mutations.json`。`.gitignore` 与索引现已一致，
`git ls-files -i -c --exclude-standard` 无输出。

## 结果

| 指标 | 重写前 | 重写后 |
| --- | --- | --- |
| `.git` 体积 | 1015 MB | 100 MB |
| pack 体积 | 922 MB | 99 MB |
| `main` 提交数 | 330 | 322 |
| `HEAD` 树对象 | `3d89559` | `3d89559`（内容逐字节相同） |

全部提交号因此改变。`main` 的文件内容与重写前完全一致，回归结果不受影响。

## 被剪除的提交

只修改被剥离路径的 8 个提交在重写后为空，已删除：

| 旧提交 | 日期 | 主题 |
| --- | --- | --- |
| `7c11f66` | 2026-07-12 | Completed RNNoise Stage #1 |
| `37acbb5` | 2026-07-12 | Pre |
| `280e3fa` | 2026-07-12 | Update of pre |
| `7a9ca5a` | 2026-07-12 | Testing pre |
| `05dfe32` | 2026-07-12 | Some optimization |
| `648e1be` | 2026-09-02 | test(rvc-golden): add preprocessing sample streaming output |
| `f6d4d35` | 2026-09-02 | test(rvc-golden): refresh TRT-GPU backend streaming output (T398 framing) |
| `0436f98` | 2026-09-02 | test(rvc-golden): refresh TRT-GPU backend streaming reference output |

其余提交的编号见完整映射 [history-sha-map.txt](history-sha-map.txt)。

## 被引用提交的旧号与新号

下表覆盖 `*.md` 与 `*.tex` 中引用、且存在于旧历史的全部提交号。
“引用数”是该旧号在文档中出现的文件数。

| 旧号 | 新号 | 引用数 | 首个引用文件 |
| --- | --- | --- | --- |
| `fd9a0fc` | `523c068` | 8 | `reports/realtime-patches-20261008/BOUNDARY_CLASSIFIER.md` |
| `b514273f87e4262aa5191a14fff71e1db61fb7ca` | `0065775` | 3 | `reports/full-run-20261005/RESULTS.md` |
| `fd9a0fc55bc6b84baa16f5d7eb91c0a02eb5cd5b` | `523c068` | 3 | `reports/full-run-20261005/RESULTS.md` |
| `0fb19e22f3ffe5424c2ebf6c163f9b84a7b15939` | `2a00d6a` | 2 | `reports/next-steps-audit-20261002/AUDIT.md` |
| `1e4d8e5` | `c2a3808` | 2 | `reports/delivery-20261016/RESULTS.md` |
| `531a18d` | `f26c0b9` | 2 | `reports/delivery-20261016/RESULTS.md` |
| `8bf2a46` | `a8fea7f` | 2 | `reports/realtime-patches-20261008/RESULTS.md` |
| `8e610e0` | `5b5cea4` | 2 | `reports/realtime-patches-20261008/FOLLOWUP.md` |
| `d16121f` | `030ab8c` | 2 | `reports/realtime-patches-20261008/BOUNDARY_CLASSIFIER.md` |
| `d869b23` | `2c0c110` | 2 | `reports/refinement-deadline-20261028/RESULTS.md` |
| `daf16b1` | `b39da38` | 2 | `reports/delivery-20261016/RESULTS.md` |
| `ec4f0c1` | `885f174` | 2 | `reports/repository-audit-20261008/REALTIME_STATUS.md` |
| `fc774b6` | `ccb1699` | 2 | `reports/codex-review-20261016/RESULTS.md` |
| `1194edc` | `20a8434` | 1 | `reports/demo-fixes-20261028/RESULTS.md` |
| `11be3bf` | `3a08f4e` | 1 | `reports/loop-latency-20261025/RESULTS.md` |
| `174eaf5` | `cf494a7` | 1 | `reports/delivery-20261016/RESULTS.md` |
| `23ca984` | `d92f274` | 1 | `reports/realtime-patches-20261008/GPU.md` |
| `3a23341` | `3aae50a` | 1 | `reports/m5-dtln-20261025/RESULTS.md` |
| `3ea9291` | `727cdd8` | 1 | `reports/repository-audit-20261008/TEMPORARY_PATCHES.md` |
| `5846bb7` | `60d1a96` | 1 | `reports/repository-audit-20261013/RESULTS.md` |
| `5edc360` | `bdff442` | 1 | `reports/delivery-20261016/RESULTS.md` |
| `5f561da` | `f5ec333` | 1 | `reports/control-races-20261029/RESULTS.md` |
| `6235d2f83416ba40c79b561b7af98781b3628b28` | `d5dd355` | 1 | `reports/product-readiness-20261019/RESULTS.md` |
| `6e77390` | `7ff36fe` | 1 | `reports/tempo-limit-20261016/RESULTS.md` |
| `70ca280` | `0d11f90` | 1 | `rvc-golden/STREAMING_BACKEND_INVESTIGATION.md` |
| `7424809` | `c317fc5` | 1 | `reports/repository-audit-20261008/TEMPORARY_PATCHES.md` |
| `746e4bd` | `71157ab` | 1 | `reports/repository-audit-20261008/RESULTS.md` |
| `75154b6` | `8148e11` | 1 | `reports/demo-fixes-20261028/RESULTS.md` |
| `786cfcb` | `175af91` | 1 | `reports/repository-audit-20261013/RESULTS.md` |
| `8d4ee02` | `f058fce` | 1 | `reports/delivery-20261016/RESULTS.md` |
| `8d4ee0248186661481c91289d4b2636ab54d4920` | `f058fce` | 1 | `reports/delivery-20261016/RESULTS.md` |
| `8f153199a7d72d4aeaf0dcb2b45bcc1bd5054dd5` | `046ab79` | 1 | `reports/next-steps-audit-20261002/AUDIT.md` |
| `91100e4` | `233de5f` | 1 | `reports/realtime-patches-20261008/EARLY_AUDIO.md` |
| `91be67b` | `6cff51c` | 1 | `reports/delivery-20261016/RESULTS.md` |
| `a506fdd30bfc879ba2dc20d36c0ba910fffce0a0` | `15167ea` | 1 | `reports/pr-2-review-20261002/REVIEW.md` |
| `a7bb966` | `7ed9419` | 1 | `reports/asr-selection-20261028/RESULTS.md` |
| `adc595c` | `15d1d1a` | 1 | `reports/repository-audit-20261013/RESULTS.md` |
| `b514273` | `0065775` | 1 | `reports/zero-shot-tts-redesign-20261004/PLAN.md` |
| `b5a359c` | `d05d6e1` | 1 | `reports/realtime-patches-20261008/FALLBACK.md` |
| `b76f110` | `de63378` | 1 | `reports/delivery-20261016/RESULTS.md` |
| `ba72b6f` | `2bcad66` | 1 | `reports/m1-single-trunk-20261025/RESULTS.md` |
| `bbb9de3` | `bbdf690` | 1 | `reports/m3-endpoints-20261025/RESULTS.md` |
| `bc73b38` | `99b6bee` | 1 | `reports/realtime-patches-20261008/COVERAGE.md` |
| `bd3b9cb` | `29c8624` | 1 | `reports/realtime-patches-20261008/STABLE_CLAUSES.md` |
| `c06cbae` | `84fbbe4` | 1 | `reports/pr-2-review-20261002/REVIEW.md` |
| `c76e990` | `c6cbfd9` | 1 | `reports/codex-review-20261016/RESULTS.md` |
| `c8b3ef2` | `deab112` | 1 | `reports/realtime-patches-20261008/CONTINUITY.md` |
| `dccc709` | `a6c812f` | 1 | `reports/loop-latency-20261025/RESULTS.md` |
| `df0af3a` | `f490d5f` | 1 | `reports/delivery-20261016/RESULTS.md` |
| `df93ef9` | `9190d27` | 1 | `reports/realtime-patches-20261008/FALLBACK.md` |
| `e4c2180` | `1ffff8b` | 1 | `reports/xasr-streaming-20261029/RESULTS.md` |
| `f7e48fe` | `66a29a1` | 1 | `reports/speech-latency-20261029/RESULTS.md` |

## 恢复旧历史

完整旧历史保存在仓库外的归档中，含全部 13 个分支、远端跟踪引用与标签：

```text
~/Projects/Project-Mozart-backups/git-bundles/mozart-full-20261103-1620.bundle
sha256 d3069c049024913ddc499788902e4c95bfb27676e55d45246351c2fd6e6e22c2
```

同目录的 `mozart-commit-map-20261103.txt` 是旧号到新号的全量映射，
`mozart-ref-map-20261103.txt` 是分支与标签的映射，均为 `git filter-repo` 原始输出。

按旧号查看某个提交：

```bash
git clone --mirror ~/Projects/Project-Mozart-backups/git-bundles/mozart-full-20261103-1620.bundle /tmp/mozart-old
git -C /tmp/mozart-old log --oneline 40045de
```

## 适用范围

- 本说明对应当前 `main`。此后新增的提交不受影响，旧号也不会再被复用。
- 重写只改变了提交号与历史对象，没有改变任何被跟踪文件的内容；`HEAD` 树对象在重写前后相同。
- 当前校验：`ctest` 39/39、`writing_lint` 无命中、前端 78/78。
