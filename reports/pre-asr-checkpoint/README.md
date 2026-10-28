# ASR 修改前备份

代码基线为 `a7bb966`。本备份保存本机和 Jetson 仓库中未跟踪或被忽略的源码、配置和小型文本证据。
本次没有修改运行代码。

## 备份分支

| 分支 | 内容 |
| --- | --- |
| `backup/pre-asr-a7bb966-main` | 修改前的主线提交 |
| `backup/pre-asr-a7bb966-files` | 本文和两台机器的文件快照 |
| `backup/pre-asr-a7bb966-host-stash-0` | 本机唯一的 stash 提交及其父提交 |
| `backup/pre-asr-a7bb966-jetson-stash-0` | Jetson 的 `jetson-worktree-20261028` stash |
| `backup/pre-asr-a7bb966-jetson-stash-1` | Jetson 的 `wola-local-20261027` stash |
| `backup/pre-asr-a7bb966-jetson-stash-2` | Jetson 的 `sync-before-switch-20261026` stash |
| `backup/pre-asr-a7bb966-jetson-stash-3` | Jetson 的 ONNX 指南 stash |

stash 分支指向原始 stash 合并提交。Git 同时保存其父提交，包括暂存状态和存在时的未跟踪文件父提交。
本次保留原有 stash，没有应用或删除 stash。stash 分支完整保存历史内容，包括其中的第三方依赖。

## 文件快照与恢复

`host/` 对应 `/home/moyamryia/Projects/Project-Mozart`。
`jetson/` 对应 `jetson-wg:/home/moyamryia/Mozart`。
每个目录包含 `source-config-snapshot.tar.gz` 和 `manifest.json`。

清单记录源目录、主机名、压缩包生成时间、Git 状态、原路径、文件字节数及 SHA-256 校验值。
清单也记录压缩包校验值，并按粗分类记录排除数量。
本机快照包含 6323 个文件，Jetson 快照包含 3970 个文件。
所有快照成员已与对应机器的源文件逐项比较，字节数和校验值全部一致。

Jetson 的 `untracked_paths` 记录两份文件：`state/corrections.json` 和 `reports/wola-silent-start-20261027/metrics.json`。
这两份文件均在压缩包内。

恢复时，先在独立目录解压。核对清单后，再选择需要的文件。
不要直接把历史快照覆盖到当前工作区。

## 适用范围

- 文件选择：未跟踪或被忽略的源码、文档、配置、TXT、SHA-256 文件及不超过 1 MiB 的 JSON 文件。
- 文件快照排除：依赖、模型、编译产物、缓存、符号链接、生成的 `.mod.c` 文件、录音和运行日志。实验驱动源码按普通源码保留。
- 仓库外的 `~/mozart-archive/` 和其他备份目录保持原位，本快照不包含这些目录。
- 凭据模式扫描覆盖全部快照文件，以及 stash 工作树、暂存父提交和未跟踪父提交中新增或修改且小于 2 MiB 的文件。
- 扫描模式包括私钥、GitHub token、AWS access key 和 `sk-` token；该扫描只检查这些固定格式。
