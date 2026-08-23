cmake_minimum_required(VERSION 3.20)
project(polymarket_quant_engine VERSION 0.3.0 LANGUAGES CXX)
set(CMAKE_CXX_STANDARD 20)
set(CMAKE_CXX_STANDARD_REQUIRED ON)
set(CMAKE_CXX_EXTENSIONS OFF)

find_package(PkgConfig REQUIRED)
pkg_check_modules(LIBCURL REQUIRED IMPORTED_TARGET libcurl)

add_library(pm_core
    src/http.cpp
    src/api.cpp
    src/event_api.cpp
    src/trade_api.cpp
    src/engine.cpp
    src/maker.cpp
    src/boost_json.cpp
)
target_include_directories(pm_core PUBLIC ${CMAKE_CURRENT_SOURCE_DIR}/include)
target_link_libraries(pm_core PUBLIC PkgConfig::LIBCURL)
target_compile_options(pm_core PRIVATE -Wall -Wextra -Wpedantic)

add_executable(polymarket_engine src/main.cpp)
target_link_libraries(polymarket_engine PRIVATE pm_core)

include(CTest)
if(BUILD_TESTING)
    add_executable(pm_tests tests/test_core.cpp)
    target_link_libraries(pm_tests PRIVATE pm_core)
    add_test(NAME pm_tests COMMAND pm_tests)
    add_test(NAME mock_integration COMMAND bash ${CMAKE_CURRENT_SOURCE_DIR}/tests/integration_mock.sh $<TARGET_FILE:polymarket_engine>)
    add_test(NAME maker_mock_integration COMMAND bash ${CMAKE_CURRENT_SOURCE_DIR}/tests/maker_mock.sh $<TARGET_FILE:polymarket_engine>)
endif()
