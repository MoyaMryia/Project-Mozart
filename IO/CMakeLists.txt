cmake_minimum_required(VERSION 3.18)
project(mozart_io VERSION 0.1.0 LANGUAGES CXX)

set(CMAKE_CXX_STANDARD 17)
set(CMAKE_CXX_STANDARD_REQUIRED ON)
set(CMAKE_CXX_EXTENSIONS OFF)

if(NOT CMAKE_BUILD_TYPE)
    set(CMAKE_BUILD_TYPE Release)
endif()

# ---- Options -----------------------------------------------------------------
option(MOZART_IO_BUILD_TESTS    "Build unit tests"          ON)
option(MOZART_IO_ENABLE_PIPEWIRE "Enable PipeWire driver"   OFF)
option(MOZART_IO_ENABLE_UDP     "Enable UDP network driver" ON)
option(MOZART_IO_BUILD_SHARED   "Build shared lib (else static)" ON)

# ---- Dependencies ------------------------------------------------------------
find_package(Threads REQUIRED)

# spdlog (logging, optional: fall back to FetchContent like rvc-backend)
if(NOT TARGET spdlog::spdlog)
    find_package(spdlog QUIET)
endif()
if(NOT TARGET spdlog::spdlog)
    include(FetchContent)
    FetchContent_Declare(
        spdlog
        GIT_REPOSITORY https://github.com/gabime/spdlog.git
        GIT_TAG v1.12.0
    )
    FetchContent_MakeAvailable(spdlog)
endif()
if(TARGET spdlog)
    set_target_properties(spdlog PROPERTIES POSITION_INDEPENDENT_CODE ON)
endif()

# ---- Sources -----------------------------------------------------------------
# pipewire_stream.cpp 默认编译为 stub（不依赖 libpipewire）；
# MOZART_IO_ENABLE_PIPEWIRE=ON 时链接 libpipewire，后续在源码内用该宏切换真实实现。
set(IO_SOURCES
    src/audio_stream.cpp
    src/ring_buffer.cpp
    src/mock_stream.cpp
    src/pipewire_stream.cpp
)

set(IO_HEADERS
    include/mozart/frame_meta.h
    include/mozart/audio_io.h
    include/mozart/audio_stream.hpp
    include/mozart/ring_buffer.hpp
    include/mozart/mock_stream.hpp
    include/mozart/pipewire_stream.hpp
)

if(MOZART_IO_ENABLE_UDP)
    list(APPEND IO_SOURCES src/udp_stream.cpp)
    list(APPEND IO_HEADERS include/mozart/udp_stream.hpp)
endif()

if(MOZART_IO_ENABLE_PIPEWIRE)
    find_package(PkgConfig REQUIRED)
    pkg_check_modules(PIPEWIRE REQUIRED libpipewire-0.3)
endif()

# ---- Library -----------------------------------------------------------------
if(MOZART_IO_BUILD_SHARED)
    add_library(mozart_io SHARED ${IO_SOURCES} ${IO_HEADERS})
    set_target_properties(mozart_io PROPERTIES
        POSITION_INDEPENDENT_CODE ON
    )
else()
    add_library(mozart_io STATIC ${IO_SOURCES} ${IO_HEADERS})
endif()

target_include_directories(mozart_io PUBLIC
    ${CMAKE_CURRENT_SOURCE_DIR}/include
)

target_link_libraries(mozart_io PUBLIC
    Threads::Threads
    spdlog::spdlog
)

if(MOZART_IO_ENABLE_UDP)
    target_compile_definitions(mozart_io PUBLIC MOZART_IO_ENABLE_UDP=1)
endif()

if(MOZART_IO_ENABLE_PIPEWIRE)
    target_link_libraries(mozart_io PUBLIC ${PIPEWIRE_LIBRARIES})
    target_include_directories(mozart_io PUBLIC ${PIPEWIRE_INCLUDE_DIRS})
    target_compile_definitions(mozart_io PUBLIC MOZART_IO_ENABLE_PIPEWIRE=1)
endif()

# Hide C-ABI symbols only when shared; keep MOZART_API export macro
if(MOZART_IO_BUILD_SHARED)
    target_compile_definitions(mozart_io PRIVATE MOZART_IO_EXPORTS=1)
endif()

# ---- Compiler flags ----------------------------------------------------------
if(CMAKE_CXX_COMPILER_ID MATCHES "GNU|Clang")
    target_compile_options(mozart_io PRIVATE -Wall -Wextra -Wpedantic -O2)
    # ARM NEON + DOTPROD + FP16 for Jetson Orin (Cortex-A78AE)
    if(CMAKE_SYSTEM_PROCESSOR MATCHES "aarch64")
        target_compile_options(mozart_io PRIVATE
            -march=armv8.2-a+fp16+dotprod -mtune=cortex-a78ae)
    endif()
elseif(MSVC)
    target_compile_options(mozart_io PRIVATE /W4 /O2)
endif()

# ---- Tests -------------------------------------------------------------------
if(MOZART_IO_BUILD_TESTS)
    enable_testing()
    add_subdirectory(tests)
endif()
