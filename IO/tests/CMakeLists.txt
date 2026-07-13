# IO 模块测试
add_executable(test_ring_buffer test_ring_buffer.cpp)
target_link_libraries(test_ring_buffer PRIVATE mozart_io spdlog::spdlog)
target_compile_options(test_ring_buffer PRIVATE -Wall -Wextra)

add_executable(test_mock_stream test_mock_stream.cpp)
target_link_libraries(test_mock_stream PRIVATE mozart_io spdlog::spdlog)
target_compile_options(test_mock_stream PRIVATE -Wall -Wextra)

add_test(NAME io_ring_buffer  COMMAND test_ring_buffer)
add_test(NAME io_mock_stream  COMMAND test_mock_stream)
