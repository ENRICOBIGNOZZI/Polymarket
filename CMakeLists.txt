cmake_minimum_required(VERSION 3.20)
project(polymarket_quant_engine VERSION 1.0.0 LANGUAGES CXX)
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

add_library(pm_core
    src/http.cpp
    src/api.cpp
    src/event_api.cpp
    src/engine.cpp
    src/boost_json.cpp
)
target_include_directories(pm_core PUBLIC ${CMAKE_CURRENT_SOURCE_DIR}/include ${Boost_INCLUDE_DIRS})
target_link_libraries(pm_core PUBLIC PkgConfig::LIBCURL)
target_compile_options(pm_core PRIVATE -Wall -Wextra -Wpedantic)

add_library(pm_fast_arb
    src/fast_arb.cpp
    src/fast_ws.cpp
)
target_include_directories(pm_fast_arb PUBLIC ${CMAKE_CURRENT_SOURCE_DIR}/include)
target_link_libraries(pm_fast_arb PUBLIC pm_core Threads::Threads OpenSSL::SSL OpenSSL::Crypto)
target_compile_options(pm_fast_arb PRIVATE -Wall -Wextra -Wpedantic)

add_executable(polymarket_engine src/main.cpp)
target_link_libraries(polymarket_engine PRIVATE pm_core)

add_executable(polymarket_trade_recorder src/trade_recorder.cpp)
target_link_libraries(polymarket_trade_recorder PRIVATE pm_core)
target_compile_options(polymarket_trade_recorder PRIVATE -Wall -Wextra -Wpedantic)

add_executable(polymarket_rewards_scan src/rewards_scan.cpp)
target_link_libraries(polymarket_rewards_scan PRIVATE pm_core)
target_compile_options(polymarket_rewards_scan PRIVATE -Wall -Wextra -Wpedantic)

add_executable(polymarket_fast_arb_shadow src/fast_arb_main.cpp)
target_link_libraries(polymarket_fast_arb_shadow PRIVATE pm_fast_arb)
target_compile_options(polymarket_fast_arb_shadow PRIVATE -Wall -Wextra -Wpedantic)

include(CTest)
if(BUILD_TESTING)
    find_package(Python3 REQUIRED COMPONENTS Interpreter)

    add_executable(pm_tests tests/test_core.cpp)
    target_link_libraries(pm_tests PRIVATE pm_core)
    add_executable(pm_execution_tests tests/test_execution.cpp)
    target_link_libraries(pm_execution_tests PRIVATE pm_core)
    add_executable(pm_reward_tests tests/test_rewards.cpp)
    target_link_libraries(pm_reward_tests PRIVATE pm_core)
    add_executable(pm_market_relation_tests tests/test_market_relation.cpp)
    target_link_libraries(pm_market_relation_tests PRIVATE pm_core)
    add_executable(pm_fast_arb_tests tests/test_fast_arb.cpp)
    target_link_libraries(pm_fast_arb_tests PRIVATE pm_fast_arb)

    add_test(NAME pm_tests COMMAND pm_tests)
    add_test(NAME pm_execution_tests COMMAND pm_execution_tests)
    add_test(NAME pm_reward_tests COMMAND pm_reward_tests)
    add_test(NAME pm_market_relation_tests COMMAND pm_market_relation_tests)
    add_test(NAME pm_fast_arb_tests COMMAND pm_fast_arb_tests)
    add_test(NAME no_legacy_runtime_tests COMMAND ${Python3_EXECUTABLE} ${CMAKE_CURRENT_SOURCE_DIR}/tests/test_no_legacy_runtime.py)
    add_test(NAME v7_authorized_paper_envelope_tests COMMAND ${Python3_EXECUTABLE} ${CMAKE_CURRENT_SOURCE_DIR}/tests/test_v7_authorized_paper_envelope.py)
    add_test(NAME external_intelligence_workflow_contract_tests COMMAND ${Python3_EXECUTABLE} ${CMAKE_CURRENT_SOURCE_DIR}/tests/test_external_intelligence_workflow_contract.py)
    add_test(NAME v7_cutover_control_plane_tests COMMAND ${Python3_EXECUTABLE} ${CMAKE_CURRENT_SOURCE_DIR}/tests/test_v7_cutover_control_plane.py)
    add_test(NAME mock_integration COMMAND bash ${CMAKE_CURRENT_SOURCE_DIR}/tests/integration_mock.sh $<TARGET_FILE:polymarket_engine>)
endif()
