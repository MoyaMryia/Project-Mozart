// test_ring_buffer.cpp — SPSC 无锁环功能测试
// ============================================================================
// 覆盖：
//   1. 基本 push/pop 单帧
//   2. 队满 push 返回 false，队空 pop 返回 false
//   3. readable_count 正确性
//   4. SPSC 多线程吞吐（生产者/消费者并发）
//   5. C-ABI mozart_ring_* 接口
#include "mozart/ring_buffer.hpp"
#include "mozart/audio_io.h"
#include "mozart/frame_meta.h"

#include <cassert>
#include <cstdio>
#include <thread>
#include <atomic>
#include <vector>
#include <cstring>

static int g_failures = 0;
#define CHECK(cond) do { \
    if (!(cond)) { \
        std::printf("FAIL [%s:%d] %s\n", __FILE__, __LINE__, #cond); \
        ++g_failures; \
    } \
} while (0)

static void test_basic_push_pop() {
    mozart::SpscRing ring(8, sizeof(uint32_t));
    CHECK(ring.capacity() >= 8);
    CHECK(ring.readable_count() == 0);

    for (uint32_t i = 0; i < 8; ++i) {
        CHECK(ring.push(&i));
    }
    CHECK(ring.readable_count() == 8);

    uint32_t v = 0;
    CHECK(!ring.push(&v));  // 队满
    for (uint32_t i = 0; i < 8; ++i) {
        CHECK(ring.pop(&v));
        CHECK(v == i);
    }
    CHECK(ring.readable_count() == 0);
    CHECK(!ring.pop(&v));  // 队空
    std::printf("[OK] test_basic_push_pop\n");
}

static void test_wraparound() {
    mozart::SpscRing ring(4, sizeof(uint32_t));
    uint32_t v;
    // 多次环绕
    for (int round = 0; round < 100; ++round) {
        for (uint32_t i = 0; i < 4; ++i) {
            uint32_t x = round * 4 + i;
            CHECK(ring.push(&x));
        }
        for (uint32_t i = 0; i < 4; ++i) {
            CHECK(ring.pop(&v));
            CHECK(v == static_cast<uint32_t>(round * 4 + i));
        }
    }
    std::printf("[OK] test_wraparound\n");
}

static void test_spsc_concurrent() {
    constexpr int N = 100000;
    mozart::SpscRing ring(1024, sizeof(uint32_t));

    std::atomic<bool> producer_done{false};
    std::atomic<int>  mismatches{0};

    std::thread producer([&] {
        for (int i = 0; i < N; ++i) {
            uint32_t v = static_cast<uint32_t>(i);
            while (!ring.push(&v)) {
                std::this_thread::yield();
            }
        }
        producer_done = true;
    });

    std::thread consumer([&] {
        int received = 0;
        while (received < N) {
            uint32_t v;
            if (ring.pop(&v)) {
                if (v != static_cast<uint32_t>(received)) ++mismatches;
                ++received;
            } else {
                std::this_thread::yield();
            }
        }
    });

    producer.join();
    consumer.join();
    CHECK(mismatches.load() == 0);
    std::printf("[OK] test_spsc_concurrent (%d frames)\n", N);
}

static void test_cabi() {
    mozart_ring_handle_t ring = mozart_ring_create(4, sizeof(mozart_input_frame_t));
    CHECK(ring != nullptr);

    mozart_input_frame_t in{};
    in.meta.frame_idx = 42;
    in.pcm[0] = 0.5f;
    CHECK(mozart_ring_push(ring, &in));

    mozart_input_frame_t out{};
    CHECK(mozart_ring_pop(ring, &out));
    CHECK(out.meta.frame_idx == 42);
    CHECK(out.pcm[0] == 0.5f);

    CHECK(mozart_ring_get_readable_count(ring) == 0);
    CHECK(mozart_ring_capacity(ring) >= 4);

    mozart_ring_destroy(ring);
    std::printf("[OK] test_capi\n");
}

int main() {
    test_basic_push_pop();
    test_wraparound();
    test_spsc_concurrent();
    test_cabi();

    if (g_failures == 0) {
        std::printf("\nAll ring_buffer tests passed.\n");
        return 0;
    }
    std::printf("\n%d failures.\n", g_failures);
    return 1;
}
