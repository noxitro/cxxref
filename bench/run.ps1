# 条件 A(rg のみ)/ B(LSP)/ C(索引)/ D(LSP + 指示)/ E(ltq CLI)/ F(MCP)/ G(MCP + 指示)/
# H(G の索引の自動更新を止めたもの)/ P(このリポジトリをプラグインとして読ませる。MCP サーバーと使い方の rule は
# プラグインから。指示のファイルと .mcp.json は置かない)/ S(Serena の MCP。clangd を使う。指示なし)/
# Y(S + Serena を先に使う指示 serena-instructions.md)で
# Copilot CLI に 1 問を解かせ、セッションを記録する。
# 問題に edit があれば、実行の前に作業フォルダのソースを書き換え、実行の後に中身と更新日時を元に戻す
# (コードを変えた後の問い合わせ。索引は変える前のソースで作ったもの)。
# Copilot に渡すのは読み取り専用のツール(view / rg / grep / glob / lsp / MCP)だけ。書き込みは渡さない。
# シェルは条件 E だけで渡し、実行を許すのは ltq コマンドだけにする。
param(
    # I・K・L・M・N は、ほかの MCP サーバー(ext\<名前>.json に起動方法と指示を書いたもの)+ 使い方の指示。比べるために v12 で足した
    [Parameter(Mandatory)][ValidateSet('A', 'B', 'C', 'D', 'E', 'F', 'G', 'H', 'P', 'S', 'Y', 'I', 'K', 'L', 'M', 'N')][string]$Cond,
    [Parameter(Mandatory)][string]$TaskId,
    [int]$Rep = 1,
    [string]$Model = 'gpt-6-luna',
    [string]$Round = 'v1',          # 計測の回(v2: dep 行と指示の改善後、v3: ltq)
    [string]$Index = 'nox.tsv',     # 条件 C・E・F・G で使う索引($IndexDir 配下のファイル名)
    [string]$Workspace = $env:CRI_BENCH_WORKSPACE,  # 解析対象のソースのコピー(.github と .index を書き換える)
    [string]$IndexDir = $env:CRI_BENCH_INDEXDIR,    # ltidx で作った索引の置き場所
    [string]$Cdb = '',              # 条件 F・G・H の自動更新が使う compile_commands.json のフォルダ(既定 <作業フォルダ>\.cdb)
    [string]$Clangd = 'clangd',     # 条件 B・D の clangd
    [string]$Tasks = 'tasks.json',          # 問題と正解(bench 配下。公開しない別のコードベースの問題は local\tasks-<名前>.json に置く)
    [string]$Format = 'answer-format.md',   # 回答の書式(bench 配下。local\ に置いたものも指定できる)
    [string]$Serena = $env:CRI_BENCH_SERENA, # 条件 S: Serena を入れたフォルダ(bin\serena.exe と、設定の home\ がある)
    # 'anthropic' で、Copilot の経路ではなく Anthropic API を直接使う(BYOK。従量課金)。$Model は API のモデル名(claude-haiku-5-5 など)。
    # キーは COPILOT_PROVIDER_API_KEY_COMMAND か COPILOT_PROVIDER_API_KEY を、このプロセスかユーザーの環境変数に置く(ここでは読むだけで表示しない)。
    # どちらも無ければ ~/.copilot/providers.json の提供元 anthropic の apiKey を使う(providers.json のモデルは --model では選べなかった。v12)
    [ValidateSet('', 'anthropic')][string]$Provider = '',
    # 条件 I・K・M・N: ほかのツールを入れたフォルダ(ext\*.json の ${TOOLS})、LLVM(${LLVM})、コードベースの名前(${CB}。索引の置き場の名前に使う)
    [string]$ToolsDir = $env:CRI_BENCH_TOOLS,
    [string]$Llvm = $env:CRI_BENCH_LLVM,
    [string]$Codebase = 'nox',
    [switch]$DryRun                 # 作業フォルダに条件の設定を置いて、その中身を出すだけ(Copilot は呼ばない)
)
$ErrorActionPreference = 'Stop'
$bench = $PSScriptRoot
if (-not $Workspace -or -not (Test-Path $Workspace)) { throw 'set -Workspace (or CRI_BENCH_WORKSPACE)' }
if ($Cond -in 'C', 'E', 'F', 'G', 'H', 'P' -and (-not $IndexDir -or -not (Test-Path "$IndexDir\$Index"))) {
    throw "index not found: $IndexDir\$Index (set -IndexDir or CRI_BENCH_INDEXDIR)"
}
$ws = $Workspace
$ltq = (Resolve-Path (Join-Path $bench '..\query')).Path
$tools = 'view', 'rg', 'grep', 'glob', 'lsp'
$allow = $tools

