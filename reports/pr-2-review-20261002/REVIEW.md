> 历史记录：本文保留当时的测量、日志与审查原文。当前部署和接口以[维护文档](../../README.md)为准。

# PR #2 review — 2026-10-02

Reviewed https://github.com/MoyaMryia/Project-Mozart/pull/2 against main `a506fdd30bfc879ba2dc20d36c0ba910fffce0a0`. The tested review head is `c06cbae`; PR #2 was merged as `0fb19e22f3ffe5424c2ebf6c163f9b84a7b15939`. Local and Jetson main checkouts were fast-forwarded to that merge. The production `~/Mozart/build-gpu` rebuild and its 5/5 backend tests also passed after synchronization.

Jetson checkout: `/home/moyamryia/Mozart`. Isolated review build: `/tmp/mozart-pr-2-review/build-gpu`. ONNX Runtime 1.29.0, CUDA EP and TensorRT are enabled. Full build passed; backend CTest passed 5/5. Comparison self-tests passed. HTTP validation passed for stale-config normalization, rejected unsupported F0 methods/presets and effective-default reset.

Parser smoke checks loaded deployed indices: qiqi_en (930 lists, 36,273 vectors), qiqi_zh (977 lists, 38,109 vectors), mike_tyson (992 lists, 38,701 vectors). Real FAISS fixture retrievals passed; deployed-model audible retrieval comparison was not performed.

Review fixes: documentation conflict resolution, file-budget checks before centroid allocation, duplicate sparse-list rejection, TensorRT test linkage, probe-length CLI parsing, input/shape/dtype validation, failed dynamic probes, internal-randomness detection, and retaining normalized defaults for reset.

## Generator diagnostic

See `generator-comparison-final.log`. This is a synthetic-input export diagnostic, not a Golden speech-quality comparison. Both exports produce 96,000 samples at T=200 and fail at T=50/100 despite symbolic frame axes. Both contain RandomNormalLike and RandomUniform operators; repeated inference with identical inputs differs. Therefore the large cross-model numerical difference cannot establish which model is correct or justify replacement. No model assets were changed.

Next step: preserve the existing deterministic espeak input, reproduce the verified PyTorch Golden, export with shared explicit noise (or a deterministic mean path), compare captured tensors against ONNX, then verify the backend. The existing exporters in `tools/` already support explicit noise inputs.

## Useful next work

- Finish draft PR #1's Jetson and real-model validation for file-queue lifecycle protection.
- Export the missing de_narrator.index from its training data; indices already exist for the other three voices.
- Connect and supervise the subtitle services through the production daemon, and wire the frontend live panel.

At inspection, no Mozart services were running; available memory was about 5 GiB and the filesystem had about 294 GiB free. No live microphone or physical-output verification was performed. The test daemon was terminated after the API smoke test.
