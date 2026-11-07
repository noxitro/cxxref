# C++ のシンボルの検索(mcp-language-server)

このフォルダの C++ は、clangd(language server)で解析した結果を、MCP サーバー `mcp-language-server` のツールで引けます。C++ の呼び出し箇所・参照・定義を探すときは、ソースを grep する前に、まず mcp-language-server を使ってください。

(mcp-language-server の README には、エージェント向けの推奨の指示がありません。上は Serena の条件と同じ書き方にそろえたものです)
