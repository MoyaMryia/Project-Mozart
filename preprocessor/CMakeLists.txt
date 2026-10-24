cmake_minimum_required(VERSION 3.16)
project(mozart_pre VERSION 0.2.0 LANGUAGES C)

set(CMAKE_C_STANDARD 11)
set(CMAKE_C_STANDARD_REQUIRED ON)
set(CMAKE_C_EXTENSIONS OFF)

if(NOT CMAKE_BUILD_TYPE)
    set(CMAKE_BUILD_TYPE Release)
endif()

# ARM NEON + DOTPROD + FP16 optimisation for Jetson Orin (Cortex-A78AE)
if(CMAKE_SYSTEM_PROCESSOR MATCHES "aarch64")
    add_compile_options(-march=armv8.2-a+fp16+dotprod -mtune=cortex-a78ae
                        -O3 -ffast-math -funroll-loops -flto)
endif()

add_compile_options(-Wall -Wextra -Wpedantic)

# ---- Options -----------------------------------------------------------------
option(MOZART_BUILD_TESTS "Build unit tests" OFF)
option(MOZART_USE_RNNOISE "Build and link xiph RNNoise from native/rnnoise/" ON)

# 有开发头文件时构建轻量变速工具；其他部署仍可使用 FFmpeg 程序。
find_package(PkgConfig QUIET)
if(PkgConfig_FOUND)
    pkg_check_modules(MOZART_AVFILTER QUIET IMPORTED_TARGET libavfilter>=9 libavutil>=58)
    if(TARGET PkgConfig::MOZART_AVFILTER)
        add_executable(mozart-tempo src/tempo_filter.c)
        target_link_libraries(mozart-tempo PRIVATE PkgConfig::MOZART_AVFILTER)
        add_library(mozart_tempo_filter SHARED src/tempo_filter.c)
        set_target_properties(mozart_tempo_filter PROPERTIES OUTPUT_NAME mozart-tempo)
        target_compile_definitions(mozart_tempo_filter PRIVATE MOZART_TEMPO_LIBRARY=1)
        target_link_libraries(mozart_tempo_filter PRIVATE PkgConfig::MOZART_AVFILTER)
    endif()
endif()

# ---- RNNoise native library --------------------------------------------------
# 外部权重模式：模型权重不入代码，运行时从 blob 文件加载（见 rnnoise.c）。
# 上游 rnnoise_data.c（78MB 内嵌权重源）已从树中移除；如需重新导出 blob，
# 从上游恢复 rnnoise_data.c 后：cmake --build build --target export_rnnoise_blob
if(MOZART_USE_RNNOISE)
    set(RNNOISE_SRC_DIR ${CMAKE_CURRENT_SOURCE_DIR}/native/rnnoise/src)

    add_library(rnnoise STATIC
        ${RNNOISE_SRC_DIR}/denoise.c
        ${RNNOISE_SRC_DIR}/rnn.c
        ${RNNOISE_SRC_DIR}/nnet.c
        ${RNNOISE_SRC_DIR}/nnet_default.c
        ${RNNOISE_SRC_DIR}/kiss_fft.c
        ${RNNOISE_SRC_DIR}/pitch.c
        ${RNNOISE_SRC_DIR}/celt_lpc.c
        # USE_WEIGHTS_FILE 下权重走 blob 文件；init_rnnoise 已抽到 rnnoise_init.c
        ${RNNOISE_SRC_DIR}/rnnoise_init.c
        ${RNNOISE_SRC_DIR}/rnnoise_tables.c
        ${RNNOISE_SRC_DIR}/parse_lpcnet_weights.c
    )
    target_include_directories(rnnoise PRIVATE
        ${RNNOISE_SRC_DIR}
        ${CMAKE_CURRENT_SOURCE_DIR}/native/rnnoise/include
    )
    target_compile_definitions(rnnoise PRIVATE
        HAVE_CONFIG_H=1
        TRAINING=0
        RNNOISE_BUILD
        USE_WEIGHTS_FILE=1
    )
    # 一次性导出工具（默认不构建）：链接内嵌权重数据源
    if(EXISTS ${RNNOISE_SRC_DIR}/rnnoise_data.c)
        add_executable(export_rnnoise_blob EXCLUDE_FROM_ALL
            tools/export_rnnoise_blob.c
            ${RNNOISE_SRC_DIR}/rnnoise_data.c
        )
        target_include_directories(export_rnnoise_blob PRIVATE
            ${RNNOISE_SRC_DIR}
            ${CMAKE_CURRENT_SOURCE_DIR}/native/rnnoise/include
        )
        target_compile_definitions(export_rnnoise_blob PRIVATE
            HAVE_CONFIG_H=1 TRAINING=0 RNNOISE_BUILD
        )
    endif()
    # RNNoise (libopus heritage) does not support -ffast-math
    target_compile_options(rnnoise PRIVATE -fno-fast-math)
    # Relax pedantic warnings for third-party code
    target_compile_options(rnnoise PRIVATE -Wno-pedantic -Wno-sign-compare
                           -Wno-unused-parameter -Wno-unused-function)
    target_link_libraries(rnnoise PRIVATE m)
