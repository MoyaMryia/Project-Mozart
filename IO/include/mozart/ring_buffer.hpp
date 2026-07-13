// ring_buffer.hpp — SPSC 无锁环形队列（C++ 接口）
// ============================================================================
// 单生产者单消费者（Single-Producer Single-Consumer）无锁环形缓冲区。
// 对应 README §6.2：
//   - 基于 std::atomic 的 write_index / read_index，临界区内禁止 mutex
//   - capacity 向上取整到 2 的幂，用位掩码代替模运算
//   - write_index 与 read_index 分离到不同缓存行，避免 false sharing
//
// C-ABI（audio_io.h 的 mozart_ring_*）由本类的 C 桥接函数实现。
#ifndef MOZART_RING_BUFFER_HPP
#define MOZART_RING_BUFFER_HPP

#include <atomic>
#include <cstddef>
#include <cstdint>
#include <vector>
#include <new>

namespace mozart {

// 缓存行大小（Jetson Orin Cortex-A78AE 为 64 字节；x86 同样 64）
inline constexpr std::size_t kCacheLineSize = 64;

class SpscRing {
public:
    // capacity 会被向上取整到 2 的幂；item_size 为每帧字节数
    SpscRing(uint32_t capacity, uint32_t item_size);
    ~SpscRing();

    SpscRing(const SpscRing&)            = delete;
    SpscRing& operator=(const SpscRing&) = delete;
    SpscRing(SpscRing&&)                 = delete;
    SpscRing& operator=(SpscRing&&)      = delete;

    // 生产者线程调用：队满返回 false（不覆盖）
    bool push(const void* data) noexcept;

    // 消费者线程调用：队空返回 false
    bool pop(void* out_data) noexcept;

    // 消费者侧观察的未读帧数（用于丢帧追赶判定，参见 README §6.4）
    uint32_t readable_count() const noexcept;

    uint32_t capacity()  const noexcept { return mask_ + 1; }
    uint32_t item_size() const noexcept { return item_size_; }

private:
    const uint32_t item_size_;   // 每帧字节数
    const uint32_t mask_;        // capacity - 1（capacity 为 2 的幂）
    std::vector<unsigned char> storage_;  // 容量 * item_size 字节

    // 生产者侧（写）
    alignas(kCacheLineSize) std::atomic<uint64_t> write_idx_{0};

    // 消费者侧（读）— 独立缓存行，避免与 write_idx_ 互相 invalidate
    alignas(kCacheLineSize) std::atomic<uint64_t> read_idx_{0};
};

} // namespace mozart

#endif // MOZART_RING_BUFFER_HPP
