cmake_minimum_required(VERSION 3.20)
project(polymarket_v7 VERSION 7.0.0 LANGUAGES CXX)
set(CMAKE_CXX_STANDARD 20)
set(CMAKE_CXX_STANDARD_REQUIRED ON)
set(CMAKE_CXX_EXTENSIONS OFF)
option(PM_LONDON_RUNTIME_ONLY "Build only binaries required by the London execution runtime" OFF)

# Release-only interprocedural optimization.  This is deliberately portable:
# unsupported toolchains fall back to ordinary Release instead of changing
# runtime semantics or requiring host-specific ISA flags.
include(CheckIPOSupported)
check_ipo_supported(RESULT PM_V7_IPO_SUPPORTED OUTPUT PM_V7_IPO_ERROR LANGUAGES CXX)
if(PM_V7_IPO_SUPPORTED)
  set(CMAKE_INTERPROCEDURAL_OPTIMIZATION_RELEASE ON)
endif()

if(POLICY CMP0167)
  cmake_policy(SET CMP0167 NEW)
endif()

find_package(PkgConfig REQUIRED)
find_package(Boost REQUIRED)
find_package(Threads REQUIRED)
find_package(OpenSSL REQUIRED)
pkg_check_modules(LIBCURL REQUIRED IMPORTED_TARGET libcurl)
pkg_check_modules(SECP256K1 REQUIRED IMPORTED_TARGET libsecp256k1)

# Boost.JSON is compiled exactly once and shared by all V7 C++ components.
# This keeps the bounded market-data decoder independent from HTTP/REST code.
add_library(pm_json src/boost_json.cpp)
target_include_directories(pm_json PUBLIC ${Boost_INCLUDE_DIRS})
target_compile_options(pm_json PRIVATE -Wall -Wextra -Wpedantic)

add_library(pm_v7_execution_mode src/v7_execution_mode.cpp)
target_include_directories(pm_v7_execution_mode PUBLIC ${CMAKE_CURRENT_SOURCE_DIR}/include)
target_compile_options(pm_v7_execution_mode PRIVATE -Wall -Wextra -Wpedantic)

add_library(pm_core src/http.cpp src/api.cpp src/event_api.cpp src/config.cpp)
target_include_directories(pm_core PUBLIC ${CMAKE_CURRENT_SOURCE_DIR}/include ${Boost_INCLUDE_DIRS})
target_link_libraries(pm_core PUBLIC PkgConfig::LIBCURL pm_json pm_v7_execution_mode)
target_compile_options(pm_core PRIVATE -Wall -Wextra -Wpedantic)

add_library(pm_fast_arb src/fast_arb.cpp src/fast_ws.cpp)
target_include_directories(pm_fast_arb PUBLIC ${CMAKE_CURRENT_SOURCE_DIR}/include)
target_link_libraries(pm_fast_arb PUBLIC pm_core Threads::Threads OpenSSL::SSL OpenSSL::Crypto)
target_compile_options(pm_fast_arb PRIVATE -Wall -Wextra -Wpedantic)

add_library(pm_v7_common
  src/v7_oms.cpp
  src/v7_paper_queue.cpp
  src/v7_market_state.cpp
  src/v7_markout.cpp
  src/v7_market_ws.cpp
  src/v7_capital.cpp
  src/v7_execution_admission.cpp
  src/v7_crypto_decision_lane.cpp
  src/v7_multirate_context.cpp
  src/v7_native_order_tx.cpp
  src/v7_native_settlement_authority.cpp
  src/v7_native_paper_execution.cpp
  src/v7_native_paper_pair_execution.cpp
  src/v7_native_runtime_evidence.cpp
  src/v7_latency_trace.cpp)
target_include_directories(pm_v7_common PUBLIC ${CMAKE_CURRENT_SOURCE_DIR}/include ${Boost_INCLUDE_DIRS})
target_link_libraries(pm_v7_common PUBLIC pm_json Threads::Threads)
target_compile_options(pm_v7_common PRIVATE -Wall -Wextra -Wpedantic)


add_library(pm_v7_probability_model src/v7_probability_model.cpp)
target_include_directories(pm_v7_probability_model PUBLIC ${CMAKE_CURRENT_SOURCE_DIR}/include ${Boost_INCLUDE_DIRS})
target_link_libraries(pm_v7_probability_model PUBLIC pm_v7_common OpenSSL::Crypto)
target_compile_options(pm_v7_probability_model PRIVATE -Wall -Wextra -Wpedantic)

