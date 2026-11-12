// /cxxref-index・/cxxref-enable の、Copilot の SDK に依らない部分(テストできるように分けた。test は mcp/test/test_plugin.py が node --test で流す)。
import { execFileSync } from "node:child_process";
import { existsSync, readFileSync, realpathSync, statSync } from "node:fs";
import { dirname, isAbsolute, join, relative, resolve, sep, win32 } from "node:path";

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

// パスを比べられる形にする(\\?\ を外す・/ を \ に・末尾の \ を外す・小文字に)。在る場所なら、ジャンクション・
// シンボリック リンク・8.3 の短い名前・subst のドライブも実体に揃える(無い場所は文字のまま比べる)。空なら ""
export function pathKey(p) {
  return realOrSelf(p).replace(/\//g, "\\").replace(/\\+$/, "").toLowerCase();
}

// \\?\ を外して、在る場所なら実体のパス(区切りは OS のまま。Linux の CI でも realpath が効くように、区切りは替えない)
function realOrSelf(p) {
  const plain = (s) => s.replace(/^\\\\\?\\UNC\\/i, "\\\\").replace(/^\\\\\?\\/, "");
  const s = plain(String(p ?? ""));
  if (!s) return "";
  try { return plain(realpathSync.native(s)); } catch { return s; }  // 無い場所は文字のまま
}

// CLI の配布物のフォルダか(npm で入れたもの・%LOCALAPPDATA%\copilot\pkg のもの、どちらにも npm-loader.js がある。
// アプリのフォルダには無い)
const looksLikeCliDist = (dir) => existsSync(join(dir, "npm-loader.js"));

// 拡張を動かしているのが Copilot CLI(画面に session.log を出す)か、GitHub Copilot app(info と warning を出さず、
// error だけを出す)か。どちらも COPILOT_CLI_PATH などを渡すので、それでは分からない。違うのは拡張に渡す SDK の場所
// (COPILOT_SDK_PATH)で、CLI は自分の配布物の中のものを、アプリは自分で持っているものを渡す。
// 配布物の場所は、実際に読み込んだ場所(COPILOT_CLI_RESOLVED_DIST_DIR)と、読めと指定された場所(COPILOT_CLI_DIST_DIR。
// 読めなければ無視される)のどちらかの下にあれば CLI とし、どちらも無いか外れたら、SDK の親が CLI の配布物に見えるかで決める。
// SDK の場所が分からなければアプリとみなす(アプリを CLI と取り違えると何も出ない。逆ならダイアログが余分に出るだけ)
export function runsUnderCli(env, isCliDist = looksLikeCliDist) {
  const sdk = pathKey(env.COPILOT_SDK_PATH);
  if (!sdk) return false;
  const dists = [env.COPILOT_CLI_RESOLVED_DIST_DIR, env.COPILOT_CLI_DIST_DIR].map(pathKey).filter(Boolean);
  // 親は win32.dirname で取る(\ と / のどちらの区切りも分けられる。在る場所なら区切りは OS のまま残る)
  return dists.some((d) => sdk === d || sdk.startsWith(`${d}\\`)) || isCliDist(win32.dirname(realOrSelf(env.COPILOT_SDK_PATH)));
}

// 結果をダイアログにも出すか。CXXREF_DISPLAY が timeline なら出さない、dialog なら出す、
// それ以外(auto)はアプリで動いているときだけ出す
export function wantDialog(env, app = !runsUnderCli(env)) {
  const mode = env.CXXREF_DISPLAY;
  if (mode === "timeline") return false;
  if (mode === "dialog") return true;
  return app;
}

// 利用者に見せる文の出し方。getSession は拡張のセッション(joinSession の戻り値)を返す関数、env は process.env。
// { tell(文, level), say(文), settled() } を返す。
// - タイムライン(session.log): level の無い文は Markdown として表示されるので escapeMarkdown を通す。error はそのまま出る
// - ダイアログ: wantDialog のとき、tell の文のうち error でないものをダイアログにも出す(error はアプリでも表示される)。
//   say(進み具合)はダイアログにしない。ダイアログは 1 つずつ出す(前のを閉じてから次を出す)。待たないので、コマンドは先へ進む。
//   アプリのダイアログも Markdown として表示されるので escapeMarkdown を通す(CLI のダイアログでの表示は確かめていないので、そのまま)
// - アプリでダイアログを出せない(ホストが対応していない・出すのに失敗した)ときは、アプリでも表示される error で出し直す
// settled() は、出しかけのダイアログがすべて閉じたら解決する(別のダイアログを出す前に待つ)
export function makeNotifier(getSession, env) {
  const app = !runsUnderCli(env);
  const useDialog = wantDialog(env, app);
  let queue = Promise.resolve();
  const log = (message, level) =>
    getSession().log(level ? message : escapeMarkdown(message), { ephemeral: true, ...(level ? { level } : {}) });
  const show = async (message) => {
    const session = getSession();
    let why = "このホストはダイアログを出せません";
    if (session.capabilities?.ui?.elicitation) {
      try {
        await session.ui.elicitation({ message: app ? escapeMarkdown(message) : message, requestedSchema: { type: "object", properties: {} } });
        return;
      } catch (err) {
        why = `ダイアログを出せませんでした: ${err?.message ?? err}`;
      }
    }
    if (app) await log(`${message}\n(${why})`, "error");
  };
  return {
    say: (message) => log(message),
    tell: (message, level) => {
      if (useDialog && level !== "error") queue = queue.then(() => show(message)).catch(() => { });
      return log(message, level);
    },
    settled: () => queue,
  };
}

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
