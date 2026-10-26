#pragma once
#include <cerrno>
#include <cstdint>
#include <cstring>
#include <fcntl.h>
#include <stdexcept>
#include <string>
#include <sys/stat.h>
#include <unistd.h>

namespace mozart_host {
// O_NONBLOCK 防止 FIFO 打开阻塞；fstat 在截断前确认文件类型。
class RegularFile {
    int fd_ = -1;
    std::string path_;
    [[noreturn]] void error(const char* operation) const {
        throw std::runtime_error(std::string(operation) + " (" + path_ + "): " + std::strerror(errno));
    }
public:
    uint64_t size = 0;
    explicit RegularFile(const std::string& path, bool output) : path_(path) {
        fd_ = ::open(path.c_str(), O_CLOEXEC | O_NONBLOCK | (output ? O_WRONLY | O_CREAT : O_RDONLY), 0666);
        if (fd_ < 0) error("file open");
        try {
            struct stat st{}; if (::fstat(fd_, &st) < 0) error("file stat");
            if (!S_ISREG(st.st_mode)) throw std::runtime_error("regular file required: " + path);
            size = uint64_t(st.st_size);
            if (output && ::ftruncate(fd_, 0) < 0) error("file truncate");
        } catch (const std::exception&) { ::close(fd_); fd_ = -1; throw; }
    }
    RegularFile(const RegularFile&) = delete;
    RegularFile& operator=(const RegularFile&) = delete;
    ~RegularFile() { if (fd_ >= 0) ::close(fd_); }
    void seek(uint64_t position) {
        if (::lseek(fd_, off_t(position), SEEK_SET) < 0) error("file seek");
    }
    void read(uint8_t* p, size_t n) {
        while (n) {
            ssize_t count = ::read(fd_, p, n);
            if (count < 0) { if (errno == EINTR) continue; error("file read"); }
            if (!count) throw std::runtime_error("truncated file: " + path_);
            p += count; n -= size_t(count);
        }
    }
    void write(const uint8_t* p, size_t n) {
        while (n) {
            ssize_t count = ::write(fd_, p, n);
            if (count < 0) { if (errno == EINTR) continue; error("file write"); }
            if (!count) throw std::runtime_error("zero file write: " + path_);
            p += count; n -= size_t(count);
        }
    }
    void write(const std::string& s) { write(reinterpret_cast<const uint8_t*>(s.data()), s.size()); }
    void close() {
        int fd = fd_; fd_ = -1;
        if (::close(fd) < 0) error("file close");
    }
};
}
