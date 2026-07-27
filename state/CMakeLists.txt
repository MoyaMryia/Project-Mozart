add_executable(mozart_stated src/main.cpp src/daemon.cpp)
target_include_directories(mozart_stated PRIVATE ${CMAKE_CURRENT_SOURCE_DIR}/include)
target_link_libraries(mozart_stated PRIVATE mozart_rvc_runtime mozart_api)

if(CMAKE_CXX_COMPILER_ID MATCHES "GNU|Clang")
    target_compile_options(mozart_stated PRIVATE -Wall -Wextra -Wpedantic -O2)
endif()
