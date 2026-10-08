#include "mozart/http_api.hpp"
#include <cstdlib>
#include <iostream>

int main(int argc, char** argv) {
    const auto port = static_cast<uint16_t>(argc > 1 ? std::atoi(argv[1]) : 18997);
    rvc::HttpApiServer server("127.0.0.1", port, nullptr);
    if (!server.start()) return 1;
    std::cin.get();
    server.stop();
}
