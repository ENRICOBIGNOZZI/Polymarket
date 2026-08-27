cmake_minimum_required(VERSION 3.20)
project(polymarket_v7 VERSION 7.0.0 LANGUAGES CXX)
set(CMAKE_CXX_STANDARD 20)
set(CMAKE_CXX_STANDARD_REQUIRED ON)
set(CMAKE_CXX_EXTENSIONS OFF)

if(POLICY CMP0167)
  cmake_policy(SET CMP0167 NEW)
endif()

find_package(PkgConfig REQUIRED)
find_package(Boost REQUIRED)
find_package(Threads REQUIRED)
find_package(OpenSSL REQUIRED)
pkg_check_modules(LIBCURL REQUIRED IMPORTED_TARGET libcurl)

# Boost.JSON is compiled exactly once and shared by all V7 C++ components.
# This keeps the bounded market-data decoder independent from HTTP/REST code.
add_library(pm_json src/boost_json.cpp)
target_include_directories(pm_json PUBLIC ${Boost_INCLUDE_DIRS})
target_compile_options(pm_json PRIVATE -Wall -Wextra -Wpedantic)

add_library(pm_core src/http.cpp src/api.cpp src/event_api.cpp src/config.cpp)
target_include_directories(pm_core PUBLIC ${CMAKE_CURRENT_SOURCE_DIR}/include ${Boost_INCLUDE_DIRS})
target_link_libraries(pm_core PUBLIC PkgConfig::LIBCURL pm_json)
target_compile_options(pm_core PRIVATE -Wall -Wextra -Wpedantic)

add_library(pm_fast_arb src/fast_arb.cpp src/fast_ws.cpp)
target_include_directories(pm_fast_arb PUBLIC ${CMAKE_CURRENT_SOURCE_DIR}/include)
target_link_libraries(pm_fast_arb PUBLIC pm_core Threads::Threads OpenSSL::SSL OpenSSL::Crypto)
target_compile_options(pm_fast_arb PRIVATE -Wall -Wextra -Wpedantic)

add_library(pm_v7_common
  src/v7_oms.cpp
  src/v7_paper_queue.cpp
  src/v7_market_state.cpp
  src/v7_market_ws.cpp)
target_include_directories(pm_v7_common PUBLIC ${CMAKE_CURRENT_SOURCE_DIR}/include ${Boost_INCLUDE_DIRS})
target_link_libraries(pm_v7_common PUBLIC pm_json)
target_compile_options(pm_v7_common PRIVATE -Wall -Wextra -Wpedantic)

add_library(pm_v7_hft
  src/v7_maker_hft.cpp
  src/v7_maker_lane.cpp
  src/v7_maker_paper.cpp)
target_include_directories(pm_v7_hft PUBLIC ${CMAKE_CURRENT_SOURCE_DIR}/include)
target_link_libraries(pm_v7_hft PUBLIC pm_v7_common Threads::Threads)
target_compile_options(pm_v7_hft PRIVATE -Wall -Wextra -Wpedantic)

add_executable(polymarket_v7_trade_recorder src/v7_trade_recorder.cpp)
target_link_libraries(polymarket_v7_trade_recorder PRIVATE pm_core)
target_compile_options(polymarket_v7_trade_recorder PRIVATE -Wall -Wextra -Wpedantic)

add_executable(polymarket_v7_maker_replay_bench src/v7_maker_replay_bench.cpp)
target_link_libraries(polymarket_v7_maker_replay_bench PRIVATE pm_v7_hft)
target_compile_options(polymarket_v7_maker_replay_bench PRIVATE -Wall -Wextra -Wpedantic)

include(CTest)
if(BUILD_TESTING)
  find_package(Python3 REQUIRED COMPONENTS Interpreter)
  add_executable(pm_fast_arb_tests tests/test_fast_arb.cpp)
  target_link_libraries(pm_fast_arb_tests PRIVATE pm_fast_arb)
  add_test(NAME pm_fast_arb_tests COMMAND pm_fast_arb_tests)

  add_executable(pm_v7_oms_tests tests/test_v7_oms.cpp)
  target_link_libraries(pm_v7_oms_tests PRIVATE pm_v7_common)
  add_test(NAME pm_v7_oms_tests COMMAND pm_v7_oms_tests)

  add_executable(pm_v7_paper_queue_tests tests/test_v7_paper_queue.cpp)
  target_link_libraries(pm_v7_paper_queue_tests PRIVATE pm_v7_common)
  add_test(NAME pm_v7_paper_queue_tests COMMAND pm_v7_paper_queue_tests)

  add_executable(pm_v7_market_state_tests tests/test_v7_market_state.cpp)
  target_link_libraries(pm_v7_market_state_tests PRIVATE pm_v7_common)
  add_test(NAME pm_v7_market_state_tests COMMAND pm_v7_market_state_tests)

  add_executable(pm_v7_market_ws_tests tests/test_v7_market_ws.cpp)
  target_link_libraries(pm_v7_market_ws_tests PRIVATE pm_v7_common)
  add_test(NAME pm_v7_market_ws_tests COMMAND pm_v7_market_ws_tests)

  add_executable(pm_v7_maker_hft_tests tests/test_v7_maker_hft.cpp)
  target_link_libraries(pm_v7_maker_hft_tests PRIVATE pm_v7_hft)
  add_test(NAME pm_v7_maker_hft_tests COMMAND pm_v7_maker_hft_tests)

  add_executable(pm_v7_maker_lane_tests tests/test_v7_maker_lane.cpp)
  target_link_libraries(pm_v7_maker_lane_tests PRIVATE pm_v7_hft)
  add_test(NAME pm_v7_maker_lane_tests COMMAND pm_v7_maker_lane_tests)

  add_executable(pm_v7_maker_paper_tests tests/test_v7_maker_paper.cpp)
  target_link_libraries(pm_v7_maker_paper_tests PRIVATE pm_v7_hft)
  add_test(NAME pm_v7_maker_paper_tests COMMAND pm_v7_maker_paper_tests)

  add_test(NAME no_legacy_runtime COMMAND ${Python3_EXECUTABLE} ${CMAKE_CURRENT_SOURCE_DIR}/tests/test_no_legacy_runtime.py)

  file(GLOB V7_PY_TESTS CONFIGURE_DEPENDS
    "${CMAKE_CURRENT_SOURCE_DIR}/tests/test_v7_*.py"
    "${CMAKE_CURRENT_SOURCE_DIR}/tests/test_monitoring_v7_*.py")
  foreach(test_file IN LISTS V7_PY_TESTS)
    get_filename_component(test_name "${test_file}" NAME_WE)
    add_test(NAME "${test_name}" COMMAND ${Python3_EXECUTABLE} "${test_file}")
  endforeach()
endif()