add_library(pm_v7_clob_native
  src/v7_keccak_fast.cpp
  src/v7_clob_eip712.cpp
  src/v7_poly1271.cpp
  src/v7_clob_order_amounts.cpp
  src/v7_clob_order_salt.cpp
  src/v7_clob_wire.cpp
  src/v7_clob_prepared_order.cpp
  src/v7_clob_http_frame.cpp
  src/v7_clob_prepared_post.cpp
  src/v7_clob_http1_response.cpp
  src/v7_clob_http_response.cpp
  src/v7_clob_order_identity.cpp
  src/v7_clob_tls.cpp)
target_include_directories(pm_v7_clob_native PUBLIC ${CMAKE_CURRENT_SOURCE_DIR}/include ${Boost_INCLUDE_DIRS})
target_link_libraries(pm_v7_clob_native PUBLIC pm_json Threads::Threads OpenSSL::SSL OpenSSL::Crypto PkgConfig::SECP256K1)
target_compile_options(pm_v7_clob_native PRIVATE -Wall -Wextra -Wpedantic)

add_library(pm_v7_account_native
  src/v7_user_ws.cpp
  src/v7_user_ws_feed.cpp
  src/v7_user_oms_bridge.cpp
  src/v7_native_clob_order_lane.cpp)
target_include_directories(pm_v7_account_native PUBLIC ${CMAKE_CURRENT_SOURCE_DIR}/include ${Boost_INCLUDE_DIRS})
target_link_libraries(pm_v7_account_native PUBLIC pm_v7_common pm_v7_clob_native pm_json Threads::Threads OpenSSL::SSL OpenSSL::Crypto)
target_compile_options(pm_v7_account_native PRIVATE -Wall -Wextra -Wpedantic)

add_library(pm_v7_hft
  src/v7_maker_hft.cpp
  src/v7_maker_execution_cells.cpp
  src/v7_maker_lane.cpp
  src/v7_maker_paper.cpp
  src/v7_maker_inventory.cpp
  src/v7_maker_execution_policy.cpp)
target_include_directories(pm_v7_hft PUBLIC ${CMAKE_CURRENT_SOURCE_DIR}/include ${Boost_INCLUDE_DIRS})
target_link_libraries(pm_v7_hft PUBLIC pm_v7_common Threads::Threads OpenSSL::Crypto)
target_compile_options(pm_v7_hft PRIVATE -Wall -Wextra -Wpedantic)

# Settlement-aware external information is a V7 capability, not a second
# runtime/OMS. This library contains bounded ingress normalization, causal hot
# state, fair-value economics, execution arbitration, async evidence tape and
# deterministic offline replay primitives.
add_library(pm_v7_external
  src/v7_external_fair.cpp
  src/v7_binance_l2.cpp
  src/v7_binance_l2_protocol.cpp
  src/v7_coinbase_l2.cpp
  src/v7_coinbase_l2_observer.cpp
  src/v7_external_state.cpp
  src/v7_external_execution.cpp
  src/v7_external_kernel.cpp
  src/v7_external_protocol.cpp
  src/v7_external_ingress.cpp
  src/v7_ingress_wakeup.cpp
  src/v7_external_ws.cpp
  src/v7_latest_json_publisher.cpp
  src/v7_external_cancel_publisher.cpp
  src/v7_external_tape.cpp
  src/v7_external_replay.cpp)
target_include_directories(pm_v7_external PUBLIC ${CMAKE_CURRENT_SOURCE_DIR}/include ${Boost_INCLUDE_DIRS})
target_link_libraries(pm_v7_external PUBLIC pm_v7_common Threads::Threads pm_json OpenSSL::SSL OpenSSL::Crypto)
target_compile_options(pm_v7_external PRIVATE -Wall -Wextra -Wpedantic)

add_executable(polymarket_v7_trade_recorder src/v7_trade_recorder.cpp)
target_link_libraries(polymarket_v7_trade_recorder PRIVATE pm_core)
target_compile_options(polymarket_v7_trade_recorder PRIVATE -Wall -Wextra -Wpedantic)

if(NOT PM_LONDON_RUNTIME_ONLY)
add_executable(polymarket_v7_multirate_bench src/v7_multirate_bench.cpp)
target_link_libraries(polymarket_v7_multirate_bench PRIVATE pm_v7_common)
target_compile_options(polymarket_v7_multirate_bench PRIVATE -Wall -Wextra -Wpedantic)
endif()

if(NOT PM_LONDON_RUNTIME_ONLY)
add_executable(polymarket_v7_maker_replay_bench src/v7_maker_replay_bench.cpp)
target_link_libraries(polymarket_v7_maker_replay_bench PRIVATE pm_v7_hft)
target_compile_options(polymarket_v7_maker_replay_bench PRIVATE -Wall -Wextra -Wpedantic)
endif()

