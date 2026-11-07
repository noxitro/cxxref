# C++ のシンボルの検索(clangd-mcp-server)

このフォルダの C++ は、clangd(language server)で解析した結果を、MCP サーバー `clangd-mcp-server` のツールで引けます。C++ の呼び出し箇所・参照・定義を探すときは、ソースを grep する前に、まず clangd-mcp-server を使ってください。

以下は clangd-mcp-server の作者が README で推奨している指示です(そのまま写しました)。

## C++ Code Navigation

This project uses the clangd MCP server for C++ code intelligence. Use these tools for:
- Finding definitions and references
- Getting type information
- Searching symbols
- Finding implementations
- Getting diagnostics
