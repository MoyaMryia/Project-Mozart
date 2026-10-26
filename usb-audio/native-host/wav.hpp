#pragma once
#include "runtime.hpp"
#include <cstring>

namespace mozart_host {
class WavReader {
    RegularFile in_;
    uint64_t remaining_ = 0;
public:
    uint64_t frames = 0;
    explicit WavReader(const std::string& path) : in_(path, false) {
        auto end = in_.size;
        require(end >= 12, "short WAV header");
        std::array<uint8_t, 12> h{}; read_exact(h.data(), h.size());
        require(!std::memcmp(h.data(), "RIFF", 4) && !std::memcmp(h.data() + 8, "WAVE", 4), "input must be RIFF WAVE");
        uint64_t riff_end = uint64_t(u32(h.data() + 4)) + 8;
        require(riff_end >= 12 && riff_end <= uint64_t(end), "invalid WAV RIFF length");
        bool format = false, data = false; uint64_t data_at = 0;
        uint64_t position = 12;
        while (position < riff_end) {
            require(riff_end - position >= 8, "truncated WAV chunk header");
            std::array<uint8_t, 8> chunk{}; read_exact(chunk.data(), chunk.size()); position += 8;
            uint64_t n = u32(chunk.data() + 4), padded = n + (n & 1);
            require(padded <= riff_end - position, "WAV chunk exceeds RIFF length");
            if (!std::memcmp(chunk.data(), "fmt ", 4)) {
                require(!format && n >= 16, "invalid WAV fmt chunk");
                std::array<uint8_t, 16> f{}; read_exact(f.data(), f.size());
                require(u16(f.data()) == 1 && u16(f.data() + 2) == MOZART_USB_CHANNELS &&
                        u32(f.data() + 4) == MOZART_USB_RATE && u32(f.data() + 8) == MOZART_USB_RATE * FrameBytes &&
                        u16(f.data() + 12) == FrameBytes && u16(f.data() + 14) == MOZART_USB_SAMPLE_BYTES * 8,
                        "WAV must be 48 kHz stereo S16 PCM");
                format = true;
            } else if (!std::memcmp(chunk.data(), "data", 4)) {
                require(!data && n % FrameBytes == 0, "invalid WAV data chunk");
                data = true; data_at = position; remaining_ = n;
            }
            position += padded; in_.seek(position);
        }
        require(format && data, "WAV missing fmt or data chunk");
        frames = remaining_ / FrameBytes; in_.seek(data_at);
    }
    void read_exact(uint8_t* p, size_t n) {
        in_.read(p, n);
    }
    unsigned read(std::vector<uint8_t>& block, unsigned requested_frames) {
        unsigned count = unsigned(std::min<uint64_t>(requested_frames, remaining_ / FrameBytes));
        std::fill(block.begin(), block.end(), 0);
        read_exact(block.data(), size_t(count) * FrameBytes); remaining_ -= uint64_t(count) * FrameBytes;
        return count;
    }
};
class WavWriter {
    RegularFile out_;
    uint64_t bytes_ = 0;
public:
    explicit WavWriter(const std::string& path) : out_(path, true) { header(); }
    void header() {
        std::array<uint8_t, 44> h{};
        std::memcpy(h.data(), "RIFF", 4); put32(h.data() + 4, uint32_t(bytes_ + 36));
        std::memcpy(h.data() + 8, "WAVEfmt ", 8); put32(h.data() + 16, 16);
        put16(h.data() + 20, 1); put16(h.data() + 22, MOZART_USB_CHANNELS);
        put32(h.data() + 24, MOZART_USB_RATE); put32(h.data() + 28, MOZART_USB_RATE * FrameBytes);
        put16(h.data() + 32, FrameBytes); put16(h.data() + 34, MOZART_USB_SAMPLE_BYTES * 8);
        std::memcpy(h.data() + 36, "data", 4); put32(h.data() + 40, uint32_t(bytes_));
        out_.seek(0); out_.write(h.data(), h.size());
    }
    void write(const uint8_t* p, unsigned frames) {
        uint64_t n = uint64_t(frames) * FrameBytes;
        require(n <= UINT32_MAX - 36 - bytes_, "WAV exceeds RIFF size limit");
        out_.write(p, size_t(n)); bytes_ += n;
    }
    void finish() {
        header(); out_.close();
    }
};
}