if(NOT PM_LONDON_RUNTIME_ONLY)
add_executable(polymarket_v7_latency_probe src/v7_latency_probe.cpp)
target_link_libraries(polymarket_v7_latency_probe PRIVATE pm_core)
target_compile_options(polymarket_v7_latency_probe PRIVATE -Wall -Wextra -Wpedantic)
endif()

add_executable(polymarket_v7_maker_markout_observer src/v7_maker_markout_observer.cpp)
target_link_libraries(polymarket_v7_maker_markout_observer PRIVATE pm_v7_common pm_fast_arb pm_core)
target_compile_options(polymarket_v7_maker_markout_observer PRIVATE -Wall -Wextra -Wpedantic)

add_executable(polymarket_v7_maker_fillability_observer src/v7_maker_fillability_observer.cpp)
target_link_libraries(polymarket_v7_maker_fillability_observer PRIVATE pm_v7_hft pm_v7_common pm_fast_arb pm_core)
target_compile_options(polymarket_v7_maker_fillability_observer PRIVATE -Wall -Wextra -Wpedantic)

if(NOT PM_LONDON_RUNTIME_ONLY)
add_executable(polymarket_v7_maker_research_replay src/v7_maker_research_replay.cpp)
target_link_libraries(polymarket_v7_maker_research_replay PRIVATE pm_v7_hft)
target_compile_options(polymarket_v7_maker_research_replay PRIVATE -Wall -Wextra -Wpedantic)
endif()

if(NOT PM_LONDON_RUNTIME_ONLY)
# Offline queue-handoff comparison; no market or execution authority.
add_executable(polymarket_v7_crypto_decision_bench src/v7_crypto_decision_bench.cpp)
target_link_libraries(polymarket_v7_crypto_decision_bench PRIVATE pm_v7_external)
target_compile_options(polymarket_v7_crypto_decision_bench PRIVATE -Wall -Wextra -Wpedantic)

add_executable(polymarket_v7_crypto_to_oms_bench src/v7_crypto_to_oms_bench.cpp)
target_link_libraries(polymarket_v7_crypto_to_oms_bench PRIVATE pm_v7_external pm_v7_common)
target_compile_options(polymarket_v7_crypto_to_oms_bench PRIVATE -Wall -Wextra -Wpedantic)

add_executable(polymarket_v7_native_clob_full_prewire_bench src/v7_native_clob_full_prewire_bench.cpp)
target_link_libraries(polymarket_v7_native_clob_full_prewire_bench PRIVATE pm_v7_clob_native)
target_compile_options(polymarket_v7_native_clob_full_prewire_bench PRIVATE -Wall -Wextra -Wpedantic)

add_executable(polymarket_v7_feed_race_probe src/v7_feed_race_probe.cpp)
target_link_libraries(polymarket_v7_feed_race_probe PRIVATE pm_v7_external)
target_compile_options(polymarket_v7_feed_race_probe PRIVATE -Wall -Wextra -Wpedantic)

add_executable(polymarket_v7_ingress_latency_bench src/v7_ingress_latency_bench.cpp)
target_link_libraries(polymarket_v7_ingress_latency_bench PRIVATE pm_v7_external)
target_compile_options(polymarket_v7_ingress_latency_bench PRIVATE -Wall -Wextra -Wpedantic)
endif()

add_executable(polymarket_v7_external_venue_runtime src/v7_external_venue_runtime.cpp)
target_link_libraries(polymarket_v7_external_venue_runtime PRIVATE pm_v7_external)
target_compile_options(polymarket_v7_external_venue_runtime PRIVATE -Wall -Wextra -Wpedantic)

# One canonical London PAPER native executable; no parallel candidate runtime.
add_executable(polymarket_v7_crypto_settlement_engine
  src/v7_crypto_settlement_engine.cpp)
target_link_libraries(polymarket_v7_crypto_settlement_engine PRIVATE
  pm_v7_hft pm_v7_external pm_v7_common pm_v7_probability_model pm_fast_arb pm_core Threads::Threads)

target_compile_options(polymarket_v7_crypto_settlement_engine PRIVATE
  -Wall -Wextra -Wpedantic)

if(NOT PM_LONDON_RUNTIME_ONLY)
add_executable(polymarket_v7_external_tape_replay src/v7_external_tape_replay.cpp)
target_link_libraries(polymarket_v7_external_tape_replay PRIVATE pm_v7_external)
target_compile_options(polymarket_v7_external_tape_replay PRIVATE -Wall -Wextra -Wpedantic)
endif()

