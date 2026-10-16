/* 持续处理单声道 PCM。使用系统音频过滤库，避免完整 FFmpeg 程序的启动成本。 */
#define _POSIX_C_SOURCE 200809L
#include <errno.h>
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <unistd.h>

#include <libavfilter/avfilter.h>
#include <libavfilter/buffersink.h>
#include <libavfilter/buffersrc.h>
#include <libavutil/channel_layout.h>
#include <libavutil/error.h>
#include <libavutil/frame.h>
#include <libavutil/opt.h>

static int drain(AVFilterContext *sink, AVFrame *output, FILE *destination) {
    int code;
    while ((code = av_buffersink_get_frame(sink, output)) >= 0) {
        if (output->format != AV_SAMPLE_FMT_S16 || output->ch_layout.nb_channels != 1) {
            av_frame_unref(output);
            return AVERROR(EINVAL);
        }
        size_t bytes = (size_t)output->nb_samples * sizeof(int16_t);
        if (fwrite(output->data[0], 1, bytes, destination) != bytes) {
            av_frame_unref(output);
            return AVERROR(EIO);
        }
        av_frame_unref(output);
    }
    return code;
}

static int filter_pcm(FILE *source_stream, FILE *destination, long rate, double tempo) {
    setvbuf(source_stream, NULL, _IONBF, 0);
    setvbuf(destination, NULL, _IONBF, 0);
    av_log_set_level(AV_LOG_ERROR);
    AVFilterGraph *graph = avfilter_graph_alloc();
    AVFrame *input = av_frame_alloc(), *output = av_frame_alloc();
    AVFilterContext *source = NULL, *filter = NULL, *sink = NULL;
    int code = AVERROR(ENOMEM);
    if (!graph || !input || !output) goto done;
    graph->nb_threads = 1;
    char args[160];
    snprintf(args, sizeof(args), "time_base=1/%ld:sample_rate=%ld:sample_fmt=s16:channel_layout=mono", rate, rate);
    code = avfilter_graph_create_filter(&source, avfilter_get_by_name("abuffer"), "input", args, NULL, graph);
    if (code < 0) goto done;
    snprintf(args, sizeof(args), "%.3f", tempo);
    code = avfilter_graph_create_filter(&filter, avfilter_get_by_name("atempo"), "tempo", args, NULL, graph);
    if (code < 0) goto done;
    code = avfilter_graph_create_filter(&sink, avfilter_get_by_name("abuffersink"), "output", NULL, NULL, graph);
    if (code < 0) goto done;
    code = avfilter_link(source, 0, filter, 0);
    if (code < 0) goto done;
    code = avfilter_link(filter, 0, sink, 0);
    if (code < 0) goto done;
    code = avfilter_graph_config(graph, NULL);
    if (code < 0) goto done;
    unsigned char pcm[2048];
    int64_t pts = 0;
    for (;;) {
        size_t count = fread(pcm, 1, sizeof(pcm), source_stream);
        if (!count) {
            if (ferror(source_stream)) { code = AVERROR(EIO); goto done; }
            break;
        }
        if (count % sizeof(int16_t)) { code = AVERROR(EINVAL); goto done; }
        input->nb_samples = (int)(count / sizeof(int16_t));
        input->format = AV_SAMPLE_FMT_S16;
        input->sample_rate = (int)rate;
        input->ch_layout = (AVChannelLayout)AV_CHANNEL_LAYOUT_MONO;
        input->pts = pts;
        pts += input->nb_samples;
        code = av_frame_get_buffer(input, 0);
        if (code < 0) goto done;
        memcpy(input->data[0], pcm, count);
        code = av_buffersrc_add_frame_flags(source, input, AV_BUFFERSRC_FLAG_KEEP_REF);
        av_frame_unref(input);
        if (code < 0) goto done;
        code = drain(sink, output, destination);
        if (code != AVERROR(EAGAIN)) goto done;
    }
    code = av_buffersrc_add_frame_flags(source, NULL, 0);
    if (code < 0) goto done;
    code = drain(sink, output, destination);
    if (code == AVERROR_EOF) code = 0;
done:
    if (code < 0) {
        char message[AV_ERROR_MAX_STRING_SIZE];
        av_strerror(code, message, sizeof(message));
        fprintf(stderr, "Tempo filter failed: %s\n", message);
    }
    av_frame_free(&input);
    av_frame_free(&output);
    avfilter_graph_free(&graph);
    return code < 0 ? 1 : 0;
}

/* 调用者移交两个文件描述符。函数在所有退出路径关闭描述符。 */
int mozart_tempo_filter(int input_fd, int output_fd, int rate, double tempo) {
    FILE *source = fdopen(input_fd, "rb");
    FILE *destination = fdopen(output_fd, "wb");
    if (!source || !destination) {
        if (source) fclose(source); else close(input_fd);
        if (destination) fclose(destination); else close(output_fd);
        return 1;
    }
    int code = 1;
    if (rate >= 8000 && rate <= 48000 && tempo >= 1.0 && tempo <= 1.15)
        code = filter_pcm(source, destination, rate, tempo);
    fclose(source);
    if (fclose(destination)) code = 1;
    return code;
}

#ifndef MOZART_TEMPO_LIBRARY
int main(int argc, char **argv) {
    char *end = NULL;
    if (argc != 3) {
        fprintf(stderr, "Usage: mozart-tempo SAMPLE_RATE TEMPO\n");
        return 2;
    }
    long rate = strtol(argv[1], &end, 10);
    if (*end || rate < 8000 || rate > 48000) return 2;
    double tempo = strtod(argv[2], &end);
    if (*end || !(tempo >= 1.0 && tempo <= 1.15)) return 2;
    return filter_pcm(stdin, stdout, rate, tempo);
}
#endif
