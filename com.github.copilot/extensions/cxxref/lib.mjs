// /cxxref-index・/cxxref-enable の、Copilot の SDK に依らない部分(テストできるように分けた。test は mcp/test/test_plugin.py が node --test で流す)。
import { execFileSync } from "node:child_process";
import { existsSync, readFileSync, statSync } from "node:fs";
import { dirname, isAbsolute, join, relative, resolve, sep } from "node:path";

// プラグインのルート(plugin.json と scripts\index-project.ps1 のあるフォルダ)。start から親へさかのぼる
export function pluginRoot(start) {
  let d = start;
  for (let i = 0; i < 6; i++) {
    if (existsSync(join(d, "plugin.json")) && existsSync(join(d, "scripts", "index-project.ps1"))) return d;
    d = dirname(d);
  }
  throw new Error("cxxref のプラグインのフォルダが見つかりません");
}

// プラグインの版(plugin.json の version)。読めなければ ""
export function pluginVersion(plugin) {
  try {
    return String(JSON.parse(readFileSync(join(plugin, "plugin.json"), "utf8").replace(/^﻿/, "")).version || "");
  } catch {
    return "";
  }
}

// ltidx.exe を MCP サーバー(mcp\autoupdate.py の find_ltidx)と同じ順に探す。{ path } か、
// 環境変数 LTIDX が無いファイルを指すときは { error }(取ってきても直らないので、取ってこない)、どこにも無ければ {}。
// 取ってきたもの(%LOCALAPPDATA%\cxxref\ltidx)の版(隣の version.txt)がプラグインの版と違えば、stale: { have, want } を添える
// (プラグインを更新しても、取ってきた ltidx は入れ替わらない。ビルドしたものと LTIDX のものは版を見ない)
export function findLtidx(plugin, env = process.env) {
  if (env.LTIDX) {
    return existsSync(env.LTIDX) ? { path: env.LTIDX } : { error: `環境変数 LTIDX の ltidx.exe がありません: ${env.LTIDX}` };
  }
  const built = join(plugin, "indexer", "build", "ltidx.exe");
  if (existsSync(built)) return { path: built };
  if (!env.LOCALAPPDATA) return {};  // 無ければ探さない(相対パスにしない)
  const got = join(env.LOCALAPPDATA, "cxxref", "ltidx", "ltidx.exe");
  if (!existsSync(got)) return {};
  let have = "";
  try { have = readFileSync(join(dirname(got), "version.txt"), "utf8").trim(); } catch { /* 古い配布物には無い */ }
  const want = pluginVersion(plugin);
  return want && have !== want ? { path: got, stale: { have: have || "不明", want } } : { path: got };
}

// /cxxref-index の引数。先頭が full なら、前回の索引があっても最初から作り直す(残りはルート)。
// full という名前のフォルダは ./full と書く
export function parseIndexArgs(arg) {
  const a = (arg ?? "").trim();
  const m = a.match(/^full(?:\s+(.*))?$/i);
  return m ? { full: true, root: (m[1] ?? "").trim() } : { full: false, root: a };
}

// セッションの作業フォルダ(最後の session.start / session.context_changed の cwd)
export function lastCwd(events) {
  let cwd = "";
  for (const e of events) {
    const c = e.data?.context?.cwd ?? (e.type === "session.context_changed" ? e.data?.cwd : undefined);
    if (typeof c === "string" && c) cwd = c;
  }
  return cwd;
}

// 作業フォルダから親へさかのぼって、索引(.index)のあるフォルダ。無ければ作業フォルダ(ltq と同じ探し方)
export function indexRoot(start) {
  for (let d = resolve(start); ; d = dirname(d)) {
    if (existsSync(join(d, ".index"))) return d;
    if (dirname(d) === d) return resolve(start);
  }
}

