cmake_minimum_required(VERSION 3.18)
project(RVCVoiceChangerBackend VERSION 0.1.0 LANGUAGES CXX)

set(CMAKE_CXX_STANDARD 17)
set(CMAKE_CXX_STANDARD_REQUIRED ON)

# ──────────────────────────────────────────────────────────
# Options
# ──────────────────────────────────────────────────────────
option(USE_LIBTORCH "Enable libtorch for real RVC inference" OFF)
option(BUILD_TESTS "Build unit tests" ON)

# ──────────────────────────────────────────────────────────
# Dependencies
# ──────────────────────────────────────────────────────────
# yaml-cpp (for config parsing)
find_package(yaml-cpp QUIET)
if(NOT yaml-cpp_FOUND)
    include(FetchContent)
    FetchContent_Declare(
        yaml-cpp
        GIT_REPOSITORY https://github.com/jbeder/yaml-cpp.git
        GIT_TAG 0.8.0
    )
    FetchContent_MakeAvailable(yaml-cpp)
endif()

# nlohmann_json (header-only, usually available via find_package or FetchContent)
find_package(nlohmann_json 3.2.0 QUIET)
if(NOT nlohmann_json_FOUND)
    include(FetchContent)
    FetchContent_Declare(
        json
        GIT_REPOSITORY https://github.com/nlohmann/json.git
        GIT_TAG v3.11.3
    )
    FetchContent_MakeAvailable(json)
endif()

# spdlog (logging)
find_package(spdlog QUIET)
if(NOT spdlog_FOUND)
    include(FetchContent)
    FetchContent_Declare(
        spdlog
        GIT_REPOSITORY https://github.com/gabime/spdlog.git
        GIT_TAG v1.12.0
    )
    FetchContent_MakeAvailable(spdlog)
endif()

# pthread for native socket HTTP server
find_package(Threads REQUIRED)

# Shared IO subsystem (UDP/PipeWire/contracts/ring buffers)
set(MOZART_IO_BUILD_TESTS OFF CACHE BOOL "Build IO tests" FORCE)
add_subdirectory(
    ${CMAKE_CURRENT_SOURCE_DIR}/../IO
    ${CMAKE_CURRENT_BINARY_DIR}/mozart_io
)

# ──────────────────────────────────────────────────────────
# libtorch (optional, for real RVC inference)
# ──────────────────────────────────────────────────────────
if(USE_LIBTORCH)
    find_package(Torch REQUIRED)
    add_definitions(-DUSE_LIBTORCH)
    set(CMAKE_CXX_FLAGS "${CMAKE_CXX_FLAGS} ${TORCH_CXX_FLAGS}")
endif()

# ──────────────────────────────────────────────────────────
# Sources
# ──────────────────────────────────────────────────────────
set(SOURCES
    src/main.cpp
    src/rvc/audio_worker.cpp
    src/rvc/pipeline.cpp
    src/rvc/inferencer.cpp
    src/rvc/feature_extractor.cpp
    src/rvc/model_loader.cpp
    src/api/http_api.cpp
    src/utils/config.cpp
)

set(HEADERS
    include/common.hpp
    include/rvc/audio_worker.hpp
    include/rvc/pipeline.hpp
    include/rvc/inferencer.hpp
    include/rvc/feature_extractor.hpp
    include/rvc/model_loader.hpp
    include/api/http_api.hpp
    include/utils/config.hpp
)

# ──────────────────────────────────────────────────────────
# Executable
# ──────────────────────────────────────────────────────────
add_executable(rvc_backend ${SOURCES} ${HEADERS})

target_include_directories(rvc_backend PRIVATE
    ${CMAKE_CURRENT_SOURCE_DIR}/include
)

target_link_libraries(rvc_backend PRIVATE
    yaml-cpp
    nlohmann_json::nlohmann_json
    spdlog::spdlog
    Threads::Threads
    mozart_io
)

if(USE_LIBTORCH)
    target_link_libraries(rvc_backend PRIVATE ${TORCH_LIBRARIES})
endif()

# ──────────────────────────────────────────────────────────
# Compiler flags
# ──────────────────────────────────────────────────────────
if(CMAKE_CXX_COMPILER_ID MATCHES "GNU|Clang")
    target_compile_options(rvc_backend PRIVATE -Wall -Wextra -Wpedantic -O2)
elseif(MSVC)
    target_compile_options(rvc_backend PRIVATE /W4 /O2)
endif()

# ──────────────────────────────────────────────────────────
# Tests
# ──────────────────────────────────────────────────────────
if(BUILD_TESTS)
    enable_testing()
    add_executable(test_udp_loopback tests/test_udp_loopback.cpp
        src/rvc/audio_worker.cpp
        src/rvc/pipeline.cpp
        src/rvc/inferencer.cpp
        src/rvc/feature_extractor.cpp
        src/rvc/model_loader.cpp
    )
    target_include_directories(test_udp_loopback PRIVATE ${CMAKE_CURRENT_SOURCE_DIR}/include)
    target_link_libraries(test_udp_loopback PRIVATE
        mozart_io
        spdlog::spdlog
        Threads::Threads
        nlohmann_json::nlohmann_json
    )
    add_test(NAME rvc_udp_loopback COMMAND test_udp_loopback)
endif()
