// ring_buffer.cpp — SPSC 无锁环形队列实现
// ============================================================================
// 对应 README §6.2：基于 std::atomic 的无锁 SPSC 环，临界区内禁止 mutex。
// 同时实现 audio_io.h 的 C-ABI mozart_ring_* 桥接。
#include "mozart/ring_buffer.hpp"
#include "mozart/audio_io.h"

#include <cstring>
#include <algorithm>
#include <limits>
#include <stdexcept>

namespace mozart {

uint32_t checked_item_size(uint32_t item_size) {
    if (item_size == 0) throw std::invalid_argument("ring item size must be non-zero");
    return item_size;
}

uint32_t next_pow2(uint32_t v) {
    constexpr uint32_t max_capacity = uint32_t{1} << 31;
    if (v == 0 || v > max_capacity) {
        throw std::length_error("ring capacity is not representable");
    }
    if (v < 2) return 2;
    --v;
    v |= v >> 1; v |= v >> 2; v |= v >> 4;
    v |= v >> 8; v |= v >> 16;
    return v + 1;
}

SpscRing::SpscRing(uint32_t capacity, uint32_t item_size)
    : item_size_(checked_item_size(item_size))
    , mask_(next_pow2(capacity) - 1)
{
    const uint32_t cap = mask_ + 1;
    if (static_cast<size_t>(cap) > std::numeric_limits<size_t>::max() / item_size_) {
        throw std::length_error("ring allocation size overflows size_t");
    }
    storage_.resize(static_cast<size_t>(cap) * item_size_);
}

SpscRing::~SpscRing() = default;

bool SpscRing::push(const void* data) noexcept {
    const uint64_t w = write_idx_.load(std::memory_order_relaxed);
    const uint64_t r = read_idx_.load(std::memory_order_acquire);

    // 队满判定：write - read == capacity
    if (w - r >= mask_ + 1) return false;

    const size_t pos = static_cast<size_t>(w & mask_) * item_size_;
    std::memcpy(storage_.data() + pos, data, item_size_);

    write_idx_.store(w + 1, std::memory_order_release);
    return true;
}

bool SpscRing::pop(void* out_data) noexcept {
    const uint64_t r = read_idx_.load(std::memory_order_relaxed);
    const uint64_t w = write_idx_.load(std::memory_order_acquire);

    if (r == w) return false;  // 队空

    const size_t pos = static_cast<size_t>(r & mask_) * item_size_;
    std::memcpy(out_data, storage_.data() + pos, item_size_);

    read_idx_.store(r + 1, std::memory_order_release);
    return true;
}

uint32_t SpscRing::readable_count() const noexcept {
    const uint64_t w = write_idx_.load(std::memory_order_acquire);
    const uint64_t r = read_idx_.load(std::memory_order_relaxed);
    return static_cast<uint32_t>(w - r);
}

} // namespace mozart

// =============================================================================
// C-ABI 桥接（audio_io.h 的 mozart_ring_*）
// =============================================================================
extern "C" {

MOZART_API mozart_ring_handle_t mozart_ring_create(uint32_t capacity, uint32_t item_size) {
    if (capacity == 0 || item_size == 0) return nullptr;
    try {
        auto* ring = new mozart::SpscRing(capacity, item_size);
        return static_cast<mozart_ring_handle_t>(ring);
    } catch (...) {
        return nullptr;
    }
}

MOZART_API void mozart_ring_destroy(mozart_ring_handle_t ring) {
    if (!ring) return;
    delete static_cast<mozart::SpscRing*>(ring);
}

MOZART_API bool mozart_ring_push(mozart_ring_handle_t ring, const void* data) {
    if (!ring || !data) return false;
    return static_cast<mozart::SpscRing*>(ring)->push(data);
}

MOZART_API bool mozart_ring_pop(mozart_ring_handle_t ring, void* out_data) {
    if (!ring || !out_data) return false;
    return static_cast<mozart::SpscRing*>(ring)->pop(out_data);
}

MOZART_API uint32_t mozart_ring_get_readable_count(mozart_ring_handle_t ring) {
    if (!ring) return 0;
    return static_cast<mozart::SpscRing*>(ring)->readable_count();
}

MOZART_API uint32_t mozart_ring_capacity(mozart_ring_handle_t ring) {
    if (!ring) return 0;
    return static_cast<mozart::SpscRing*>(ring)->capacity();
}

} // extern "C"
