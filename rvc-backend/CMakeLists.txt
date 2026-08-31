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

# Shared IO subsystem (UDP/PipeWire/contracts/ring buffers).
if(NOT TARGET mozart_io)
    set(MOZART_IO_BUILD_TESTS OFF CACHE BOOL "Build IO tests" FORCE)
    add_subdirectory(
        ${CMAKE_CURRENT_SOURCE_DIR}/../IO
        ${CMAKE_CURRENT_BINARY_DIR}/mozart_io
    )
endif()

# FILE_RVC is a data-plane worker and owns the preprocessing lifecycle.
if(NOT TARGET mozart_pre_core)
    add_subdirectory(
        ${CMAKE_CURRENT_SOURCE_DIR}/../preprocessor
        ${CMAKE_CURRENT_BINARY_DIR}/mozart_pre
    )
endif()

if(NOT TARGET mozart_api)
    add_subdirectory(
        ${CMAKE_CURRENT_SOURCE_DIR}/../api
        ${CMAKE_CURRENT_BINARY_DIR}/mozart_api
    )
endif()

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
# TensorRT 直载（.engine 优先于 .onnx）
# ══════════════════════════════════════════════════════════
option(USE_TENSORRT "Enable TensorRT engine direct loading" ON)
add_library(trt_flags INTERFACE)
if(USE_TENSORRT)
    find_path(TENSORRT_INCLUDE_DIR NvInfer.h
        PATHS /usr/include/aarch64-linux-gnu /usr/include/x86_64-linux-gnu /usr/include)
    find_path(CUDA_INCLUDE_DIR cuda_runtime_api.h
        PATHS /usr/local/cuda/include /usr/local/cuda-13.2/include /usr/include)
    find_library(TENSORRT_LIBRARY nvinfer
        PATHS /usr/lib/aarch64-linux-gnu /usr/lib/x86_64-linux-gnu)
    find_library(CUDART_LIBRARY cudart
        PATHS /usr/local/cuda/lib64 /usr/local/cuda-13.2/lib64 /usr/lib/aarch64-linux-gnu)
    if(TENSORRT_INCLUDE_DIR AND TENSORRT_LIBRARY AND CUDART_LIBRARY)
        target_include_directories(trt_flags INTERFACE ${TENSORRT_INCLUDE_DIR} ${CUDA_INCLUDE_DIR})
        target_compile_definitions(trt_flags INTERFACE USE_TENSORRT=1)
        target_link_libraries(trt_flags INTERFACE ${TENSORRT_LIBRARY} ${CUDART_LIBRARY})
        message(STATUS "TensorRT enabled: ${TENSORRT_LIBRARY}")
    else()
        message(STATUS "TensorRT not found — engines fall back to ONNX Runtime")
    endif()
endif()

# ══════════════════════════════════════════════════════════
# Sources
# ══════════════════════════════════════════════════════════
set(RUNTIME_SOURCES
    src/rvc/audio_worker.cpp
    src/rvc/streaming_pipeline.cpp
    src/rvc/trt_engine.cpp
    src/rvc/pipeline.cpp
    src/rvc/inferencer.cpp
    src/rvc/feature_extractor.cpp
    src/rvc/model_loader.cpp
    src/rvc/onnx_engine.cpp
    src/rvc/index_search.cpp
    src/state/mode_controller.cpp
    src/state/realtime_rvc_worker.cpp
    src/state/file_rvc_worker.cpp
    src/state/log_store.cpp
    src/utils/config.cpp
)

set(HEADERS
    include/common.hpp
    include/rvc/audio_worker.hpp
    include/rvc/streaming_pipeline.hpp
    include/rvc/pipeline.hpp
    include/rvc/inferencer.hpp
    include/rvc/feature_extractor.hpp
    include/rvc/model_loader.hpp
    include/rvc/onnx_engine.hpp
    include/rvc/index_search.hpp
    include/state/mode_controller.hpp
    include/state/realtime_rvc_worker.hpp
    include/state/file_rvc_worker.hpp
    include/state/control_plane.hpp
    include/state/log_store.hpp
    include/utils/config.hpp
)

# ══════════════════════════════════════════════════════════
# Runtime library
# ══════════════════════════════════════════════════════════
add_library(mozart_rvc_runtime STATIC ${RUNTIME_SOURCES} ${HEADERS})

target_include_directories(mozart_rvc_runtime PUBLIC
    ${CMAKE_CURRENT_SOURCE_DIR}/include
    ${CMAKE_CURRENT_SOURCE_DIR}/../preprocessor
)

target_link_libraries(mozart_rvc_runtime PRIVATE trt_flags)
target_link_libraries(mozart_rvc_runtime PUBLIC
    yaml-cpp
    nlohmann_json::nlohmann_json
    spdlog::spdlog
    Threads::Threads
    mozart_io
    mozart_pre_core
)

if(USE_ONNX AND ONNX_RUNTIME_LIB)
    target_compile_definitions(mozart_rvc_runtime PUBLIC USE_ONNX)
    target_include_directories(mozart_rvc_runtime PUBLIC ${ONNX_RUNTIME_INCLUDE_DIR})
    target_link_libraries(mozart_rvc_runtime PUBLIC ${ONNX_RUNTIME_LIB})
