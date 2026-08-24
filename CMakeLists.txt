cmake_minimum_required(VERSION 3.20)
project(polymarket_quant_engine VERSION 0.9.0 LANGUAGES CXX)
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

add_library(pm_cross_venue
    src/cross_venue.cpp
)
target_include_directories(pm_cross_venue PUBLIC ${CMAKE_CURRENT_SOURCE_DIR}/include)
target_link_libraries(pm_cross_venue PUBLIC pm_core Threads::Threads OpenSSL::Crypto)
target_compile_options(pm_cross_venue PRIVATE -Wall -Wextra -Wpedantic)

add_executable(polymarket_engine src/main.cpp)
target_link_libraries(polymarket_engine PRIVATE pm_core)

add_executable(polymarket_negrisk_arb src/negrisk_arb.cpp)
target_link_libraries(polymarket_negrisk_arb PRIVATE pm_core)
target_compile_options(polymarket_negrisk_arb PRIVATE -Wall -Wextra -Wpedantic)

add_executable(polymarket_stat_arb src/stat_arb.cpp)
target_link_libraries(polymarket_stat_arb PRIVATE pm_core)
target_compile_options(polymarket_stat_arb PRIVATE -Wall -Wextra -Wpedantic)

add_executable(polymarket_pca_stat_arb src/pca_stat_arb.cpp)
target_link_libraries(polymarket_pca_stat_arb PRIVATE pm_core)
target_compile_options(polymarket_pca_stat_arb PRIVATE -Wall -Wextra -Wpedantic)

add_executable(polymarket_maker_paper src/maker_paper.cpp)
target_link_libraries(polymarket_maker_paper PRIVATE pm_core)
target_compile_options(polymarket_maker_paper PRIVATE -Wall -Wextra -Wpedantic)

add_executable(polymarket_trade_recorder src/trade_recorder.cpp)
target_link_libraries(polymarket_trade_recorder PRIVATE pm_core)
target_compile_options(polymarket_trade_recorder PRIVATE -Wall -Wextra -Wpedantic)

add_executable(polymarket_multileg_paper src/multileg_paper.cpp)
target_link_libraries(polymarket_multileg_paper PRIVATE pm_core)
target_compile_options(polymarket_multileg_paper PRIVATE -Wall -Wextra -Wpedantic)

add_executable(polymarket_rewards_scan src/rewards_scan.cpp)
target_link_libraries(polymarket_rewards_scan PRIVATE pm_core)
target_compile_options(polymarket_rewards_scan PRIVATE -Wall -Wextra -Wpedantic)

add_executable(polymarket_fast_arb_shadow src/fast_arb_main.cpp)
target_link_libraries(polymarket_fast_arb_shadow PRIVATE pm_fast_arb)
target_compile_options(polymarket_fast_arb_shadow PRIVATE -Wall -Wextra -Wpedantic)

add_executable(prediction_cross_venue_arb src/cross_venue_main.cpp)
target_link_libraries(prediction_cross_venue_arb PRIVATE pm_cross_venue)
target_compile_options(prediction_cross_venue_arb PRIVATE -Wall -Wextra -Wpedantic)

