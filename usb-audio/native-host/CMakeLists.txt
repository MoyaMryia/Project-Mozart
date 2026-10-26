cmake_minimum_required(VERSION 3.18)
if(CMAKE_SOURCE_DIR STREQUAL CMAKE_CURRENT_SOURCE_DIR)
  project(mozart_usb_host LANGUAGES C CXX)
  include(CTest)
endif()
find_package(Threads REQUIRED)
find_path(ALSA_INCLUDE_DIR alsa/asoundlib.h REQUIRED)
find_library(ALSA_LIBRARY NAMES asound REQUIRED)
find_library(LIBUSB_LIBRARY NAMES usb-1.0 libusb-1.0.so.0 REQUIRED)

add_library(mozart-usb-host-core INTERFACE)
target_compile_features(mozart-usb-host-core INTERFACE cxx_std_17)
target_include_directories(mozart-usb-host-core INTERFACE
  "${CMAKE_CURRENT_SOURCE_DIR}"
  "${CMAKE_CURRENT_SOURCE_DIR}/../../IO/include")
target_link_libraries(mozart-usb-host-core INTERFACE Threads::Threads)

add_executable(mozart-usb-host host.cpp)
target_include_directories(mozart-usb-host SYSTEM PRIVATE
  "${ALSA_INCLUDE_DIR}" "${CMAKE_CURRENT_SOURCE_DIR}/third_party")
target_link_libraries(mozart-usb-host PRIVATE mozart-usb-host-core "${ALSA_LIBRARY}" "${LIBUSB_LIBRARY}")
target_compile_options(mozart-usb-host PRIVATE -Wall -Wextra -Wpedantic)

if(BUILD_TESTING)
  enable_testing()
  add_executable(mozart-usb-host-tests tests.cpp)
  target_link_libraries(mozart-usb-host-tests PRIVATE mozart-usb-host-core)
  target_compile_definitions(mozart-usb-host-tests PRIVATE HOST_TEST_DIR="${CMAKE_CURRENT_BINARY_DIR}/test-artifacts")
  target_compile_options(mozart-usb-host-tests PRIVATE -Wall -Wextra -Wpedantic)
  add_test(NAME mozart-usb-host-tests COMMAND mozart-usb-host-tests)
  find_package(Python3 COMPONENTS Interpreter REQUIRED)
  add_test(NAME mozart-usb-host-schema COMMAND "${Python3_EXECUTABLE}"
    "${CMAKE_CURRENT_SOURCE_DIR}/schema_tests.py"
    "$<TARGET_FILE:mozart-usb-host-tests>" "$<TARGET_FILE:mozart-usb-host>")
endif()
