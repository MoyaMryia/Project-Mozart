cmake_minimum_required(VERSION 3.18)
project(ProjectMozart VERSION 0.1.0 LANGUAGES C CXX)

find_package(Threads REQUIRED)
find_package(nlohmann_json 3.2.0 REQUIRED)
find_package(spdlog REQUIRED)

option(MOZART_BUILD_TESTS "Build Project Mozart tests" ON)
option(USE_ONNX "Enable ONNX Runtime for RVC inference" ON)
option(USE_LIBTORCH "Enable libtorch for real RVC inference" OFF)

set(MOZART_IO_BUILD_TESTS OFF CACHE BOOL "Build IO tests" FORCE)
set(MOZART_BUILD_TESTS OFF CACHE BOOL "Build preprocessor tests" FORCE)

add_subdirectory(IO)
add_subdirectory(preprocessor)
add_subdirectory(monitor)
add_subdirectory(api)
add_subdirectory(rvc-backend)
add_subdirectory(state)
