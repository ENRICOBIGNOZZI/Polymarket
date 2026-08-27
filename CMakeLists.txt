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
    add_test(NAME v7_execution_evidence_tests COMMAND ${Python3_EXECUTABLE} ${CMAKE_CURRENT_SOURCE_DIR}/tests/test_v7_execution_evidence.py)
    add_test(NAME v7_execution_ledger_tests COMMAND ${Python3_EXECUTABLE} ${CMAKE_CURRENT_SOURCE_DIR}/tests/test_v7_execution_ledger.py)
    add_test(NAME v7_learned_execution_model_tests COMMAND ${Python3_EXECUTABLE} ${CMAKE_CURRENT_SOURCE_DIR}/tests/test_v7_learned_execution_model.py)
    add_test(NAME v7_canonical_economics_tests COMMAND ${Python3_EXECUTABLE} ${CMAKE_CURRENT_SOURCE_DIR}/tests/test_v7_canonical_economics.py)
    add_test(NAME v7_complete_set_maker_tests COMMAND ${Python3_EXECUTABLE} ${CMAKE_CURRENT_SOURCE_DIR}/tests/test_v7_complete_set_maker.py)
    add_test(NAME v7_model_intent_router_tests COMMAND ${Python3_EXECUTABLE} ${CMAKE_CURRENT_SOURCE_DIR}/tests/test_v7_model_intent_router.py)
    add_test(NAME v7_profit_stack_runtime_tests COMMAND ${Python3_EXECUTABLE} ${CMAKE_CURRENT_SOURCE_DIR}/tests/test_v7_profit_stack_runtime.py)
    add_test(NAME v7_point_in_time_universe_native_tests COMMAND ${Python3_EXECUTABLE} ${CMAKE_CURRENT_SOURCE_DIR}/tests/test_v7_point_in_time_universe_native.py)
    add_test(NAME v7_external_bridge_profit_stack_tests COMMAND ${Python3_EXECUTABLE} ${CMAKE_CURRENT_SOURCE_DIR}/tests/test_v7_external_bridge_profit_stack.py)
    add_test(NAME v7_native_monitoring_tests COMMAND ${Python3_EXECUTABLE} ${CMAKE_CURRENT_SOURCE_DIR}/tests/test_monitoring_v7_native.py)
    add_test(NAME v7_ledger_monitoring_tests COMMAND ${Python3_EXECUTABLE} ${CMAKE_CURRENT_SOURCE_DIR}/tests/test_monitoring_v7_ledger.py)
    add_test(NAME v7_dashboard_completion_tests COMMAND ${Python3_EXECUTABLE} ${CMAKE_CURRENT_SOURCE_DIR}/tests/test_monitoring_v7_dashboard_completion.py)
    add_test(NAME external_intelligence_workflow_contract_tests COMMAND ${Python3_EXECUTABLE} ${CMAKE_CURRENT_SOURCE_DIR}/tests/test_external_intelligence_workflow_contract.py)
    add_test(NAME v7_live_paper_validation_contract_tests COMMAND ${Python3_EXECUTABLE} ${CMAKE_CURRENT_SOURCE_DIR}/tests/test_v7_live_paper_validation_contract.py)
    add_test(NAME v7_cutover_updater_tests COMMAND ${Python3_EXECUTABLE} ${CMAKE_CURRENT_SOURCE_DIR}/tests/test_v7_cutover_updater.py)
    add_test(NAME v7_paper_evidence_router_tests COMMAND ${Python3_EXECUTABLE} ${CMAKE_CURRENT_SOURCE_DIR}/tests/test_v7_paper_evidence_router.py)
    add_test(NAME v7_paper_entrypoint_cutover_tests COMMAND ${Python3_EXECUTABLE} ${CMAKE_CURRENT_SOURCE_DIR}/tests/test_v7_paper_entrypoint_cutover.py)
    add_test(NAME v7_fast_runtime_contract_tests COMMAND ${Python3_EXECUTABLE} ${CMAKE_CURRENT_SOURCE_DIR}/tests/test_fast_runtime_contract.py)

    add_test(NAME v7_current_model_state_tests COMMAND ${Python3_EXECUTABLE} ${CMAKE_CURRENT_SOURCE_DIR}/tests/test_v7_current_model_state.py)
    add_test(NAME v7_local_factor_by_contract_tests COMMAND ${Python3_EXECUTABLE} ${CMAKE_CURRENT_SOURCE_DIR}/tests/test_v7_local_factor_by_contract.py)
    add_test(NAME v7_local_factor_core_tests COMMAND ${Python3_EXECUTABLE} ${CMAKE_CURRENT_SOURCE_DIR}/tests/test_v7_local_factor_core.py)
    add_test(NAME v7_local_factor_inference_tests COMMAND ${Python3_EXECUTABLE} ${CMAKE_CURRENT_SOURCE_DIR}/tests/test_v7_local_factor_inference.py)
    add_test(NAME v7_local_factor_multiplicity_tests COMMAND ${Python3_EXECUTABLE} ${CMAKE_CURRENT_SOURCE_DIR}/tests/test_v7_local_factor_multiplicity.py)
    add_test(NAME v7_local_factor_orientation_tests COMMAND ${Python3_EXECUTABLE} ${CMAKE_CURRENT_SOURCE_DIR}/tests/test_v7_local_factor_orientation.py)
    add_test(NAME v7_local_factor_pairs_tests COMMAND ${Python3_EXECUTABLE} ${CMAKE_CURRENT_SOURCE_DIR}/tests/test_v7_local_factor_pairs.py)
    add_test(NAME v7_pca_stat_arb_tests COMMAND ${Python3_EXECUTABLE} ${CMAKE_CURRENT_SOURCE_DIR}/tests/test_v7_pca_stat_arb.py)
    add_test(NAME v7_pca_stat_arb_inference_tests COMMAND ${Python3_EXECUTABLE} ${CMAKE_CURRENT_SOURCE_DIR}/tests/test_v7_pca_stat_arb_inference.py)
    add_test(NAME v7_cross_sectional_history_tests COMMAND ${Python3_EXECUTABLE} ${CMAKE_CURRENT_SOURCE_DIR}/tests/test_v7_cross_sectional_history.py)
    add_test(NAME v7_cross_sectional_rank_tests COMMAND ${Python3_EXECUTABLE} ${CMAKE_CURRENT_SOURCE_DIR}/tests/test_v7_cross_sectional_rank.py)
    add_test(NAME v7_cross_sectional_rank_fast_tests COMMAND ${Python3_EXECUTABLE} ${CMAKE_CURRENT_SOURCE_DIR}/tests/test_v7_cross_sectional_rank_fast.py)
    add_test(NAME v7_cross_sectional_rank_forward_tests COMMAND ${Python3_EXECUTABLE} ${CMAKE_CURRENT_SOURCE_DIR}/tests/test_v7_cross_sectional_rank_forward.py)
    add_test(NAME v7_cross_sectional_rank_inference_tests COMMAND ${Python3_EXECUTABLE} ${CMAKE_CURRENT_SOURCE_DIR}/tests/test_v7_cross_sectional_rank_inference.py)
    add_test(NAME v7_cross_sectional_relative_tests COMMAND ${Python3_EXECUTABLE} ${CMAKE_CURRENT_SOURCE_DIR}/tests/test_v7_cross_sectional_relative.py)
    add_test(NAME v7_ranking_causal_snapshot_tests COMMAND ${Python3_EXECUTABLE} ${CMAKE_CURRENT_SOURCE_DIR}/tests/test_v7_ranking_causal_snapshot.py)
    add_test(NAME v7_cross_sectional_rank_15m_tests COMMAND ${Python3_EXECUTABLE} ${CMAKE_CURRENT_SOURCE_DIR}/tests/test_v7_cross_sectional_rank_15m.py)
    add_test(NAME v7_micro_taker_core_tests COMMAND ${Python3_EXECUTABLE} ${CMAKE_CURRENT_SOURCE_DIR}/tests/test_v7_micro_taker_core.py)
    add_test(NAME v7_micro_target_tests COMMAND ${Python3_EXECUTABLE} ${CMAKE_CURRENT_SOURCE_DIR}/tests/test_v7_micro_target.py)

    add_test(NAME mock_integration COMMAND bash ${CMAKE_CURRENT_SOURCE_DIR}/tests/integration_mock.sh $<TARGET_FILE:polymarket_engine>)
endif()