endif()

if(USE_LIBTORCH)
    target_link_libraries(mozart_rvc_runtime PUBLIC ${TORCH_LIBRARIES})
endif()

# Compatibility executable for current deployment scripts. New deployments
# should run mozart_stated, which is defined in state/.
add_executable(rvc_backend src/main.cpp)
target_link_libraries(rvc_backend PRIVATE mozart_rvc_runtime mozart_api)

# ══════════════════════════════════════════════════════════
# Compiler flags
# ══════════════════════════════════════════════════════════
if(CMAKE_CXX_COMPILER_ID MATCHES "GNU|Clang")
    target_compile_options(mozart_rvc_runtime PRIVATE -Wall -Wextra -Wpedantic -O2)
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
        src/rvc/streaming_pipeline.cpp
        src/rvc/trt_engine.cpp
        src/rvc/pipeline.cpp
        src/rvc/inferencer.cpp
        src/rvc/feature_extractor.cpp
        src/rvc/model_loader.cpp
        src/rvc/onnx_engine.cpp
        src/rvc/index_search.cpp
    )
    target_include_directories(test_udp_loopback PRIVATE ${CMAKE_CURRENT_SOURCE_DIR}/include)
    if(USE_ONNX AND ONNX_RUNTIME_LIB)
        target_include_directories(test_udp_loopback PRIVATE ${ONNX_RUNTIME_INCLUDE_DIR})
    endif()
    target_link_libraries(test_udp_loopback PRIVATE
        trt_flags
        mozart_io
        spdlog::spdlog
        Threads::Threads
        nlohmann_json::nlohmann_json
        ${ONNX_RUNTIME_LIB}
    )
    if(USE_ONNX AND ONNX_RUNTIME_LIB)
        target_include_directories(test_udp_loopback PRIVATE ${ONNX_RUNTIME_INCLUDE_DIR})
        target_link_libraries(test_udp_loopback PRIVATE ${ONNX_RUNTIME_LIB})
    endif()
    add_test(NAME rvc_udp_loopback COMMAND test_udp_loopback)

    add_executable(test_streaming tests/test_streaming.cpp
        src/rvc/streaming_pipeline.cpp
    )
    target_include_directories(test_streaming PRIVATE ${CMAKE_CURRENT_SOURCE_DIR}/include ${CMAKE_CURRENT_SOURCE_DIR}/../IO/include)
    if(USE_ONNX AND ONNX_RUNTIME_LIB)
        target_include_directories(test_streaming PRIVATE ${ONNX_RUNTIME_INCLUDE_DIR})
    endif()
    target_link_libraries(test_streaming PRIVATE
        spdlog::spdlog
        Threads::Threads
        ${ONNX_RUNTIME_LIB}
    )
    add_test(NAME test_streaming COMMAND test_streaming)

    add_executable(test_feature_extractor tests/test_feature_extractor.cpp
        src/rvc/feature_extractor.cpp
        src/rvc/onnx_engine.cpp
        src/rvc/trt_engine.cpp
    )
    target_include_directories(test_feature_extractor PRIVATE ${CMAKE_CURRENT_SOURCE_DIR}/include)
    if(USE_ONNX AND ONNX_RUNTIME_LIB)
        target_include_directories(test_feature_extractor PRIVATE ${ONNX_RUNTIME_INCLUDE_DIR})
    endif()
    target_link_libraries(test_feature_extractor PRIVATE
        trt_flags
        spdlog::spdlog
        ${ONNX_RUNTIME_LIB}
    )
    if(USE_ONNX AND ONNX_RUNTIME_LIB)
        target_include_directories(test_feature_extractor PRIVATE ${ONNX_RUNTIME_INCLUDE_DIR})
        target_link_libraries(test_feature_extractor PRIVATE ${ONNX_RUNTIME_LIB})
    endif()
    add_test(NAME test_feature_extractor COMMAND test_feature_extractor)

    add_executable(test_inferencer tests/test_inferencer.cpp
        src/rvc/inferencer.cpp
        src/rvc/feature_extractor.cpp
        src/rvc/model_loader.cpp
        src/rvc/onnx_engine.cpp
        src/rvc/index_search.cpp
    )
    target_include_directories(test_inferencer PRIVATE ${CMAKE_CURRENT_SOURCE_DIR}/include)
    if(USE_ONNX AND ONNX_RUNTIME_LIB)
        target_include_directories(test_inferencer PRIVATE ${ONNX_RUNTIME_INCLUDE_DIR})
    endif()
    target_link_libraries(test_inferencer PRIVATE
        spdlog::spdlog
        nlohmann_json::nlohmann_json
        ${ONNX_RUNTIME_LIB}
    )
    if(USE_ONNX AND ONNX_RUNTIME_LIB)
        target_include_directories(test_inferencer PRIVATE ${ONNX_RUNTIME_INCLUDE_DIR})
        target_link_libraries(test_inferencer PRIVATE ${ONNX_RUNTIME_LIB})
    endif()
    add_test(NAME test_inferencer COMMAND test_inferencer)
endif()
