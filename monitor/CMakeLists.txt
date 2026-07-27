add_library(mozart_monitor STATIC src/monitor.cpp)
target_include_directories(mozart_monitor PUBLIC ${CMAKE_CURRENT_SOURCE_DIR}/include)
target_link_libraries(mozart_monitor PUBLIC nlohmann_json::nlohmann_json)

if(CMAKE_CXX_COMPILER_ID MATCHES "GNU|Clang")
    target_compile_options(mozart_monitor PRIVATE -Wall -Wextra -Wpedantic -O2)
endif()