$name = if ($Round -eq 'v1') { "$TaskId-$Cond-r$Rep" } else { "$Round-$TaskId-$Cond-r$Rep" }
New-Item -ItemType Directory "$bench\runs" -Force | Out-Null

# 公式の設定の形(.github/lsp.json の lspServers)のまま、command だけを記録用の中継(lsp_log_proxy.py)にする。
# 中継はメッセージをそのまま通し、やり取りを runs\<名前>.lsp.log に書く(サーバーが動いていたかを後から確かめる)
function Write-LspJson {
    $j = Get-Content "$bench\lsp.json" -Raw -Encoding utf8 | ConvertFrom-Json
    $lspLog = "$bench\runs\$name.lsp.log"
    if (Test-Path $lspLog) { Remove-Item $lspLog }
    $j.lspServers.clangd.args = @('-I', "$bench\lsp_log_proxy.py", $lspLog, $Clangd) + $j.lspServers.clangd.args
    $j.lspServers.clangd.command = 'python'
    $j | ConvertTo-Json -Depth 5 | Set-Content -Encoding utf8 "$ws\.github\lsp.json"
}

# 条件ごとの設定を作業フォルダに置く(他の条件の物は消す)。ルートの compile_commands.json は、条件 S・Y が置いたもの
# (目印 .cri-bench-cdb がある)だけを消す。写しに元からあるものは解析対象の一部なので残す
$cdbMark = "$ws\.cri-bench-cdb"
# 条件 F・G・H・P が写した索引は、消さずに .index-keep へ退避して次の回に使い回す(UE5 では 54 GB あり、毎回は写せない)。
# 使い回すのは、元の索引と大きさ・更新日時が同じで、写しも写した時のままのときだけ(自動更新が作り直したら写し直す)
$keep = "$ws\.index-keep"
if (Test-Path "$ws\.index\keep.txt") {
    if (Test-Path $keep) { Remove-Item -Recurse -Force $keep }
    Move-Item "$ws\.index" $keep
}
foreach ($p in "$ws\.github", "$ws\.index", "$ws\.mcp.json") { if (Test-Path $p) { Remove-Item -Recurse -Force $p } }
if (Test-Path $cdbMark) { Remove-Item -Force "$ws\compile_commands.json", $cdbMark -ErrorAction SilentlyContinue }
switch ($Cond) {
    { $_ -in 'I', 'K', 'L', 'M', 'N' } {
        # ext\<名前>.json の起動方法を、作業フォルダの .mcp.json に書く(${...} を置き換える)。使い方の指示も置く。シェルは渡さない
        $ext = Get-ChildItem "$bench\ext\*.json" | ForEach-Object { Get-Content $_ -Raw -Encoding utf8 | ConvertFrom-Json } |
            Where-Object cond -eq $Cond
        if (-not $ext) { throw "ext\*.json に条件 $Cond がない" }
        if (-not $ToolsDir -or -not (Test-Path $ToolsDir)) { throw 'set -ToolsDir (or CRI_BENCH_TOOLS)' }
        if (-not $Llvm -or -not (Test-Path $Llvm)) { throw 'set -Llvm (or CRI_BENCH_LLVM)' }
        $vars = @{ TOOLS = $ToolsDir; LLVM = $Llvm; WS = (Resolve-Path $ws).Path; CB = $Codebase
            JOBS = [Math]::Max(1, [int]([Environment]::ProcessorCount / 2)) }
        # コードベースごとの値(clangd を起こすために開くファイル、索引の置き場など)は ext の vars.<名前> に書く
        if ($ext.vars -and $ext.vars.$Codebase) { foreach ($p in $ext.vars.$Codebase.PSObject.Properties) { $vars[$p.Name] = $p.Value } }
        $expand = { param($s) [regex]::Replace($s, '\$\{(\w+)\}', { param($m) $vars[$m.Groups[1].Value] }) }
        $envMap = [ordered]@{}
        foreach ($p in $ext.env.PSObject.Properties) { $envMap[$p.Name] = & $expand $p.Value }
        @{ mcpServers = @{ $ext.server = [ordered]@{ type = 'local'; command = (& $expand $ext.command)
                    args = @($ext.args | ForEach-Object { & $expand $_ }); env = $envMap; tools = @($ext.tools) } } } |
            ConvertTo-Json -Depth 6 | Set-Content -Encoding utf8 "$ws\.mcp.json"
        New-Item -ItemType Directory "$ws\.github" | Out-Null
        Copy-Item "$bench\ext\$($ext.instructions)" "$ws\.github\copilot-instructions.md"
        $tools = 'view', 'rg', 'grep', 'glob', $ext.server
        $allow = $tools
    }
    { $_ -in 'S', 'Y' } {
        # Serena(既定の REPL の形)を作業フォルダの .mcp.json に登録する。clangd はルートの compile_commands.json を読むので、
        # 実行の間だけ置く(写しの指紋に入るので、実行の後に消す)。Serena と clangd の索引(.serena・.cache)は消さずに使い回す
        if (-not $Serena -or -not (Test-Path "$Serena\bin\serena.exe")) { throw 'set -Serena (or CRI_BENCH_SERENA)' }
        if (-not (Test-Path "$ws\compile_commands.json")) {
            Copy-Item "$ws\.cdb\compile_commands.json" "$ws\compile_commands.json"
            New-Item -ItemType File $cdbMark | Out-Null
        }
        if ($Cond -eq 'Y') {
            New-Item -ItemType Directory "$ws\.github" | Out-Null
            Copy-Item "$bench\serena-instructions.md" "$ws\.github\copilot-instructions.md"
        }
        $sargs = @('start-mcp-server', '--project', (Resolve-Path $ws).Path, '--context', 'copilot-cli',
            '--enable-web-dashboard', 'False', '--open-web-dashboard', 'False', '--log-level', 'WARNING')
        @{ mcpServers = @{ serena = @{ type = 'local'; command = "$Serena\bin\serena.exe"; args = $sargs; tools = @('*')
                    env = @{ SERENA_HOME = "$Serena\home" } } } } |
            ConvertTo-Json -Depth 5 | Set-Content -Encoding utf8 "$ws\.mcp.json"
        $tools = 'view', 'rg', 'grep', 'glob', 'serena'
        $allow = $tools
    }
    'B' {
        New-Item -ItemType Directory "$ws\.github" | Out-Null
        Write-LspJson
    }
    'C' {
        New-Item -ItemType Directory "$ws\.github", "$ws\.index" | Out-Null
        Copy-Item "$bench\index-instructions.md" "$ws\.github\copilot-instructions.md"
        Copy-Item "$IndexDir\$Index" "$ws\.index\nox.tsv"
        Copy-Item "$IndexDir\$Index.tu.tsv" "$ws\.index\nox.tsv.tu.tsv"
        # コピー直後は読めないことがあった(T4-C-r1 で rg が一致なし)。一度読み切ってから始める
        Get-FileHash "$ws\.index\nox.tsv" | Out-Null
    }
    'D' {
        # B に「LSP を使え」という指示を足した条件(C と指示の有無をそろえる)
        New-Item -ItemType Directory "$ws\.github" | Out-Null
        Write-LspJson
        Copy-Item "$bench\lsp-instructions.md" "$ws\.github\copilot-instructions.md"
    }
    'E' {
        # 段階 2 の CLI。索引(本体・TU・コメントと無効領域)を置き、ltq だけを実行可にする
        New-Item -ItemType Directory "$ws\.github", "$ws\.index" | Out-Null
        Copy-Item "$bench\cli-instructions.md" "$ws\.github\copilot-instructions.md"
        foreach ($s in '', '.tu.tsv', '.skip.tsv') { Copy-Item "$IndexDir\$Index$s" "$ws\.index\nox.tsv$s" }
        Get-FileHash "$ws\.index\nox.tsv" | Out-Null
        $tools = 'view', 'rg', 'grep', 'glob', 'powershell'
        $allow = 'view', 'rg', 'grep', 'glob', 'shell(ltq)', 'shell(ltq:*)'
    }
    { $_ -in 'F', 'G', 'H', 'P' } {
        # 段階 3 の MCP。作業フォルダの .mcp.json に登録する(F は指示なし、G・H は指示あり)。シェルは渡さない
        New-Item -ItemType Directory "$ws\.github" | Out-Null
        if ($Cond -in 'G', 'H') { Copy-Item "$bench\mcp-instructions.md" "$ws\.github\copilot-instructions.md" }
        # 自動更新には .deps.tsv・.own.bin と作り方の記録(.recipe.json)が要る。記録はこの作業フォルダ向けに作る。
        # ハードリンクにはしない(自動更新が索引の時刻を進め、作り直すと、リンク先の元の索引まで変わる)
        $parts = @('', '.tu.tsv', '.skip.tsv', '.deps.tsv', '.own.bin') | Where-Object { Test-Path "$IndexDir\$Index$_" }
        $sig = $Index + '|' + (($parts | ForEach-Object { $f = Get-Item "$IndexDir\$Index$_"; "$_=$($f.Length):$($f.LastWriteTimeUtc.Ticks)" }) -join ';')
        $same = (Test-Path "$keep\keep.txt") -and ((Get-Content "$keep\keep.txt" -Raw).Trim() -eq $sig)
        foreach ($s in $parts) {
            $src = Get-Item "$IndexDir\$Index$s"; $dst = "$keep\index.tsv$s"
            if (-not $same -or -not (Test-Path $dst)) { $same = $false; break }
            $d = Get-Item $dst
            if ($d.Length -ne $src.Length -or $d.LastWriteTimeUtc -ne $src.LastWriteTimeUtc) { $same = $false }
        }
        if ($same) { Move-Item $keep "$ws\.index" }
        else {
            if (Test-Path $keep) { Remove-Item -Recurse -Force $keep }
            New-Item -ItemType Directory "$ws\.index" | Out-Null
            foreach ($s in $parts) { Copy-Item "$IndexDir\$Index$s" "$ws\.index\index.tsv$s" }
            Set-Content -LiteralPath "$ws\.index\keep.txt" -Value $sig -Encoding utf8NoBOM
        }
        $cdbDir = if ($Cdb) { $Cdb } else { "$ws\.cdb" }
        [ordered]@{ version = 1; root = (Resolve-Path $ws).Path; cdb = $cdbDir; out = "$((Resolve-Path $ws).Path)\.index\index.tsv"
            jobs = [Math]::Max(1, [int]([Environment]::ProcessorCount / 2)); makeCdb = $null } |
            ConvertTo-Json | Set-Content -LiteralPath "$ws\.index\index.tsv.recipe.json" -Encoding utf8NoBOM
        # 索引の時刻はコピー元のまま(Copy-Item は更新日時を保つ)。ソースの時刻と比べて古さを決めるので触らない
        Get-FileHash "$ws\.index\index.tsv" | Out-Null
        $server = (Resolve-Path (Join-Path $bench '..\mcp\server.py')).Path
        $serverArgs = @('-I', $server) + $(if ($Cond -eq 'H') { @('--no-auto-update') } else { @() })
        if ($Cond -ne 'P') {
            @{ mcpServers = @{ 'code-ref-index' = @{ type = 'local'; command = 'python'; args = $serverArgs; tools = @('*') } } } |
                ConvertTo-Json -Depth 5 | Set-Content -Encoding utf8 "$ws\.mcp.json"
        }
        $tools = 'view', 'rg', 'grep', 'glob', 'code-ref-index'
        $allow = $tools
    }
}
if ($DryRun) {
    foreach ($p in "$ws\.mcp.json", "$ws\.github\copilot-instructions.md") { if (Test-Path $p) { "== $p"; Get-Content $p -Raw -Encoding utf8 } }
    "tools: $($tools -join ' ')"
    foreach ($p in "$ws\.github", "$ws\.index", "$ws\.mcp.json") { if (Test-Path $p) { Remove-Item -Recurse -Force $p } }
    if (Test-Path $cdbMark) { Remove-Item -Force "$ws\compile_commands.json", $cdbMark -ErrorAction SilentlyContinue }
    return
}
# P はこのリポジトリ(plugin.json のあるルート)を --plugin-dir で読ませる。MCP サーバーは mcp.json の ${PLUGIN_ROOT} から起動される
$pluginArgs = if ($Cond -eq 'P') { @('--plugin-dir', (Resolve-Path (Join-Path $bench '..')).Path) } else { @() }
$oldPath = $env:PATH
if ($Cond -eq 'E') { $env:PATH = "$ltq;$env:PATH" }

