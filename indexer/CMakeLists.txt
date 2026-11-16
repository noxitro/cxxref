cmake_minimum_required(VERSION 3.20)
project(ltidx C CXX)

# 公式配布物は zlib / zstd を同梱しない(build_llvm_release.bat が別途ビルドして静的リンクする)。
# 圧縮機能は使わないので、空のターゲットにして zstub.c で代替する。
add_library(ZLIB::ZLIB INTERFACE IMPORTED)
add_library(zstd::libzstd_static INTERFACE IMPORTED)

find_package(Clang REQUIRED CONFIG)

set(CMAKE_CXX_STANDARD 17)
set(CMAKE_MSVC_RUNTIME_LIBRARY MultiThreaded)  # 配布物の LLVM に合わせる

add_executable(ltidx ltidx.cpp codepage.cpp zstub.c)
target_include_directories(ltidx PRIVATE ${LLVM_INCLUDE_DIRS} ${CLANG_INCLUDE_DIRS})
target_compile_definitions(ltidx PRIVATE ${LLVM_DEFINITIONS}
  LTIDX_RESOURCE_DIR="${LLVM_LIBRARY_DIR}/clang/${LLVM_VERSION_MAJOR}")
# /Zc:preprocessor: clang のオプション表(clang/Options/Options.inc)の __VA_ARGS__ を、MSVC の古いプリプロセッサは展開できない
target_compile_options(ltidx PRIVATE /utf-8
  $<$<COMPILE_LANGUAGE:CXX>:/GR- /Zc:preprocessor /wd4141 /wd4146 /wd4244 /wd4267 /wd4291 /wd4624 /wd4805>)
target_link_libraries(ltidx PRIVATE clangTooling clangIndex clangFrontend clangDriver clangAST clangBasic)