# Research-only deterministic decoder for the frozen BTC M5 external-cancel forward study.
# Research-only high-frequency BTC M5 CLOB books. It reuses the canonical
# public WebSocket decoder and segmented evidence tape, but is not launched by
# the production process manifest and owns no execution authority.
include(CTest)
if(PM_LONDON_RUNTIME_ONLY AND BUILD_TESTING)
  message(FATAL_ERROR "PM_LONDON_RUNTIME_ONLY requires BUILD_TESTING=OFF")
endif()
if(BUILD_TESTING)
  add_test(NAME pm_v7_multirate_compute_bench_smoke COMMAND polymarket_v7_multirate_bench)
  add_executable(pm_v7_multirate_context_tests tests/test_v7_multirate_context.cpp)
  target_link_libraries(pm_v7_multirate_context_tests PRIVATE pm_v7_external)
  target_compile_options(pm_v7_multirate_context_tests PRIVATE -UNDEBUG)
  add_test(NAME pm_v7_multirate_context_tests COMMAND pm_v7_multirate_context_tests)
  find_package(Python3 REQUIRED COMPONENTS Interpreter)
  execute_process(COMMAND ${Python3_EXECUTABLE} -c "import pytest"
                  RESULT_VARIABLE V7_PYTEST_RESULT OUTPUT_QUIET ERROR_QUIET)
  if(NOT V7_PYTEST_RESULT EQUAL 0)
    message(FATAL_ERROR "BUILD_TESTING requires pytest for ${Python3_EXECUTABLE}; function tests must not silently pass")
  endif()

  add_test(NAME pm_v7_latency_probe_validate_public
    COMMAND polymarket_v7_latency_probe --region validation --exact-code-sha 1111111111111111111111111111111111111111 --validate-only)
  add_test(NAME pm_v7_latency_probe_reject_order_endpoint
    COMMAND polymarket_v7_latency_probe --region validation --exact-code-sha 1111111111111111111111111111111111111111 --endpoint https://clob.polymarket.com/order --validate-only)
  add_test(NAME pm_v7_latency_probe_reject_credentials
    COMMAND polymarket_v7_latency_probe --region validation --exact-code-sha 1111111111111111111111111111111111111111 --endpoint https://user@clob.polymarket.com/time --validate-only)
  add_test(NAME pm_v7_latency_probe_reject_integer_overflow
    COMMAND polymarket_v7_latency_probe --region validation --exact-code-sha 1111111111111111111111111111111111111111 --interval-ms 99999999999999999999999999 --validate-only)
  set_tests_properties(pm_v7_latency_probe_reject_order_endpoint pm_v7_latency_probe_reject_credentials
    pm_v7_latency_probe_reject_integer_overflow PROPERTIES WILL_FAIL TRUE)
  add_executable(pm_v7_causal_book_observer_tests tests/test_v7_causal_book_observer.cpp)
  target_link_libraries(pm_v7_causal_book_observer_tests PRIVATE pm_v7_hft pm_fast_arb pm_core)
  target_compile_options(pm_v7_causal_book_observer_tests PRIVATE -UNDEBUG)
  add_test(NAME pm_v7_causal_book_observer_tests COMMAND pm_v7_causal_book_observer_tests)
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

  add_executable(pm_v7_markout_tests tests/test_v7_markout.cpp)
  target_link_libraries(pm_v7_markout_tests PRIVATE pm_v7_common)
  add_test(NAME pm_v7_markout_tests COMMAND pm_v7_markout_tests)

  add_executable(pm_v7_jsonl_tail_tests tests/test_v7_jsonl_tail.cpp)
  target_include_directories(pm_v7_jsonl_tail_tests PRIVATE ${CMAKE_CURRENT_SOURCE_DIR}/include)
  add_test(NAME pm_v7_jsonl_tail_tests COMMAND pm_v7_jsonl_tail_tests)

  add_executable(pm_v7_capital_tests tests/test_v7_capital.cpp)
  target_link_libraries(pm_v7_capital_tests PRIVATE pm_v7_common)
  add_test(NAME pm_v7_capital_tests COMMAND pm_v7_capital_tests)

  add_executable(pm_v7_execution_admission_tests tests/test_v7_execution_admission.cpp)
  target_link_libraries(pm_v7_execution_admission_tests PRIVATE pm_v7_common)
  add_test(NAME pm_v7_execution_admission_tests COMMAND pm_v7_execution_admission_tests)

  add_executable(pm_v7_probability_model_tests tests/test_v7_probability_model.cpp)
  target_link_libraries(pm_v7_probability_model_tests PRIVATE pm_v7_probability_model)
  target_compile_options(pm_v7_probability_model_tests PRIVATE -UNDEBUG)
  add_test(NAME pm_v7_probability_model_tests COMMAND pm_v7_probability_model_tests)

  add_executable(pm_v7_probability_ev_tests tests/test_v7_probability_ev.cpp)
  target_include_directories(pm_v7_probability_ev_tests PRIVATE include)
  target_compile_options(pm_v7_probability_ev_tests PRIVATE -UNDEBUG)
  add_test(NAME pm_v7_probability_ev_tests COMMAND pm_v7_probability_ev_tests)

  add_executable(pm_v7_crypto_decision_lane_tests tests/test_v7_crypto_decision_lane.cpp)
  target_link_libraries(pm_v7_crypto_decision_lane_tests PRIVATE pm_v7_common)
  target_compile_options(pm_v7_crypto_decision_lane_tests PRIVATE -UNDEBUG)
  add_test(NAME pm_v7_crypto_decision_lane_tests COMMAND pm_v7_crypto_decision_lane_tests)

  add_executable(pm_v7_coinbase_l2_observer_tests tests/test_v7_coinbase_l2_observer.cpp)
  target_link_libraries(pm_v7_coinbase_l2_observer_tests PRIVATE pm_v7_external)
  target_compile_options(pm_v7_coinbase_l2_observer_tests PRIVATE -UNDEBUG)
  add_test(NAME pm_v7_coinbase_l2_observer_tests COMMAND pm_v7_coinbase_l2_observer_tests)

  add_test(NAME pm_v7_crypto_settlement_native_candidate_validate
    COMMAND polymarket_v7_crypto_settlement_engine --validate-only)
  add_test(NAME pm_v7_crypto_settlement_native_candidate_10k_limits
    COMMAND polymarket_v7_crypto_settlement_engine
      --sleeve-budget-microdollars 10000000000
      --max-total-exposure-microdollars 10000000000
      --max-market-exposure-microdollars 333333333
      --max-single-order-microdollars 20000000
      --validate-only)

  add_executable(pm_v7_native_order_tx_tests tests/test_v7_native_order_tx.cpp)
  target_link_libraries(pm_v7_native_order_tx_tests PRIVATE pm_v7_common)
  target_compile_options(pm_v7_native_order_tx_tests PRIVATE -UNDEBUG)
  add_test(NAME pm_v7_native_order_tx_tests COMMAND pm_v7_native_order_tx_tests)

  add_executable(pm_v7_native_settlement_authority_tests
    tests/test_v7_native_settlement_authority.cpp)
  target_link_libraries(pm_v7_native_settlement_authority_tests PRIVATE pm_v7_common)
  target_compile_options(pm_v7_native_settlement_authority_tests PRIVATE -UNDEBUG)
  add_test(NAME pm_v7_native_settlement_authority_tests
    COMMAND pm_v7_native_settlement_authority_tests)

  add_executable(pm_v7_native_settlement_oms_endpoint_tests
    tests/test_v7_native_settlement_oms_endpoint.cpp)
  target_link_libraries(pm_v7_native_settlement_oms_endpoint_tests PRIVATE pm_v7_common)
  add_test(NAME pm_v7_native_settlement_oms_endpoint_tests
    COMMAND pm_v7_native_settlement_oms_endpoint_tests)

  add_executable(pm_v7_native_paper_execution_tests
    tests/test_v7_native_paper_execution.cpp)
  target_link_libraries(pm_v7_native_paper_execution_tests PRIVATE pm_v7_common)
  target_compile_options(pm_v7_native_paper_execution_tests PRIVATE -UNDEBUG)
  add_test(NAME pm_v7_native_paper_execution_tests
    COMMAND pm_v7_native_paper_execution_tests)

  add_executable(pm_v7_native_paper_pair_execution_tests
    tests/test_v7_native_paper_pair_execution.cpp)
  target_link_libraries(pm_v7_native_paper_pair_execution_tests PRIVATE pm_v7_common)
  target_compile_options(pm_v7_native_paper_pair_execution_tests PRIVATE -UNDEBUG)
  add_test(NAME pm_v7_native_paper_pair_execution_tests
    COMMAND pm_v7_native_paper_pair_execution_tests)

  add_executable(pm_v7_native_runtime_evidence_tests
    tests/test_v7_native_runtime_evidence.cpp)
  target_link_libraries(pm_v7_native_runtime_evidence_tests PRIVATE pm_v7_common)
  target_compile_options(pm_v7_native_runtime_evidence_tests PRIVATE -UNDEBUG)
  add_test(NAME pm_v7_native_runtime_evidence_tests
    COMMAND pm_v7_native_runtime_evidence_tests)

  add_executable(pm_v7_latency_trace_tests tests/test_v7_latency_trace.cpp)
  target_link_libraries(pm_v7_latency_trace_tests PRIVATE pm_v7_common)
  target_compile_options(pm_v7_latency_trace_tests PRIVATE -UNDEBUG)
  add_test(NAME pm_v7_latency_trace_tests COMMAND pm_v7_latency_trace_tests)

  add_executable(pm_v7_native_clob_order_lane_driver tests/v7_native_clob_order_lane_driver.cpp)
  target_link_libraries(pm_v7_native_clob_order_lane_driver PRIVATE pm_v7_account_native)
  target_compile_options(pm_v7_native_clob_order_lane_driver PRIVATE -UNDEBUG)

  add_executable(pm_v7_clob_rate_limit_tests tests/test_v7_clob_rate_limit.cpp)
  target_link_libraries(pm_v7_clob_rate_limit_tests PRIVATE pm_v7_clob_native)
  target_compile_options(pm_v7_clob_rate_limit_tests PRIVATE -UNDEBUG)
  add_test(NAME pm_v7_clob_rate_limit_tests COMMAND pm_v7_clob_rate_limit_tests)

  add_executable(pm_v7_pure_arb_lane_tests tests/test_v7_pure_arb_lane.cpp)
  target_link_libraries(pm_v7_pure_arb_lane_tests PRIVATE pm_v7_common)
  target_compile_options(pm_v7_pure_arb_lane_tests PRIVATE -UNDEBUG)
  add_test(NAME pm_v7_pure_arb_lane_tests COMMAND pm_v7_pure_arb_lane_tests)

  add_test(NAME pm_v7_native_clob_full_prewire_bench_smoke
    COMMAND polymarket_v7_native_clob_full_prewire_bench 64)



  add_executable(pm_v7_execution_mode_tests tests/test_v7_execution_mode.cpp)
  target_link_libraries(pm_v7_execution_mode_tests PRIVATE pm_v7_execution_mode)
  add_test(NAME pm_v7_execution_mode_tests COMMAND pm_v7_execution_mode_tests)





  add_executable(pm_v7_queue_visibility_tests tests/test_v7_queue_visibility.cpp)
  target_link_libraries(pm_v7_queue_visibility_tests PRIVATE pm_v7_common)
  add_test(NAME pm_v7_queue_visibility_tests COMMAND pm_v7_queue_visibility_tests)

  add_executable(pm_v7_market_ws_tests tests/test_v7_market_ws.cpp)
  target_link_libraries(pm_v7_market_ws_tests PRIVATE pm_v7_common)
  add_test(NAME pm_v7_market_ws_tests COMMAND pm_v7_market_ws_tests)

  add_executable(pm_v7_maker_hft_tests tests/test_v7_maker_hft.cpp)
  target_link_libraries(pm_v7_maker_hft_tests PRIVATE pm_v7_hft)
  add_test(NAME pm_v7_maker_hft_tests COMMAND pm_v7_maker_hft_tests)

  add_executable(pm_v7_maker_execution_cells_tests tests/test_v7_maker_execution_cells.cpp)
  target_link_libraries(pm_v7_maker_execution_cells_tests PRIVATE pm_v7_hft)
  target_compile_options(pm_v7_maker_execution_cells_tests PRIVATE -UNDEBUG)
  add_test(NAME pm_v7_maker_execution_cells_tests COMMAND pm_v7_maker_execution_cells_tests)

  add_executable(pm_v7_maker_lane_tests tests/test_v7_maker_lane.cpp)
  target_link_libraries(pm_v7_maker_lane_tests PRIVATE pm_v7_hft)
  add_test(NAME pm_v7_maker_lane_tests COMMAND pm_v7_maker_lane_tests)

  add_executable(pm_v7_maker_paper_tests tests/test_v7_maker_paper.cpp)
  target_link_libraries(pm_v7_maker_paper_tests PRIVATE pm_v7_hft)
  add_test(NAME pm_v7_maker_paper_tests COMMAND pm_v7_maker_paper_tests)

  add_executable(pm_v7_maker_inventory_tests tests/test_v7_maker_inventory.cpp)
  target_link_libraries(pm_v7_maker_inventory_tests PRIVATE pm_v7_hft)
  add_test(NAME pm_v7_maker_inventory_tests COMMAND pm_v7_maker_inventory_tests)

  add_executable(pm_v7_maker_execution_policy_tests tests/test_v7_maker_execution_policy.cpp)
  target_link_libraries(pm_v7_maker_execution_policy_tests PRIVATE pm_v7_hft)
  add_test(NAME pm_v7_maker_execution_policy_tests COMMAND pm_v7_maker_execution_policy_tests)

  add_executable(pm_v7_external_fair_tests tests/test_v7_external_fair.cpp)
  target_link_libraries(pm_v7_external_fair_tests PRIVATE pm_v7_external)
  add_test(NAME pm_v7_external_fair_tests COMMAND pm_v7_external_fair_tests)

  add_executable(pm_v7_external_state_tests tests/test_v7_external_state.cpp)
  target_link_libraries(pm_v7_external_state_tests PRIVATE pm_v7_external)
  add_test(NAME pm_v7_external_state_tests COMMAND pm_v7_external_state_tests)

  add_executable(pm_v7_external_execution_tests tests/test_v7_external_execution.cpp)
  target_link_libraries(pm_v7_external_execution_tests PRIVATE pm_v7_external)
  add_test(NAME pm_v7_external_execution_tests COMMAND pm_v7_external_execution_tests)

  add_executable(pm_v7_taker_parity_tests tests/test_v7_taker_parity.cpp)
  target_link_libraries(pm_v7_taker_parity_tests PRIVATE pm_v7_external)
  add_test(NAME pm_v7_taker_parity_tests COMMAND pm_v7_taker_parity_tests
    "${CMAKE_CURRENT_SOURCE_DIR}/tests/fixtures/v7_taker_parity.json")

  add_executable(pm_v7_external_kernel_tests tests/test_v7_external_kernel.cpp)
  target_link_libraries(pm_v7_external_kernel_tests PRIVATE pm_v7_external)
  add_test(NAME pm_v7_external_kernel_tests COMMAND pm_v7_external_kernel_tests)

  add_executable(pm_v7_external_protocol_tests tests/test_v7_external_protocol.cpp)
  target_link_libraries(pm_v7_external_protocol_tests PRIVATE pm_v7_external)
  add_test(NAME pm_v7_external_protocol_tests COMMAND pm_v7_external_protocol_tests)

  add_test(NAME pm_v7_feed_race_probe_validate COMMAND polymarket_v7_feed_race_probe --validate-only)
  add_test(NAME pm_v7_feed_race_probe_reduce COMMAND polymarket_v7_feed_race_probe --self-test)
  add_test(NAME pm_v7_feed_race_probe_reject_endpoint COMMAND polymarket_v7_feed_race_probe --endpoint https://clob.polymarket.com/order)
  set_tests_properties(pm_v7_feed_race_probe_reject_endpoint PROPERTIES WILL_FAIL TRUE)

  add_test(NAME pm_v7_ingress_latency_bench_smoke
    COMMAND polymarket_v7_ingress_latency_bench --samples 16 --rounds 1)

  add_executable(pm_v7_ingress_wakeup_tests tests/test_v7_ingress_wakeup.cpp)
  target_link_libraries(pm_v7_ingress_wakeup_tests PRIVATE pm_v7_external)
  target_compile_options(pm_v7_ingress_wakeup_tests PRIVATE -UNDEBUG)
  add_test(NAME pm_v7_ingress_wakeup_tests COMMAND pm_v7_ingress_wakeup_tests)
  set_tests_properties(pm_v7_ingress_wakeup_tests PROPERTIES TIMEOUT 30)

  add_executable(pm_v7_external_ingress_tests tests/test_v7_external_ingress.cpp)
  target_link_libraries(pm_v7_external_ingress_tests PRIVATE pm_v7_external)
  add_test(NAME pm_v7_external_ingress_tests COMMAND pm_v7_external_ingress_tests)

  add_executable(pm_v7_latest_json_publisher_tests tests/test_v7_latest_json_publisher.cpp)
  target_link_libraries(pm_v7_latest_json_publisher_tests PRIVATE pm_v7_external)
  add_test(NAME pm_v7_latest_json_publisher_tests COMMAND pm_v7_latest_json_publisher_tests)

  add_executable(pm_v7_external_cancel_publisher_tests tests/test_v7_external_cancel_publisher.cpp)
  target_link_libraries(pm_v7_external_cancel_publisher_tests PRIVATE pm_v7_external)
  target_compile_options(pm_v7_external_cancel_publisher_tests PRIVATE -UNDEBUG)
  add_test(NAME pm_v7_external_cancel_publisher_tests COMMAND pm_v7_external_cancel_publisher_tests)
  add_executable(pm_v7_external_ws_spec_tests tests/test_v7_external_ws_spec.cpp)
  target_link_libraries(pm_v7_external_ws_spec_tests PRIVATE pm_v7_external)
  add_test(NAME pm_v7_external_ws_spec_tests COMMAND pm_v7_external_ws_spec_tests)

  add_executable(pm_v7_external_tape_tests tests/test_v7_external_tape.cpp)
  target_link_libraries(pm_v7_external_tape_tests PRIVATE pm_v7_external)
  add_test(NAME pm_v7_external_tape_tests COMMAND pm_v7_external_tape_tests)

  add_executable(pm_v7_crypto_book_tape_tests tests/test_v7_crypto_book_tape.cpp)
  target_link_libraries(pm_v7_crypto_book_tape_tests PRIVATE pm_v7_external)
  add_test(NAME pm_v7_crypto_book_tape_tests COMMAND pm_v7_crypto_book_tape_tests)

  add_executable(pm_v7_external_replay_tests tests/test_v7_external_replay.cpp)
  target_link_libraries(pm_v7_external_replay_tests PRIVATE pm_v7_external)
  add_test(NAME pm_v7_external_replay_tests COMMAND pm_v7_external_replay_tests)

  add_executable(pm_v7_binance_l2_tests tests/test_v7_binance_l2.cpp)
  target_link_libraries(pm_v7_binance_l2_tests PRIVATE pm_v7_external)
  add_test(NAME pm_v7_binance_l2_tests COMMAND pm_v7_binance_l2_tests)

  add_executable(pm_v7_binance_l2_protocol_tests tests/test_v7_binance_l2_protocol.cpp)
  target_link_libraries(pm_v7_binance_l2_protocol_tests PRIVATE pm_v7_external)
  add_test(NAME pm_v7_binance_l2_protocol_tests COMMAND pm_v7_binance_l2_protocol_tests)

  add_executable(pm_v7_coinbase_l2_tests tests/test_v7_coinbase_l2.cpp)
  target_link_libraries(pm_v7_coinbase_l2_tests PRIVATE pm_v7_external)
  add_test(NAME pm_v7_coinbase_l2_tests COMMAND pm_v7_coinbase_l2_tests)

  add_executable(pm_v7_coinbase_observer_capacity_tests tests/test_v7_coinbase_observer_capacity.cpp)
  target_link_libraries(pm_v7_coinbase_observer_capacity_tests PRIVATE pm_v7_external)
  add_test(NAME pm_v7_coinbase_observer_capacity_tests COMMAND pm_v7_coinbase_observer_capacity_tests)


  add_test(NAME test_v7_profit_native_replay
    COMMAND ${Python3_EXECUTABLE} "${CMAKE_CURRENT_SOURCE_DIR}/tests/integration_v7_profit_replay.py"
      --binary $<TARGET_FILE:polymarket_v7_maker_research_replay>)

  add_test(NAME test_v7_trade_recorder_recovery
    COMMAND ${Python3_EXECUTABLE}
      "${CMAKE_CURRENT_SOURCE_DIR}/tests/integration_v7_trade_recorder_recovery.py"
      --binary $<TARGET_FILE:polymarket_v7_trade_recorder>)

  file(GLOB V7_PY_TESTS CONFIGURE_DEPENDS
    "${CMAKE_CURRENT_SOURCE_DIR}/tests/test_v7_*.py"
    "${CMAKE_CURRENT_SOURCE_DIR}/tests/test_monitoring_v7_*.py")
  foreach(test_file IN LISTS V7_PY_TESTS)
    get_filename_component(test_name "${test_file}" NAME_WE)
    if(NOT test_name STREQUAL "test_v7_authorized_make_cancel_runtime")
      add_test(NAME "${test_name}" COMMAND ${Python3_EXECUTABLE}
        "${CMAKE_CURRENT_SOURCE_DIR}/tests/run_v7_test_file.py" "${test_file}")
    endif()
  endforeach()
  # Production Release remains optimized; test assertions must remain active.
  get_property(V7_TARGETS DIRECTORY PROPERTY BUILDSYSTEM_TARGETS)
  foreach(test_target IN LISTS V7_TARGETS)
    if(test_target MATCHES "_tests$|_driver$")
      target_compile_options(${test_target} PRIVATE
        $<$<CXX_COMPILER_ID:GNU,Clang,AppleClang>:-UNDEBUG>)
    endif()
  endforeach()
endif()
