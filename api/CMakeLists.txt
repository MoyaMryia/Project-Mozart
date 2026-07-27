if(NOT TARGET mozart_monitor)
    add_subdirectory(
        ${CMAKE_CURRENT_SOURCE_DIR}/../monitor
        ${CMAKE_CURRENT_BINARY_DIR}/mozart_monitor
    )
endif()

add_library(mozart_api STATIC src/http_api.cpp)
target_include_directories(mozart_api PUBLIC
    ${CMAKE_CURRENT_SOURCE_DIR}/include
    ${CMAKE_CURRENT_SOURCE_DIR}/../rvc-backend/include
)
target_link_libraries(mozart_api PUBLIC
    nlohmann_json::nlohmann_json
    spdlog::spdlog
    Threads::Threads
    mozart_monitor
)

if(CMAKE_CXX_COMPILER_ID MATCHES "GNU|Clang")
    target_compile_options(mozart_api PRIVATE -Wall -Wextra -Wpedantic -O2)
endif()
