// contract/mozart_ring_source.cpp
#include "contract/mozart_ring_source.hpp"
#include "utils/logging.hpp"

#include <stdexcept>
#include <string>

#if defined(_WIN32)
#  include <windows.h>
#else
#  include <dlfcn.h>
#endif

namespace mozart {

namespace {

#if defined(_WIN32)
using DLHandle = HMODULE;
inline DLHandle dl_open(const char* p)  { return LoadLibraryA(p); }
inline void*     dl_sym(DLHandle h, const char* n) {
    return reinterpret_cast<void*>(GetProcAddress(h, n));
}
inline void      dl_close(DLHandle h)   { if (h) FreeLibrary(h); }
inline std::string dl_error() { return "LoadLibrary failed"; }
#else
using DLHandle = void*;
inline DLHandle dl_open(const char* p)  { return dlopen(p, RTLD_NOW | RTLD_GLOBAL); }
inline void*    dl_sym(DLHandle h, const char* n) { return dlsym(h, n); }
inline void     dl_close(DLHandle h)    { if (h) dlclose(h); }
inline std::string dl_error() {
    const char* e = dlerror();
    return e ? std::string(e) : std::string("unknown dl error");
}
#endif

constexpr const char* kLibNames[] = {
#if defined(_WIN32)
    "mozart_pre.dll",
    "mozart_pre"
#else
    "libmozart_pre.so",
    "mozart_pre.so",
    "mozart_pre"
#endif
};

}  // namespace

MozartRingSource::MozartRingSource(const std::string& ring_name) {
    std::string last_err;
    for (const char* name : kLibNames) {
        dll_handle_ = dl_open(name);
        if (dll_handle_) break;
        last_err = dl_error();
    }

    if (!dll_handle_) {
        throw std::runtime_error(
            "MozartRingSource: cannot dlopen mozart_pre (" + last_err +
            "). Install the Mozart preprocessing library or use contract.source=mock.");
    }

    fn_attach_ = reinterpret_cast<mozart_post* (*)(const char*)>(
        dl_sym(static_cast<DLHandle>(dll_handle_), "mozart_post_attach"));
    fn_poll_ = reinterpret_cast<int (*)(mozart_post*, float*, mozart_frame_meta*, int)>(
        dl_sym(static_cast<DLHandle>(dll_handle_), "mozart_post_poll"));
    fn_detach_ = reinterpret_cast<void (*)(mozart_post*)>(
        dl_sym(static_cast<DLHandle>(dll_handle_), "mozart_post_detach"));

    if (!fn_attach_ || !fn_poll_ || !fn_detach_) {
        dl_close(static_cast<DLHandle>(dll_handle_));
        dll_handle_ = nullptr;
        throw std::runtime_error(
            "MozartRingSource: mozart_pre ABI symbols missing");
    }

    handle_ = fn_attach_(ring_name.c_str());
    if (!handle_) {
        dl_close(static_cast<DLHandle>(dll_handle_));
        dll_handle_ = nullptr;
        throw std::runtime_error(
            "MozartRingSource: mozart_post_attach(" + ring_name + ") failed");
    }
    MOZART_INFO("MozartRingSource: attached ring '%s'", ring_name.c_str());
}

MozartRingSource::~MozartRingSource() {
    if (handle_ && fn_detach_) fn_detach_(handle_);
    if (dll_handle_) dl_close(static_cast<DLHandle>(dll_handle_));
}

PollResult MozartRingSource::poll(ContractFrame& out_pcm, FrameMeta& out_meta,
                                  int timeout_us) {
    if (!handle_ || !fn_poll_) return PollResult::Error;
    mozart_frame_meta raw{};
    int rc = fn_poll_(handle_, out_pcm.data(), &raw, timeout_us);
    if (rc <= 0) return rc == 0 ? PollResult::Timeout : PollResult::Error;
    out_meta.pts_ns      = raw.pts_ns;
    out_meta.frame_idx   = raw.frame_idx;
    out_meta.vad_flag    = raw.vad_flag;
    out_meta.energy_db   = raw.energy_db;
    out_meta.conf        = raw.conf;
    out_meta.segment_id  = raw.segment_id;
    return PollResult::Ok;
}

}  // namespace mozart