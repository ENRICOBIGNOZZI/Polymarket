cmake_minimum_required(VERSION 3.20)
project(polymarket_quant_engine VERSION 0.2.0 LANGUAGES CXX)

set(CMAKE_CXX_STANDARD 20)
set(CMAKE_CXX_STANDARD_REQUIRED ON)
set(CMAKE_CXX_EXTENSIONS OFF)

option(POLY_BUILD_TESTS "Build unit tests" ON)

find_package(Armadillo REQUIRED)
find_package(PkgConfig REQUIRED)
pkg_check_modules(JSONC REQUIRED IMPORTED_TARGET json-c)
pkg_check_modules(LIBCURL REQUIRED IMPORTED_TARGET libcurl)
find_package(Threads REQUIRED)

add_library(polycore
    src/model_factor.cpp
    src/model_graph.cpp
    src/model_arb.cpp
    src/risk.cpp
    src/paper_broker.cpp
)
target_include_directories(polycore PUBLIC include ${ARMADILLO_INCLUDE_DIRS})
target_link_libraries(polycore PUBLIC ${ARMADILLO_LIBRARIES})
target_compile_options(polycore PRIVATE -Wall -Wextra -Wpedantic)

add_executable(poly-engine
    src/main.cpp
    src/engine_core.cpp
    src/server.cpp
    src/polymarket_client.cpp
)
target_include_directories(poly-engine PRIVATE include)
target_link_libraries(poly-engine PRIVATE polycore PkgConfig::LIBCURL PkgConfig::JSONC Threads::Threads)
target_compile_options(poly-engine PRIVATE -Wall -Wextra -Wpedantic)

if(POLY_BUILD_TESTS)
    enable_testing()
    add_executable(poly-tests tests/test_core.cpp)
    target_include_directories(poly-tests PRIVATE include)
    target_link_libraries(poly-tests PRIVATE polycore)
    add_test(NAME poly-tests COMMAND poly-tests)
endif()
