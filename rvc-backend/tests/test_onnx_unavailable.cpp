#include "rvc/onnx_engine.hpp"
#include <fstream>
#include <unistd.h>

int main() {
    char name[] = "/tmp/mozart-onnx-unavailable-XXXXXX";
    const int fd = mkstemp(name);
    if (fd < 0) return 1;
    close(fd);
    std::ofstream(name) << "invalid ONNX fixture";
    rvc::OnnxEngine engine;
    const bool accepted = engine.load(name) || engine.loaded();
    std::filesystem::remove(name);
    return accepted ? 1 : 0;
}
