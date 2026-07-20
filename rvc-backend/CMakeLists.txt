cmake_minimum_required(VERSION 3.18)
project(RVCVoiceChangerBackend VERSION 0.1.0 LANGUAGES CXX)

set(CMAKE_CXX_STANDARD 17)
set(CMAKE_CXX_STANDARD_REQUIRED ON)

# ══════════════════════════════════════════════════════════
# Options
# ══════════════════════════════════════════════════════════
option(USE_LIBTORCH "Enable libtorch for real RVC inference" OFF)
option(USE_ONNX "Enable ONNX Runtime for real RVC inference" ON)
option(BUILD_TESTS "Build unit tests" ON)

# ══════════════════════════════════════════════════════════
# Dependencies
# ══════════════════════════════════════════════════════════
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

# nlohmann_json (header-only)
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

# ══════════════════════════════════════════════════════════
# ONNX Runtime
# ══════════════════════════════════════════════════════════
if(USE_ONNX)
    # JetPack default paths
    set(ONNX_RUNTIME_PATH "/usr/lib/aarch64-linux-gnu" CACHE PATH
        "ONNX Runtime install path")
    set(ONNX_RUNTIME_INCLUDE_DIR "/usr/include/onnxruntime" CACHE PATH
        "ONNX Runtime header directory")

    find_library(ONNX_RUNTIME_LIB onnxruntime
        PATHS ${ONNX_RUNTIME_PATH}
        NO_DEFAULT_PATH
    )

    if(NOT ONNX_RUNTIME_LIB)
        # Try system default paths
        find_library(ONNX_RUNTIME_LIB onnxruntime)
    endif()

    if(ONNX_RUNTIME_LIB)
        message(STATUS "Found ONNX Runtime: ${ONNX_RUNTIME_LIB}")
        add_definitions(-DUSE_ONNX)
    else()
        message(WARNING "ONNX Runtime not found. "
            "Install with: apt install libonnxruntime-dev\n"
            "Or set ONNX_RUNTIME_PATH to custom install location.\n"
            "Reverting to USE_ONNX=OFF")
        set(USE_ONNX OFF)
    endif()
endif()

# ══════════════════════════════════════════════════════════
# libtorch (optional)
# ══════════════════════════════════════════════════════════
if(USE_LIBTORCH)
    find_package(Torch REQUIRED)
    add_definitions(-DUSE_LIBTORCH)
    set(CMAKE_CXX_FLAGS "${CMAKE_CXX_FLAGS} ${TORCH_CXX_FLAGS}")
endif()

# ══════════════════════════════════════════════════════════
# Sources
# ══════════════════════════════════════════════════════════
set(SOURCES
    src/main.cpp
    src/rvc/audio_worker.cpp
    src/rvc/pipeline.cpp
    src/rvc/inferencer.cpp
    src/rvc/feature_extractor.cpp
    src/rvc/model_loader.cpp
    src/rvc/onnx_engine.cpp
    src/rvc/index_search.cpp
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
    include/rvc/onnx_engine.hpp
    include/rvc/index_search.hpp
    include/api/http_api.hpp
    include/utils/config.hpp
)

# ══════════════════════════════════════════════════════════
# Executable
# ══════════════════════════════════════════════════════════
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

if(USE_ONNX AND ONNX_RUNTIME_LIB)
    target_include_directories(rvc_backend PRIVATE ${ONNX_RUNTIME_INCLUDE_DIR})
    target_link_libraries(rvc_backend PRIVATE ${ONNX_RUNTIME_LIB})
endif()

if(USE_LIBTORCH)
    target_link_libraries(rvc_backend PRIVATE ${TORCH_LIBRARIES})
endif()

# ══════════════════════════════════════════════════════════
# Compiler flags
# ══════════════════════════════════════════════════════════
if(CMAKE_CXX_COMPILER_ID MATCHES "GNU|Clang")
    target_compile_options(rvc_backend PRIVATE -Wall -Wextra -Wpedantic -O2)
elseif(MSVC)
    target_compile_options(rvc_backend PRIVATE /W4 /O2)
endif()

# ══════════════════════════════════════════════════════════
# Tests
# ══════════════════════════════════════════════════════════
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

    add_executable(test_feature_extractor tests/test_feature_extractor.cpp
        src/rvc/feature_extractor.cpp
    )
    target_include_directories(test_feature_extractor PRIVATE ${CMAKE_CURRENT_SOURCE_DIR}/include)
    target_link_libraries(test_feature_extractor PRIVATE
        spdlog::spdlog
    )
    add_test(NAME test_feature_extractor COMMAND test_feature_extractor)

    add_executable(test_inferencer tests/test_inferencer.cpp
        src/rvc/inferencer.cpp
        src/rvc/feature_extractor.cpp
        src/rvc/model_loader.cpp
    )
    target_include_directories(test_inferencer PRIVATE ${CMAKE_CURRENT_SOURCE_DIR}/include)
    target_link_libraries(test_inferencer PRIVATE
        spdlog::spdlog
        nlohmann_json::nlohmann_json
    )
    add_test(NAME test_inferencer COMMAND test_inferencer)
endif()
