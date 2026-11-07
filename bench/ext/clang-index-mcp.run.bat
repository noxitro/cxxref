@echo off
rem Launcher for clang_index_mcp (andreymedv) over stdio. Keeps cache and session.json under this folder.
set "STATE=%~dp0"
set "TOOL=E:\dev\bench-data\cxxref\tools\clang_index_mcp"
set "LIBCLANG_PATH=E:\tools\llvm-23.1.4\bin\libclang.dll"
set "MCP_CACHE_BASE_DIR=%STATE%cache"
set "PATH=E:\tools\llvm-23.1.4\bin;%PATH%"
cd /d "%STATE%"
"%TOOL%\venv\Scripts\python.exe" -m clang_index_mcp
