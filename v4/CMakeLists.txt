cmake_minimum_required(VERSION 3.20)
project(polymarket_v4 VERSION 1.0.0 LANGUAGES CXX)

set(CMAKE_CXX_STANDARD 20)
set(CMAKE_CXX_STANDARD_REQUIRED ON)
set(CMAKE_CXX_EXTENSIONS OFF)

set(PM_ROOT "${CMAKE_CURRENT_LIST_DIR}/..")

find_package(PkgConfig REQUIRED)
pkg_check_modules(LIBCURL REQUIRED IMPORTED_TARGET libcurl)

add_library(pm_v4_core
    "${PM_ROOT}/src/http.cpp"
    "${PM_ROOT}/src/api.cpp"
    "${PM_ROOT}/src/boost_json.cpp"
    "${PM_ROOT}/src/v4.cpp"
)
target_include_directories(pm_v4_core PUBLIC "${PM_ROOT}/include")
target_link_libraries(pm_v4_core PUBLIC PkgConfig::LIBCURL)
target_compile_options(pm_v4_core PRIVATE -Wall -Wextra -Wpedantic -Werror)

add_executable(polymarket_v4 "${PM_ROOT}/src/v4_main.cpp")
target_link_libraries(polymarket_v4 PRIVATE pm_v4_core)

add_executable(polymarket_v4_replay "${PM_ROOT}/src/v4_replay_main.cpp")
target_link_libraries(polymarket_v4_replay PRIVATE pm_v4_core)

include(CTest)
if(BUILD_TESTING)
    add_executable(pm_v4_tests "${PM_ROOT}/tests/test_v4.cpp")
    target_link_libraries(pm_v4_tests PRIVATE pm_v4_core)
    target_compile_options(pm_v4_tests PRIVATE -Wall -Wextra -Wpedantic -Werror)
    add_test(NAME pm_v4_tests COMMAND pm_v4_tests)
endif()
