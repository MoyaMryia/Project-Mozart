cmake_minimum_required(VERSION 3.16)
project(mozart_post VERSION 0.1.0 LANGUAGES CXX)

set(CMAKE_CXX_STANDARD 17)
set(CMAKE_CXX_STANDARD_REQUIRED ON)
set(CMAKE_CXX_EXTENSIONS OFF)

if(NOT CMAKE_BUILD_TYPE)
    set(CMAKE_BUILD_TYPE Release)
endif()

add_compile_options(-Wall -Wextra -Wpedantic)

# ----------------------------------------------------------------------------
# Options
# ----------------------------------------------------------------------------
option(MOZART_BUILD_TESTS "Build unit tests" ON)
option(MOZART_ENABLE_PIPEWIRE "Enable PipeWire output sink" OFF)
option(MOZART_ENABLE_ONNXRUNTIME "Enable ONNX Runtime inference" OFF)
option(MOZART_ENABLE_HTTP "Enable cpp-httplib model server" ON)

# ----------------------------------------------------------------------------
# Third-party (header-only vendored under third_party/)
# ----------------------------------------------------------------------------
add_library(tomlplusplus INTERFACE)
target_include_directories(tomlplusplus INTERFACE
    ${CMAKE_SOURCE_DIR}/third_party/tomlplusplus/include)

if(MOZART_ENABLE_HTTP)
    add_library(cpp_httplib INTERFACE)
    target_include_directories(cpp_httplib INTERFACE
        ${CMAKE_SOURCE_DIR}/third_party/cpp-httplib/include)
    # cpp-httplib needs a threading lib for its server
    find_package(Threads REQUIRED)
    target_link_libraries(cpp_httplib INTERFACE Threads::Threads)
endif()

if(MOZART_ENABLE_ONNXRUNTIME)
    # Expect onnxruntime provided by system (Jetson JetPack / NVIDIA Jetson Zoo).
    # Fallback to manual path via -DONNXRUNTIME_ROOT=/path/to/onnxruntime
    if(NOT DEFINED ONNXRUNTIME_ROOT)
        set(ONNXRUNTIME_ROOT "/usr")
    endif()
    find_library(ONNXRUNTIME_LIB onnxruntime
        PATHS ${ONNXRUNTIME_ROOT}/lib
        REQUIRED)
    add_library(onnxruntime SHARED IMPORTED)
    set_target_properties(onnxruntime PROPERTIES
        IMPORTED_LOCATION ${ONNXRUNTIME_LIB}
        INTERFACE_INCLUDE_DIRECTORIES ${ONNXRUNTIME_ROOT}/include)
endif()

if(MOZART_ENABLE_PIPEWIRE)
    find_package(PkgConfig REQUIRED)
    pkg_check_modules(PIPEWIRE REQUIRED libpipewire-0.3)
endif()

# ----------------------------------------------------------------------------
# Core library
# ----------------------------------------------------------------------------
add_library(mozart_post_core STATIC
    src/contract/mock_source.cpp
    src/contract/mozart_ring_source.cpp
    src/output/dummy_sink.cpp
    src/rvc/rvc_pipeline.cpp
    src/rvc/model_loader.cpp
    src/utils/config.cpp
    src/utils/logging.cpp
)
target_include_directories(mozart_post_core PUBLIC include)
target_link_libraries(mozart_post_core PUBLIC tomlplusplus)
# Mozart ring source uses dlopen to avoid hard linkage.
find_package(Threads REQUIRED)
target_link_libraries(mozart_post_core PUBLIC ${CMAKE_DL_LIBS} Threads::Threads)

if(MOZART_ENABLE_ONNXRUNTIME)
    target_sources(mozart_post_core PRIVATE
        src/inference/onnx_engine.cpp
        src/rvc/hubert_extractor.cpp
        src/rvc/rmvpe_f0.cpp
        src/rvc/generator.cpp)
    target_link_libraries(mozart_post_core PUBLIC onnxruntime)
    target_compile_definitions(mozart_post_core PUBLIC MOZART_ENABLE_ONNXRUNTIME=1)
endif()

if(MOZART_ENABLE_PIPEWIRE)
    target_sources(mozart_post_core PRIVATE src/output/pipewire_sink.cpp)
    target_link_libraries(mozart_post_core PUBLIC ${PIPEWIRE_LIBRARIES})
    target_include_directories(mozart_post_core PUBLIC ${PIPEWIRE_INCLUDE_DIRS})
    target_compile_definitions(mozart_post_core PUBLIC MOZART_ENABLE_PIPEWIRE=1)
endif()

# ----------------------------------------------------------------------------
# Main executable
# ----------------------------------------------------------------------------
add_executable(mozart_post src/main.cpp)
target_link_libraries(mozart_post PRIVATE mozart_post_core)

if(MOZART_ENABLE_HTTP)
    target_sources(mozart_post PRIVATE src/server/http_server.cpp)
    target_link_libraries(mozart_post PRIVATE cpp_httplib)
    target_compile_definitions(mozart_post PRIVATE MOZART_ENABLE_HTTP=1)
endif()

# ----------------------------------------------------------------------------
# Tests
# ----------------------------------------------------------------------------
if(MOZART_BUILD_TESTS)
    enable_testing()
    add_executable(frame_meta_test tests/frame_meta_test.cpp)
    target_link_libraries(frame_meta_test PRIVATE mozart_post_core)
    add_test(NAME frame_meta_test COMMAND frame_meta_test)

    add_executable(mock_pipeline_test tests/mock_pipeline_test.cpp)
    target_link_libraries(mock_pipeline_test PRIVATE mozart_post_core)
    add_test(NAME mock_pipeline_test COMMAND mock_pipeline_test)

    if(MOZART_ENABLE_ONNXRUNTIME)
        add_executable(onnx_smoke_test tests/onnx_smoke_test.cpp)
        target_link_libraries(onnx_smoke_test PRIVATE mozart_post_core)
        add_test(NAME onnx_smoke_test COMMAND onnx_smoke_test)
    endif()
endif()