$task = (Get-Content "$bench\$Tasks" -Raw -Encoding utf8 | ConvertFrom-Json) | Where-Object id -eq $TaskId
if (-not $task) { throw "unknown task $TaskId" }
$prompt = $task.prompt + "`n`n" + (Get-Content "$bench\$Format" -Raw -Encoding utf8)

# 問題の edit を当てる(行は 1 始まり。insert はその行の前に足す、delete はその行を消す)。終わったら元に戻す
$saved = @()
foreach ($e in @($task.edit | Where-Object { $_ })) {
    $fp = Join-Path $ws $e.file
    $bytes = [IO.File]::ReadAllBytes($fp)
    $saved += , @($fp, $bytes, (Get-Item -LiteralPath $fp).LastWriteTimeUtc)
    $bom = $bytes.Length -ge 3 -and $bytes[0] -eq 0xEF -and $bytes[1] -eq 0xBB -and $bytes[2] -eq 0xBF
    $text = [Text.UTF8Encoding]::new($false).GetString($bytes, [int]$bom * 3, $bytes.Length - [int]$bom * 3)
    $eol = if ($text.Contains("`r`n")) { "`r`n" } else { "`n" }
    $lines = [Collections.Generic.List[string]]::new([string[]]($text -split "`r?`n"))
    if ($e.delete) { $lines.RemoveAt($e.line - 1) } else { $lines.Insert($e.line - 1, [string]$e.insert) }
    [IO.File]::WriteAllText($fp, ($lines -join $eol), [Text.UTF8Encoding]::new($bom))
}

