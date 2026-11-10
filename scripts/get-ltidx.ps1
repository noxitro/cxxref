# ltidx の配布物を GitHub のリリースから取ってきて、%LOCALAPPDATA%\cxxref\ltidx に置く(LLVM と Visual Studio での
# ビルドが要らない)。版は、このリポジトリ(プラグイン)の plugin.json の version。sha256 を確かめてから置く。
# MCP サーバー(mcp\autoupdate.py)と index-project は、ビルドした ltidx.exe が無ければこの場所のものを使う。
[CmdletBinding()]  # 知らない引数をエラーにする(黙って捨てない)
param(
    [string]$Version,                                           # 省くと plugin.json の version
    [string]$Zip,                                               # 取ってこずに手元の zip を使う(隣に .sha256 があれば確かめる)
    [string]$Dest = (Join-Path $env:LOCALAPPDATA 'cxxref\ltidx'),
    [string]$Repo = 'noxitro/cxxref'
)
$ErrorActionPreference = 'Stop'
# 出力をファイルや別のプロセスへ流すとき(/cxxref-index のログ・MCP サーバーの update.log)は、UTF-8 で色を付けずに書く
# (既定では OEM のコードページ(日本語なら cp932)になり、エラーに色のエスケープが混じる)
if ([Console]::IsOutputRedirected) { [Console]::OutputEncoding = [Text.UTF8Encoding]::new($false); $PSStyle.OutputRendering = 'PlainText' }
$name = 'cxxref-ltidx-win-x64'
if (-not $Version) { $Version = (Get-Content -Raw -LiteralPath (Join-Path $PSScriptRoot '..\plugin.json') | ConvertFrom-Json).version }
$tmp = Join-Path ([IO.Path]::GetTempPath()) ("cxxref-ltidx-" + [Guid]::NewGuid().ToString('N'))
New-Item -ItemType Directory $tmp | Out-Null
try {
    if ($Zip) {
        $file = (Resolve-Path -LiteralPath $Zip).Path
        $sumFile = if (Test-Path -LiteralPath "$file.sha256") { "$file.sha256" } else { $null }
    } else {
        $url = "https://github.com/$Repo/releases/download/v$Version/$name.zip"
        "取ってきます: $url"
        $file = Join-Path $tmp "$name.zip"
        $sumFile = "$file.sha256"
        Invoke-WebRequest -Uri $url -OutFile $file -UseBasicParsing
        Invoke-WebRequest -Uri "$url.sha256" -OutFile $sumFile -UseBasicParsing
    }
    if ($sumFile) {
        $want = ((Get-Content -Raw -LiteralPath $sumFile).Trim() -split '\s+')[0].ToLowerInvariant()
        $got = (Get-FileHash -LiteralPath $file -Algorithm SHA256).Hash.ToLowerInvariant()
        if ($want -ne $got) { throw "sha256 が合いません(期待 $want、実際 $got): $file" }
    } elseif (-not $Zip) {
        throw 'sha256 のファイルがありません'
    }
    # 展開してから差し替える(途中で失敗しても前のものを残す)。MCP サーバーの作り直しが ltidx.exe を使っている間は差し替えられない
    $new = "$Dest.new"
    if (Test-Path -LiteralPath $new) { Remove-Item -LiteralPath $new -Recurse -Force }
    Expand-Archive -LiteralPath $file -DestinationPath $new
    if (-not (Test-Path -LiteralPath (Join-Path $new 'ltidx.exe'))) { throw "zip に ltidx.exe がありません: $file" }
    $old = "$Dest.old"
    if (Test-Path -LiteralPath $old) { Remove-Item -LiteralPath $old -Recurse -Force }
    if (Test-Path -LiteralPath $Dest) {
        try { Rename-Item -LiteralPath $Dest -NewName (Split-Path $old -Leaf) }
        catch { throw "今の ltidx を差し替えられません(使用中かもしれません。Copilot CLI を終えてからやり直してください): $Dest" }
    }
    New-Item -ItemType Directory (Split-Path $Dest) -Force | Out-Null
    try { Rename-Item -LiteralPath $new -NewName (Split-Path $Dest -Leaf) }
    catch {
        # 新しいものを置けなかったら前のものに戻す(ltidx の無い状態で終えない)
        if ((Test-Path -LiteralPath $old) -and -not (Test-Path -LiteralPath $Dest)) {
            Rename-Item -LiteralPath $old -NewName (Split-Path $Dest -Leaf) -ErrorAction SilentlyContinue
        }
        throw
    }
    if (Test-Path -LiteralPath $old) { Remove-Item -LiteralPath $old -Recurse -Force -ErrorAction SilentlyContinue }
    # 版の記録の無い古い配布物には、取ってきた版を書いておく(/cxxref-index と MCP サーバーが、プラグインの版と比べる)
    if (-not (Test-Path -LiteralPath (Join-Path $Dest 'version.txt'))) {
        [IO.File]::WriteAllText((Join-Path $Dest 'version.txt'), "$Version`n")
    }
    $v = (Get-Content -Raw -LiteralPath (Join-Path $Dest 'version.txt') -ErrorAction SilentlyContinue)
    "ltidx を置きました: $(Join-Path $Dest 'ltidx.exe')(版 $("$v".Trim()))"
} finally {
    Remove-Item -LiteralPath $tmp -Recurse -Force -ErrorAction SilentlyContinue
}
