cmake_minimum_required(VERSION 3.18)
project(ProjectMozart VERSION 0.1.0 LANGUAGES C CXX)

find_package(Threads REQUIRED)
find_package(nlohmann_json 3.2.0 REQUIRED)
find_package(spdlog REQUIRED)

# 组件测试由各自独立构建树控制（如 preprocessor/build）；根构建是纯运行时
# 构建，强制关闭组件测试。
set(MOZART_IO_BUILD_TESTS OFF CACHE BOOL "Build IO tests" FORCE)
set(MOZART_BUILD_TESTS OFF CACHE BOOL "Build preprocessor tests" FORCE)

add_subdirectory(IO)
add_subdirectory(preprocessor)
add_subdirectory(monitor)
add_subdirectory(api)
add_subdirectory(rvc-backend)
add_subdirectory(state)
