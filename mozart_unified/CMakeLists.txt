cmake_minimum_required(VERSION 3.16)
project(mozart_unified VERSION 0.2.0 LANGUAGES CXX)

set(CMAKE_CXX_STANDARD 17)
set(CMAKE_CXX_STANDARD_REQUIRED ON)
set(CMAKE_EXPORT_COMPILE_COMMANDS ON)

# ── Options ───────────────────────────────────────────────────────────────────
option(MOZART_BUILD_TESTS    "Build test executables"           OFF)
option(MOZART_ENABLE_PIPEWIRE "Enable PipeWire virtual source"  OFF)
option(MOZART_ENABLE_ONNXRUNTIME "Enable ONNX Runtime inference" OFF)
option(MOZART_ENABLE_UDP     "Enable UDP transport"             ON)
option(MOZART_ENABLE_HTTP    "Enable HTTP API server"           ON)

# ── Dependencies ──────────────────────────────────────────────────────────────
# spdlog (logging)
find_package(spdlog QUIET)
if(NOT spdlog_FOUND)
    include(FetchContent)
    FetchContent_Declare(
        spdlog
        GIT_REPOSITORY https://github.com/gabime/spdlog.git
        GIT_TAG        v1.12.0
    )
    FetchContent_MakeAvailable(spdlog)
endif()

# nlohmann/json (model config parsing)
include(FetchContent)
FetchContent_Declare(
    nlohmann_json
    GIT_REPOSITORY https://github.com/nlohmann/json.git
    GIT_TAG        v3.11.3
)
FetchContent_MakeAvailable(nlohmann_json)

# ── Core library ──────────────────────────────────────────────────────────────
add_library(mozart_core STATIC
    src/core/frame_meta.cpp
    src/core/contract_packet.cpp
    src/utils/config.cpp
    src/utils/logging.cpp
)
target_include_directories(mozart_core PUBLIC
    ${CMAKE_CURRENT_SOURCE_DIR}/include
    ${CMAKE_CURRENT_SOURCE_DIR}/third_party
)
target_link_libraries(mozart_core PUBLIC spdlog::spdlog nlohmann_json::nlohmann_json)

# ── Source module ─────────────────────────────────────────────────────────────
add_library(mozart_source STATIC
    src/source/mock_source.cpp
    src/source/ring_source.cpp
)
if(MOZART_ENABLE_UDP)
    target_sources(mozart_source PRIVATE src/source/udp_source.cpp)
    target_compile_definitions(mozart_source PUBLIC MOZART_ENABLE_UDP=1)
endif()
target_link_libraries(mozart_source PUBLIC mozart_core ${CMAKE_DL_LIBS})

# ── RVC inference module ──────────────────────────────────────────────────────
add_library(mozart_rvc STATIC
    src/rvc/model_loader.cpp
    src/rvc/rvc_pipeline.cpp
    src/rvc/hubert_extractor.cpp
    src/rvc/rmvpe_f0.cpp
    src/rvc/generator.cpp
)
if(MOZART_ENABLE_ONNXRUNTIME)
    target_sources(mozart_rvc PRIVATE src/rvc/onnx_engine.cpp)
    target_compile_definitions(mozart_rvc PUBLIC MOZART_ENABLE_ONNXRUNTIME=1)
    find_path(ONNXRUNTIME_INCLUDE_DIR onnxruntime_cxx_api.h
        PATHS /usr/include /usr/local/include /opt/onnxruntime/include
    )
    find_library(ONNXRUNTIME_LIB onnxruntime
        PATHS /usr/lib /usr/local/lib /opt/onnxruntime/lib
    )
    if(ONNXRUNTIME_INCLUDE_DIR AND ONNXRUNTIME_LIB)
        target_include_directories(mozart_rvc PUBLIC ${ONNXRUNTIME_INCLUDE_DIR})
        target_link_libraries(mozart_rvc PUBLIC ${ONNXRUNTIME_LIB})
    else()
        message(WARNING "ONNX Runtime not found, ONNX engine disabled")
    endif()