add_executable(prediction_cross_venue_auth_check src/cross_venue_auth_main.cpp)
target_link_libraries(prediction_cross_venue_auth_check PRIVATE pm_cross_venue)
target_compile_options(prediction_cross_venue_auth_check PRIVATE -Wall -Wextra -Wpedantic)

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
    add_executable(pm_cross_venue_tests tests/test_cross_venue.cpp)
    target_link_libraries(pm_cross_venue_tests PRIVATE pm_cross_venue)
    add_test(NAME pm_tests COMMAND pm_tests)
    add_test(NAME pm_execution_tests COMMAND pm_execution_tests)
    add_test(NAME pm_reward_tests COMMAND pm_reward_tests)
    add_test(NAME pm_market_relation_tests COMMAND pm_market_relation_tests)
    add_test(NAME pm_fast_arb_tests COMMAND pm_fast_arb_tests)
    add_test(NAME pm_cross_venue_tests COMMAND pm_cross_venue_tests)
    add_test(NAME v4_research_tests COMMAND ${Python3_EXECUTABLE} ${CMAKE_CURRENT_SOURCE_DIR}/tests/test_v4_research.py)
    add_test(NAME runtime_supervisor_tests COMMAND ${Python3_EXECUTABLE} ${CMAKE_CURRENT_SOURCE_DIR}/tests/test_runtime_supervisor.py)
    add_test(NAME live_smoke_summary_tests COMMAND ${Python3_EXECUTABLE} ${CMAKE_CURRENT_SOURCE_DIR}/tests/test_live_smoke_summary.py)
    add_test(NAME runtime_action_report_tests COMMAND ${Python3_EXECUTABLE} ${CMAKE_CURRENT_SOURCE_DIR}/tests/test_runtime_action_report.py)
    add_test(NAME coherent_hedge_filter_tests COMMAND ${Python3_EXECUTABLE} ${CMAKE_CURRENT_SOURCE_DIR}/tests/test_coherent_hedges.py)
    add_test(NAME reward_payout_floor_tests COMMAND ${Python3_EXECUTABLE} ${CMAKE_CURRENT_SOURCE_DIR}/tests/test_reward_payout_floor.py)
    add_test(NAME forward_maker_probe_tests COMMAND ${Python3_EXECUTABLE} ${CMAKE_CURRENT_SOURCE_DIR}/tests/test_forward_maker_probe.py)
    add_test(NAME forward_candidate_selection_tests COMMAND ${Python3_EXECUTABLE} ${CMAKE_CURRENT_SOURCE_DIR}/tests/test_forward_candidate_selection.py)
    add_test(NAME finalize_forward_probe_tests COMMAND ${Python3_EXECUTABLE} ${CMAKE_CURRENT_SOURCE_DIR}/tests/test_finalize_forward_probe.py)
    add_test(NAME forward_maker_calibration_tests COMMAND ${Python3_EXECUTABLE} ${CMAKE_CURRENT_SOURCE_DIR}/tests/test_calibrate_forward_maker.py)
    add_test(NAME github_contents_request_tests COMMAND ${Python3_EXECUTABLE} ${CMAKE_CURRENT_SOURCE_DIR}/tests/test_github_contents_request.py)
    add_test(NAME walk_forward_execution_filter_tests COMMAND ${Python3_EXECUTABLE} ${CMAKE_CURRENT_SOURCE_DIR}/tests/test_walk_forward_execution_filter.py)
    add_test(NAME model_governance_tests COMMAND ${Python3_EXECUTABLE} ${CMAKE_CURRENT_SOURCE_DIR}/tests/test_model_governance.py)
    add_test(NAME research_policy_branch_classification_tests COMMAND ${Python3_EXECUTABLE} ${CMAKE_CURRENT_SOURCE_DIR}/tests/test_research_policy_branch_classification.py)
    add_test(NAME arb_theory_scheduler_tests COMMAND ${Python3_EXECUTABLE} ${CMAKE_CURRENT_SOURCE_DIR}/tests/test_arb_theory_scheduler.py)
    add_test(NAME fast_runtime_contract_tests COMMAND ${Python3_EXECUTABLE} ${CMAKE_CURRENT_SOURCE_DIR}/tests/test_fast_runtime_contract.py)
    add_test(NAME incumbent_health_gate_tests COMMAND ${Python3_EXECUTABLE} ${CMAKE_CURRENT_SOURCE_DIR}/tests/test_incumbent_health_gate.py)
    add_test(NAME portfolio_supervisor_tests COMMAND ${Python3_EXECUTABLE} ${CMAKE_CURRENT_SOURCE_DIR}/tests/test_portfolio_supervisor.py)
    add_test(NAME mock_integration COMMAND bash ${CMAKE_CURRENT_SOURCE_DIR}/tests/integration_mock.sh $<TARGET_FILE:polymarket_engine>)
endif()
