# Mozart 预处理

纯 C11 音频预处理管线。详细文档见 [`docs/PREPROCESSING.md`](../docs/PREPROCESSING.md)。

```bash
make          # Release（含 RNNoise, ARM NEON）
make stub     # 直通（不含 RNNoise）
make run      # 构建 + 冒烟测试
```
