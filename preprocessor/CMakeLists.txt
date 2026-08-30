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
            ${CMAKE_SOURCE_DIR}/native/rnnoise/include
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
add_library(mozart_pre_core STATIC
    src/rnnoise.c
    src/dsp.c
    src/capture.c
    src/wav.c
)
target_include_directories(mozart_pre_core PUBLIC
    include
    .
    ${CMAKE_CURRENT_SOURCE_DIR}/../IO/include
)
target_link_libraries(mozart_pre_core PUBLIC m asound)

if(MOZART_USE_RNNOISE)
    set(MOZART_RNNOISE_DEFAULT_MODEL "${CMAKE_SOURCE_DIR}/assets/rnnoise_default.rnnb"
        CACHE PATH "RNNoise 权重 blob 默认路径")
    if(NOT EXISTS "${MOZART_RNNOISE_DEFAULT_MODEL}")
        message(WARNING "RNNoise blob 不存在: ${MOZART_RNNOISE_DEFAULT_MODEL}\n"
                        "先生成: cmake --build build --target export_rnnoise_blob")
    endif()
    target_compile_definitions(mozart_pre_core PUBLIC
        MOZART_RNNOISE_DEFAULT_MODEL="${MOZART_RNNOISE_DEFAULT_MODEL}")
endif()

if(MOZART_USE_RNNOISE)
    target_link_libraries(mozart_pre_core PUBLIC rnnoise)
    target_include_directories(mozart_pre_core PUBLIC
        ${CMAKE_CURRENT_SOURCE_DIR}/native/rnnoise/include
    )
    target_compile_definitions(mozart_pre_core PUBLIC MOZART_USE_RNNOISE=1)
endif()

# ---- Daemon ------------------------------------------------------------------
add_executable(mozart-pre src/main.c)
target_link_libraries(mozart-pre PRIVATE mozart_pre_core)

# ---- Tests -------------------------------------------------------------------
if(MOZART_BUILD_TESTS)
    enable_testing()
    add_executable(test_dsp tests/test_dsp.c)
    target_link_libraries(test_dsp PRIVATE mozart_pre_core)
    add_test(NAME test_dsp COMMAND test_dsp)
endif()
