// /cxxref-index の、Copilot の SDK に依らない部分(テストできるように分けた。test は mcp/test/test_plugin.py が node --test で流す)。
import { existsSync, statSync } from "node:fs";
import { dirname, isAbsolute, join, resolve } from "node:path";

// プラグインのルート(plugin.json と scripts\index-project.ps1 のあるフォルダ)。start から親へさかのぼる
export function pluginRoot(start) {
  let d = start;
  for (let i = 0; i < 6; i++) {
    if (existsSync(join(d, "plugin.json")) && existsSync(join(d, "scripts", "index-project.ps1"))) return d;
    d = dirname(d);
  }
  throw new Error("cxxref のプラグインのフォルダが見つかりません");
}

// ltidx.exe を MCP サーバー(mcp\autoupdate.py の find_ltidx)と同じ順に探す。{ path } か、
// 環境変数 LTIDX が無いファイルを指すときは { error }(取ってきても直らないので、取ってこない)、どこにも無ければ {}
export function findLtidx(plugin, env = process.env) {
  if (env.LTIDX) {
    return existsSync(env.LTIDX) ? { path: env.LTIDX } : { error: `環境変数 LTIDX の ltidx.exe がありません: ${env.LTIDX}` };
  }
  const cands = [join(plugin, "indexer", "build", "ltidx.exe")];
  if (env.LOCALAPPDATA) cands.push(join(env.LOCALAPPDATA, "cxxref", "ltidx", "ltidx.exe"));  // 無ければ探さない(相対パスにしない)
  for (const p of cands) {
    if (existsSync(p)) return { path: p };
  }
  return {};
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
// (E:\src\Nox\.index が E:\src\Nox.index と出る)。その \ を二重にする。error の表示はそのまま出るので通さない
export const escapeMarkdown = (s) => s.replace(/\\(?=[!-/:-@[-`{-~])/g, "\\\\");

// 索引を作るルート。引数(引用符は外す。相対なら作業フォルダから)、無ければ indexRoot(cwd)。{ root } か { error }
export function targetRoot(arg, cwd) {
  const a = (arg ?? "").trim().replace(/^"(.*)"$/, "$1");
  const root = a ? (isAbsolute(a) ? a : resolve(cwd, a)) : indexRoot(cwd);
  if (!existsSync(root) || !statSync(root).isDirectory()) return { error: `フォルダがありません: ${root}` };
  return { root };
}
