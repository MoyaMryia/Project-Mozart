cmake_minimum_required(VERSION 3.16)
project(mozart_pre VERSION 0.1.0 LANGUAGES C)

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
if(MOZART_USE_RNNOISE)
    set(RNNOISE_SRC_DIR ${CMAKE_SOURCE_DIR}/native/rnnoise/src)

    add_library(rnnoise STATIC
        ${RNNOISE_SRC_DIR}/denoise.c
        ${RNNOISE_SRC_DIR}/rnn.c
        ${RNNOISE_SRC_DIR}/nnet.c
        ${RNNOISE_SRC_DIR}/nnet_default.c
        ${RNNOISE_SRC_DIR}/kiss_fft.c
        ${RNNOISE_SRC_DIR}/pitch.c
        ${RNNOISE_SRC_DIR}/celt_lpc.c
        ${RNNOISE_SRC_DIR}/rnnoise_data.c
        ${RNNOISE_SRC_DIR}/rnnoise_tables.c
        ${RNNOISE_SRC_DIR}/parse_lpcnet_weights.c
    )
    target_include_directories(rnnoise PRIVATE
        ${RNNOISE_SRC_DIR}
        ${CMAKE_SOURCE_DIR}/native/rnnoise/include
    )
    target_compile_definitions(rnnoise PRIVATE
        HAVE_CONFIG_H=1
        TRAINING=0
        RNNOISE_BUILD
    )
    # RNNoise (libopus heritage) does not support -ffast-math
    target_compile_options(rnnoise PRIVATE -fno-fast-math)
    # Relax pedantic warnings for third-party code
    target_compile_options(rnnoise PRIVATE -Wno-pedantic -Wno-sign-compare
                           -Wno-unused-parameter -Wno-unused-function)
    target_link_libraries(rnnoise PRIVATE m)
endif()

# ---- Core library ------------------------------------------------------------
add_library(mozart_pre_core STATIC
    src/stage.c
    src/pipeline.c
    src/rnnoise.c
    src/pre.c
)
target_include_directories(mozart_pre_core PUBLIC
    include
    .
    ${CMAKE_CURRENT_SOURCE_DIR}/../IO/include
)
target_link_libraries(mozart_pre_core PUBLIC m)

if(MOZART_USE_RNNOISE)
    target_link_libraries(mozart_pre_core PUBLIC rnnoise)
    target_include_directories(mozart_pre_core PUBLIC
        ${CMAKE_SOURCE_DIR}/native/rnnoise/include
    )
    target_compile_definitions(mozart_pre_core PUBLIC MOZART_USE_RNNOISE=1)
endif()

# ---- Example executable ------------------------------------------------------
add_executable(mozart_pre_example src/main.c)
target_link_libraries(mozart_pre_example PRIVATE mozart_pre_core)

# ---- Tests -------------------------------------------------------------------
if(MOZART_BUILD_TESTS)
    enable_testing()
    add_executable(stage_test tests/test_stage.c)
    target_link_libraries(stage_test PRIVATE mozart_pre_core)
    add_test(NAME stage_test COMMAND stage_test)
endif()