endif()

# ---- Core library ------------------------------------------------------------
find_package(Threads REQUIRED)
add_library(mozart_pre_core STATIC
    src/dsp.c
    src/noise_suppressor.c
    src/stages.c
    src/control.c
    src/usb_link.c
    src/capture.c
    src/playback.c
    src/wav.c
)
target_include_directories(mozart_pre_core PUBLIC
    include
    .
    ${CMAKE_CURRENT_SOURCE_DIR}/../IO/include
)
# WOLA 数值路径不参与 fast-math：对拍门限 1e-6 依赖确定浮点行为。
set_source_files_properties(src/noise_suppressor.c PROPERTIES
    COMPILE_OPTIONS "-fno-fast-math")
target_link_libraries(mozart_pre_core PUBLIC m asound Threads::Threads)

if(MOZART_USE_RNNOISE)
    # RNNoise 已退出处理链（M1 单干重构）；保留静态库与导出工具供回退参考。
    set(MOZART_RNNOISE_DEFAULT_MODEL "${CMAKE_CURRENT_SOURCE_DIR}/assets/rnnoise_default.rnnb"
        CACHE PATH "RNNoise 权重 blob 默认路径")
endif()

# ---- Daemon ------------------------------------------------------------------
find_package(Threads REQUIRED)
add_executable(mozart-pre src/main.c)
target_link_libraries(mozart-pre PRIVATE mozart_pre_core Threads::Threads)

# ---- Tests -------------------------------------------------------------------
if(MOZART_BUILD_TESTS)
    enable_testing()
    add_executable(test_dsp tests/test_dsp.c)
    target_link_libraries(test_dsp PRIVATE mozart_pre_core)
    add_test(NAME test_dsp COMMAND test_dsp)
    find_package(Python3 REQUIRED COMPONENTS Interpreter)
    add_test(NAME test_replay COMMAND ${Python3_EXECUTABLE}
        ${CMAKE_CURRENT_SOURCE_DIR}/tests/test_replay.py $<TARGET_FILE:mozart-pre>)
    set_tests_properties(test_replay PROPERTIES TIMEOUT 20)

    # 噪声抑制器对拍夹具：构建期由 Python 参考实现生成
    set(NS_FIXTURE_DIR ${CMAKE_CURRENT_BINARY_DIR}/ns-fixture)
    add_custom_command(
        OUTPUT ${NS_FIXTURE_DIR}/expected.f32
        COMMAND ${Python3_EXECUTABLE}
                ${CMAKE_CURRENT_SOURCE_DIR}/../tools/gen_ns_fixture.py
                ${NS_FIXTURE_DIR}
        DEPENDS ${CMAKE_CURRENT_SOURCE_DIR}/../tools/gen_ns_fixture.py
                ${CMAKE_CURRENT_SOURCE_DIR}/../tools/wola_reference.py
        COMMENT "生成噪声抑制器对拍夹具"
    )
    add_custom_target(ns-fixture DEPENDS ${NS_FIXTURE_DIR}/expected.f32)

    add_executable(test_noise_suppressor tests/test_noise_suppressor.c)
    target_link_libraries(test_noise_suppressor PRIVATE mozart_pre_core m)
    add_test(NAME test_noise_suppressor
             COMMAND test_noise_suppressor ${NS_FIXTURE_DIR})
    add_dependencies(test_noise_suppressor ns-fixture)

    add_executable(test_stages tests/test_stages.c)
    target_link_libraries(test_stages PRIVATE mozart_pre_core m)
    add_test(NAME test_stages COMMAND test_stages)

    add_executable(test_usb_link tests/test_usb_link.c)
    target_include_directories(test_usb_link PRIVATE ${CMAKE_CURRENT_SOURCE_DIR}/include)
    target_link_libraries(test_usb_link PRIVATE m)
    add_test(NAME test_usb_link COMMAND test_usb_link)
endif()
