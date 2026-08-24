cmake_minimum_required(VERSION 3.20)
project(polymarket_quant_engine VERSION 0.7.0 LANGUAGES CXX)
set(CMAKE_CXX_STANDARD 20)
set(CMAKE_CXX_STANDARD_REQUIRED ON)
set(CMAKE_CXX_EXTENSIONS OFF)

if(POLICY CMP0167)
    cmake_policy(SET CMP0167 NEW)
endif()

find_package(PkgConfig REQUIRED)
find_package(Boost REQUIRED)
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

include(CTest)
if(BUILD_TESTING)
    find_package(Python3 REQUIRED COMPONENTS Interpreter)
    add_executable(pm_tests tests/test_core.cpp)
    target_link_libraries(pm_tests PRIVATE pm_core)
    add_executable(pm_execution_tests tests/test_execution.cpp)
    target_link_libraries(pm_execution_tests PRIVATE pm_core)
    add_test(NAME pm_tests COMMAND pm_tests)
    add_test(NAME pm_execution_tests COMMAND pm_execution_tests)
    add_test(NAME v4_research_tests COMMAND ${Python3_EXECUTABLE} ${CMAKE_CURRENT_SOURCE_DIR}/tests/test_v4_research.py)
    add_test(NAME runtime_supervisor_tests COMMAND ${Python3_EXECUTABLE} ${CMAKE_CURRENT_SOURCE_DIR}/tests/test_runtime_supervisor.py)
    add_test(NAME mock_integration COMMAND bash ${CMAKE_CURRENT_SOURCE_DIR}/tests/integration_mock.sh $<TARGET_FILE:polymarket_engine>)
endif()
