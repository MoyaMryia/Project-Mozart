// export_rnnoise_blob.c — 把内嵌在 rnnoise_data.c 里的模型权重导出为
// parse_weights() 可读的二进制 blob（WeightHead + 裸数据 记录流）。
//
// 一次性工具：须在 rnnoise 库仍以"内嵌权重"模式（未定义 USE_WEIGHTS_FILE）
// 编译时运行，直接链接 rnnoise_data.c 遍历 rnnoise_arrays[]。
//
//   mozart_export_rnnoise_blob <output.rnnb>
#define _POSIX_C_SOURCE 200809L
#include "nnet.h"

#include <stdio.h>
#include <stdlib.h>
#include <string.h>

// denoise.h 里声明，定义在 rnnoise_data.c（内嵌模式）
extern const WeightArray rnnoise_arrays[];

int main(int argc, char **argv)
{
    if (argc != 2) {
        fprintf(stderr, "用法: %s <output.rnnb>\n", argv[0]);
        return 1;
    }
    if (sizeof(WeightHead) != WEIGHT_BLOCK_SIZE) {
        fprintf(stderr, "WeightHead 布局不符 (%zu != %d)\n",
                sizeof(WeightHead), WEIGHT_BLOCK_SIZE);
        return 1;
    }

    FILE *f = fopen(argv[1], "wb");
    if (!f) { perror("fopen"); return 1; }

    int n = 0;
    long total = 0;
    for (const WeightArray *a = rnnoise_arrays; a->name; a++, n++) {
        WeightHead h;
        memset(&h, 0, sizeof(h));
        memcpy(h.head, "RNNN", 4);
        h.version    = WEIGHT_BLOB_VERSION;
        h.type       = a->type;
        h.size       = a->size;
        h.block_size = a->size;   // parse_record 允许 block_size == size
        if (strlen(a->name) >= sizeof(h.name)) {
            fprintf(stderr, "name too long: %s\n", a->name);
            fclose(f);
            return 1;
        }
        strcpy(h.name, a->name);

        if (fwrite(&h, sizeof(h), 1, f) != 1 || fwrite(a->data, 1, a->size, f) != (size_t)a->size) {
            fprintf(stderr, "write failed at %s\n", a->name);
            fclose(f);
            return 1;
        }
        total += (long)a->size;
    }
    fclose(f);
    printf("导出 %d 个数组, %.2f MB → %s\n", n, total / 1048576.0, argv[1]);
    return 0;
}
