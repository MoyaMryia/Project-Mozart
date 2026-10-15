// 比较相同缓存文件的逐字节迭代读取与一次定长读取。
#include <algorithm>
#include <chrono>
#include <filesystem>
#include <fstream>
#include <iostream>
#include <iterator>
#include <stdexcept>
#include <string>

int main(int argc, char** argv) {
    if (argc != 2) return 2;
    const auto bytes = std::filesystem::file_size(argv[1]);
    std::string expected;
    for (int round=0; round<4; ++round) {
        for (int step=0; step<2; ++step) {
            const bool bulk = (round % 2 ? 1-step : step) != 0;
            const auto began = std::chrono::steady_clock::now();
            std::ifstream file(argv[1], std::ios::binary);
            std::string data;
            if (bulk) {
                data.resize(bytes);
                if (!file.read(data.data(), bytes)) throw std::runtime_error("Read failed");
            } else {
                data.assign(std::istreambuf_iterator<char>(file), std::istreambuf_iterator<char>());
            }
            const double elapsed = std::chrono::duration<double, std::milli>(
                std::chrono::steady_clock::now()-began).count();
            if (expected.empty()) expected = data;
            if (data != expected) throw std::runtime_error("File contents differ");
            std::cout << "{\"round\":" << round << ",\"bulk\":" << (bulk?"true":"false")
                      << ",\"bytes\":" << data.size() << ",\"elapsed_ms\":" << elapsed << "}\n";
        }
    }
}