// ふつうの(level の無い)session.log は Markdown として表示され、\ の後ろが記号だと \ が消える
// (E:\src\Proj\.index が E:\src\Proj.index と出る)。その \ を二重にする。error の表示はそのまま出るので通さない
export const escapeMarkdown = (s) => s.replace(/\\(?=[!-/:-@[-`{-~])/g, "\\\\");

// git のリポジトリのルート(start の上で)。git が無い・リポジトリでなければ ""
export function gitTop(start) {
  try {
    return execFileSync("git", ["-C", start, "rev-parse", "--show-toplevel"], { encoding: "utf8", stdio: ["ignore", "pipe", "ignore"], windowsHide: true })
      .trim().replace(/\//g, sep);
  } catch {
    return "";
  }
}

// /cxxref-enable の引数: [on|off|status] [local|project] [ルート]。{ action, scope, root } か { error }
//   action: Add / Remove / Status(省くと Status)。scope: Local / Project / ""(on で省くと Local、off で省くと両方)
//   root: 残りの文字列(省くと "")
export function parseEnableArgs(arg) {
  let rest = (arg ?? "").trim();
  const take = (words) => {
    const m = rest.match(/^(\S+)(?:\s+|$)/);
    const w = m ? words[m[1].toLowerCase()] : undefined;
    if (w === undefined) return undefined;
    rest = rest.slice(m[0].length);
    return w;
  };
  const action = take({ on: "Add", off: "Remove", status: "Status" }) ?? (rest ? undefined : "Status");
  if (action === undefined) return { error: "使い方: /cxxref-enable on|off|status [local|project] [ルート]" };
  let scope = take({ local: "Local", project: "Project" }) ?? "";
  if (action === "Add" && !scope) scope = "Local";
  if (action === "Status") scope = "";
  return { action, scope, root: rest.trim() };
}

// /cxxref-enable(状態)の出力(setup-instructions.ps1 の Show-State)に添える、次に打つコマンドの案内。読めなければ ""
//   拡張のコマンドには引数のヒントを付けられない(SDK の CommandDefinition は name・description だけ)ので、ここで教える
export function statusHint(out) {
  const project = /^プロジェクト\(.*\): 入っています$/m.test(out);
  const local = /^ローカル\(.*\): 入っています$/m.test(out);
  if (project || local) return "消すには: /cxxref-enable off(入っている方から消す)";
  if (!/^プロジェクト\(.*\): 入っていません$/m.test(out)) return "";
  return "足すには: /cxxref-enable on(自分だけ。git から外す)か /cxxref-enable on project(コミットするとチームの全員)";
}

// 指示を足すルート。引数があれば targetRoot と同じ扱い、無ければ git のルート、それも無ければ作業フォルダ
export function enableRoot(arg, cwd) {
  if ((arg ?? "").trim()) return targetRoot(arg, cwd);
  return { root: gitTop(cwd) || resolve(cwd) };
}

// 前に選んだ compile_commands.json の作り元(index-project が残した作り方の makeCdb.Project)。無い・読めなければ ""
export function savedProject(root) {
  try {
    const r = JSON.parse(readFileSync(join(root, ".index", "index.tsv.recipe.json"), "utf8").replace(/^﻿/, ""));
    const p = r?.makeCdb?.Project;
    return typeof p === "string" && p && existsSync(p) ? p : "";
  } catch {
    return "";
  }
}

// make-cdb.ps1 -ListProjects の出力(1 行に 1 つのフルパス)を、選ぶための { label: ルートからの相対パス, path } にする
export function projectChoices(root, out) {
  return out.split(/\r?\n/).map((s) => s.trim()).filter(Boolean).map((path) => ({ label: relative(root, path) || path, path }));
}

// 索引を作るルート。引数(引用符は外す。相対なら作業フォルダから)、無ければ indexRoot(cwd)。{ root } か { error }
export function targetRoot(arg, cwd) {
  const a = (arg ?? "").trim().replace(/^"(.*)"$/, "$1");
  const root = a ? (isAbsolute(a) ? a : resolve(cwd, a)) : indexRoot(cwd);
  if (!existsSync(root) || !statSync(root).isDirectory()) return { error: `フォルダがありません: ${root}` };
  return { root };
}

// ltidx の進み具合。index-project が .index\index.log に落とす "[n/全体] ok|NG ファイル" の行から、
// { done, total, ng } を返す(並列で解析するので、行の n は前後する。いちばん大きい n を使う)。行が無ければ null
export function parseProgress(text) {
  let done = 0, total = 0, ng = 0;
  for (const m of text.matchAll(/^\[(\d+)\/(\d+)\] (ok|NG)\b/gm)) {
    done = Math.max(done, Number(m[1]));
    total = Number(m[2]);
    if (m[3] === "NG") ng++;
  }
  return total ? { done, total, ng } : null;
}

// 進み具合を画面に出すか決める。snap は { phase: "cdb" }(compile_commands.json を用意している)か
// { phase: "tu", done, total, ng }(ltidx が TU を解析している)。prev は前に出したときの { phase, tenth, at }(初めは {})。
// 出すのは、段階が変わったとき・TU が 1 割進んだとき・compile_commands.json に 1 分以上かかっているとき。
// どれも前に出してから minGap ms 以上あける(行が増えすぎないように)。{ note(出さなければ ""), state(次の prev) }
export function progressNote(prev, snap, now, elapsedSec, minGap = 10000) {
  const quiet = { note: "", state: prev };
  if (snap.phase === "cdb") {
    if (prev.phase !== "cdb") return { note: "cxxref: compile_commands.json を用意しています", state: { phase: "cdb", at: now } };
    if (now - prev.at < 60000) return quiet;
    return { note: `cxxref: compile_commands.json を用意しています(${elapsedSec} 秒)`, state: { phase: "cdb", at: now } };
  }
  const { done, total, ng } = snap;
  const tenth = Math.floor((done * 10) / total);
  if (done >= total) return quiet;  // 終わりは、索引ができたメッセージで知らせる
  if (prev.phase === "tu" && (tenth <= prev.tenth || now - prev.at < minGap)) return quiet;
  const pct = Math.floor((done * 100) / total);
  return {
    note: `cxxref: TU を解析しています ${done}/${total}(${pct}%${ng ? `、解析エラー ${ng}` : ""}、${elapsedSec} 秒)`,
    state: { phase: "tu", tenth, at: now },
  };
}
