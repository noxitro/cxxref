# 問題 × 条件 × 回数をまとめて run.ps1 で流す。runs\index.jsonl に終わった(exit 0 の)回があれば飛ばすので、
# 止まっても同じコマンドで続きから流せる。Copilot CLI のセッションは実行後に最新のものを引くので、並列には流さない。
#   pwsh -File batch.ps1 -Round v6 -Tasks local\tasks-a.json -TaskIds T1,T2 -Conds A,F,G -Reps 5 -Workspace <写し> -IndexDir <dir> -Index <索引.tsv>
param(
    [Parameter(Mandatory)][string]$Round,
    [Parameter(Mandatory)][string[]]$TaskIds,
    [Parameter(Mandatory)][string[]]$Conds,
    [int]$Reps = 5,
    [string]$Tasks = 'local\tasks-a.json',
    [string]$Format = 'answer-format.md',
    [string]$Workspace = $env:CRI_BENCH_WORKSPACE,
    [string]$IndexDir = $env:CRI_BENCH_INDEXDIR,
    [string]$Index = 'index.tsv',
    [string]$Digest = '',           # 写しの指紋(snapshot.py digest)。指定すると、始める前と各回の後に確かめる
    [string]$Clangd = 'clangd',     # 条件 B・D の clangd(run.ps1 に渡す)
    [string]$Model = 'gpt-6-luna',  # run.ps1 に渡す
    [string]$Provider = '',         # run.ps1 に渡す('anthropic' で BYOK)
    [string]$Codebase = 'a'         # run.ps1 に渡す(条件 I・K・M・N の索引の置き場の名前)
)
$ErrorActionPreference = 'Stop'
$bench = $PSScriptRoot
# pwsh -File では -Conds A,F,G が配列にならず 1 つの文字列 "A,F,G" で届くので、カンマで分ける
$TaskIds = @($TaskIds | ForEach-Object { $_ -split ',' } | Where-Object { $_ })
$Conds = @($Conds | ForEach-Object { $_ -split ',' } | Where-Object { $_ })
function Test-Digest {
    if (-not $Digest) { return }
    $d = (& python -I "$bench\snapshot.py" digest $Workspace).Split(' ')[0]
    if ($d -ne $Digest) { throw "写しが変わっています: $d (期待 $Digest)" }
}
$done = @{}
if (Test-Path "$bench\runs\index.jsonl") {
    foreach ($l in Get-Content "$bench\runs\index.jsonl" -Encoding utf8) {
        $r = $l | ConvertFrom-Json
        if ($r.exit -eq 0) { $done[$r.name] = $true }
    }
}
Test-Digest
# 回を外側にして、どこで止まっても条件と問題の間で回数が揃うようにする
for ($rep = 1; $rep -le $Reps; $rep++) {
    foreach ($t in $TaskIds) {
        foreach ($c in $Conds) {
            $name = "$Round-$t-$c-r$rep"
            if ($done[$name]) { continue }
            & "$bench\run.ps1" -Cond $c -TaskId $t -Rep $rep -Round $Round -Tasks $Tasks -Format $Format `
                -Workspace $Workspace -IndexDir $IndexDir -Index $Index -Clangd $Clangd -Model $Model -Provider $Provider -Codebase $Codebase
            Test-Digest
        }
    }
}