endif()
target_link_libraries(mozart_rvc PUBLIC mozart_core)

# ── Output module ─────────────────────────────────────────────────────────────
add_library(mozart_output STATIC
    src/output/dummy_sink.cpp
)
if(MOZART_ENABLE_PIPEWIRE)
    target_sources(mozart_output PRIVATE src/output/pipewire_sink.cpp)
    target_compile_definitions(mozart_output PUBLIC MOZART_ENABLE_PIPEWIRE=1)
    find_package(PkgConfig)
    pkg_check_modules(PIPEWIRE libpipewire-0.3)
    if(PIPEWIRE_FOUND)
        target_include_directories(mozart_output PUBLIC ${PIPEWIRE_INCLUDE_DIRS})
        target_link_libraries(mozart_output PUBLIC ${PIPEWIRE_LIBRARIES})
    endif()
endif()
if(MOZART_ENABLE_UDP)
    target_sources(mozart_output PRIVATE src/output/udp_sink.cpp)
endif()
target_link_libraries(mozart_output PUBLIC mozart_core)

# ── HTTP server module ────────────────────────────────────────────────────────
if(MOZART_ENABLE_HTTP)
    add_library(mozart_server STATIC
        src/server/http_server.cpp
    )
    target_include_directories(mozart_server PUBLIC
        ${CMAKE_CURRENT_SOURCE_DIR}/third_party
    )
    target_link_libraries(mozart_server PUBLIC mozart_core)
    target_compile_definitions(mozart_server PUBLIC MOZART_ENABLE_HTTP=1)
endif()

# ── Main executable ───────────────────────────────────────────────────────────
add_executable(mozart_unified src/main.cpp)
target_link_libraries(mozart_unified PRIVATE
    mozart_core mozart_source mozart_rvc mozart_output
)
if(MOZART_ENABLE_HTTP)
    target_link_libraries(mozart_unified PRIVATE mozart_server)
endif()

# ── Tests ─────────────────────────────────────────────────────────────────────
if(MOZART_BUILD_TESTS)
    enable_testing()

    add_executable(test_frame_meta tests/test_frame_meta.cpp)
    target_link_libraries(test_frame_meta PRIVATE mozart_core)
    add_test(NAME test_frame_meta COMMAND test_frame_meta)

    add_executable(test_contract_packet tests/test_contract_packet.cpp)
    target_link_libraries(test_contract_packet PRIVATE mozart_core)
    add_test(NAME test_contract_packet COMMAND test_contract_packet)

    add_executable(test_mock_pipeline tests/test_mock_pipeline.cpp)
    target_link_libraries(test_mock_pipeline PRIVATE mozart_core mozart_source mozart_rvc mozart_output)
    add_test(NAME test_mock_pipeline COMMAND test_mock_pipeline)

    if(MOZART_ENABLE_UDP)
        add_executable(test_udp_loopback tests/test_udp_loopback.cpp)
        target_link_libraries(test_udp_loopback PRIVATE mozart_core mozart_source mozart_rvc mozart_output)
        add_test(NAME test_udp_loopback COMMAND test_udp_loopback)
    endif()
endif()

# ── Summary ───────────────────────────────────────────────────────────────────
message(STATUS "")
message(STATUS "=== Mozart Unified Configuration ===")
message(STATUS "  Build tests:       ${MOZART_BUILD_TESTS}")
message(STATUS "  PipeWire output:   ${MOZART_ENABLE_PIPEWIRE}")
message(STATUS "  ONNX Runtime:      ${MOZART_ENABLE_ONNXRUNTIME}")
message(STATUS "  UDP transport:     ${MOZART_ENABLE_UDP}")
message(STATUS "  HTTP API:          ${MOZART_ENABLE_HTTP}")
message(STATUS "=====================================")
message(STATUS "")