# BYOK の環境変数はこの実行の間だけ置き、終わったら元に戻す(ふだんの Copilot の経路に残さない)
$providerVars = 'COPILOT_PROVIDER_TYPE', 'COPILOT_PROVIDER_BASE_URL', 'COPILOT_PROVIDER_API_KEY', 'COPILOT_PROVIDER_API_KEY_COMMAND',
'COPILOT_MODEL', 'COPILOT_PROVIDER_MODEL_ID', 'COPILOT_PROVIDER_WIRE_MODEL'
$oldProvider = @{}
foreach ($v in $providerVars) { $oldProvider[$v] = [Environment]::GetEnvironmentVariable($v, 'Process') }
$modelArgs = @('--model', $Model)
if ($Provider -eq 'anthropic') {
    foreach ($v in 'COPILOT_PROVIDER_API_KEY_COMMAND', 'COPILOT_PROVIDER_API_KEY') {
        if (-not [Environment]::GetEnvironmentVariable($v, 'Process')) {
            $u = [Environment]::GetEnvironmentVariable($v, 'User')
            if ($u) { [Environment]::SetEnvironmentVariable($v, $u, 'Process') }
        }
    }
    if (-not ($env:COPILOT_PROVIDER_API_KEY_COMMAND -or $env:COPILOT_PROVIDER_API_KEY)) {
        # どちらも無ければ、Copilot CLI の providers.json の提供元 anthropic の apiKey を、要求のたびに provider_key.py で読む
        $env:COPILOT_PROVIDER_API_KEY_COMMAND = "python -I `"$bench\provider_key.py`" anthropic"
    }
    $env:COPILOT_PROVIDER_TYPE = 'anthropic'
    $env:COPILOT_PROVIDER_BASE_URL = 'https://api.anthropic.com'
    # API の名前(claude-haiku-5-5)をそのまま送り、Copilot の既知のモデル名(claude-haiku-5.5)で上限などの設定を引かせる
    $env:COPILOT_PROVIDER_WIRE_MODEL = $Model
    $env:COPILOT_PROVIDER_MODEL_ID = $Model -replace '-(\d+)-(\d+)$', '-$1.$2'
    $env:COPILOT_MODEL = $env:COPILOT_PROVIDER_MODEL_ID
    $modelArgs = @()
}

$t0 = Get-Date
Push-Location $ws
try {
    copilot -p $prompt @modelArgs --trust-folder --available-tools @tools --allow-tool @allow -s @pluginArgs `
        --share "$bench\runs\$name.md" --log-level error *> "$bench\runs\$name.out.txt"
    $code = $LASTEXITCODE
} finally {
    Pop-Location
    $env:PATH = $oldPath
    foreach ($v in $providerVars) { [Environment]::SetEnvironmentVariable($v, $oldProvider[$v], 'Process') }
    # 裏の作り直し(自動更新)が続いていれば終わるまで待つ。次の実行が .index を消すとぶつかる
    $deadline = (Get-Date).AddMinutes(10)
    while ((Test-Path "$ws\.index\update.lock") -and (Get-Date) -lt $deadline) {
        try { Remove-Item "$ws\.index\update.lock" -ErrorAction Stop } catch { Start-Sleep -Milliseconds 500 }
    }
    if (Test-Path $cdbMark) { Remove-Item -Force "$ws\compile_commands.json", $cdbMark -ErrorAction SilentlyContinue }
    [array]::Reverse($saved)  # 同じファイルを何度も書き換えたときは、最初の中身に戻す
    foreach ($x in $saved) {
        [IO.File]::WriteAllBytes($x[0], $x[1])
        (Get-Item -LiteralPath $x[0]).LastWriteTimeUtc = $x[2]
    }
}
$sec = [int]((Get-Date) - $t0).TotalSeconds
# --trust-folder は --session-id と併用できないので、実行後にセッションを引く。ほかの作業の Copilot のセッションと
# 取り違えないよう、開始の記録(session.start)の作業フォルダがこの作業フォルダで、この実行の後に始まったものを選ぶ
$wsFull = (Resolve-Path $ws).Path.TrimEnd('\')
$sid = Get-ChildItem "$env:USERPROFILE\.copilot\session-state" -Directory | Where-Object LastWriteTime -ge $t0 |
    Sort-Object LastWriteTime -Descending | Where-Object {
        $first = Get-Content "$($_.FullName)\events.jsonl" -TotalCount 1 -Encoding utf8 -ErrorAction SilentlyContinue
        if (-not $first) { return $false }
        $st = ($first | ConvertFrom-Json).data
        $st.context.cwd.TrimEnd('\') -eq $wsFull -and ([datetime]$st.startTime).ToUniversalTime() -ge $t0.ToUniversalTime()
    } | Select-Object -First 1 -ExpandProperty Name
[pscustomobject]@{ name = $name; round = $Round; task = $TaskId; cond = $Cond; rep = $Rep; model = $Model; provider = $Provider; session = $sid; exit = $code; sec = $sec; at = $t0.ToString('s') } |
    ConvertTo-Json -Compress | Add-Content -Encoding utf8 "$bench\runs\index.jsonl"
"$name exit=$code ${sec}s session=$sid"
