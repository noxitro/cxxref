// node --test com.github.copilot/extensions/cxxref/lib.test.mjs(mcp/test/test_plugin.py が流す)。/cxxref-index・/cxxref-enable の SDK に依らない部分
import assert from "node:assert/strict";
import { execFileSync } from "node:child_process";
import { mkdirSync, mkdtempSync, realpathSync, rmSync, symlinkSync, writeFileSync } from "node:fs";
import { tmpdir } from "node:os";
import { join, sep } from "node:path";
import { test } from "node:test";
import { ENABLE_ON_GONE, enableRoot, escapeMarkdown, findLtidx, gitTop, indexRoot, joinOptions, lastCwd, loadSdk, makeNotifier, parseEnableArgs, parseIndexArgs, parseProgress, pathKey, pluginRoot, progressNote, projectChoices, savedProject, runsUnderCli, SDK_NAMES, statusHint, targetRoot, wantDialog } from "./lib.mjs";

const notFound = (name) => Object.assign(new Error(`Cannot find package '${name.split("/").slice(0, 2).join("/")}' imported from x`),
  { code: "ERR_MODULE_NOT_FOUND" });

test("loadSdk: 新しい名前(@microsoft)を先に試す", async () => {
  const tried = [];
  const m = await loadSdk(async (n) => { tried.push(n); return { which: n }; });
  assert.deepEqual(m, { which: "@microsoft/copilot-sdk/extension" });
  assert.deepEqual(tried, ["@microsoft/copilot-sdk/extension"]);
  assert.deepEqual(SDK_NAMES, ["@microsoft/copilot-sdk/extension", "@github/copilot-sdk/extension"]);
});

test("loadSdk: 新しい名前が無ければ、古い名前(@github)を読む", async () => {
  const m = await loadSdk(async (n) => {
    if (n.startsWith("@microsoft/")) throw notFound(n);
    return { which: n };
  });
  assert.deepEqual(m, { which: "@github/copilot-sdk/extension" });
});

test("loadSdk: どちらも無ければ、名前を挙げて止まる", async () => {
  await assert.rejects(loadSdk(async (n) => { throw notFound(n); }),
    /SDK が見つかりません.@microsoft\/copilot-sdk\/extension、@github\/copilot-sdk\/extension./);
});

test("loadSdk: SDK の中の誤り(ほかのパッケージが無いなど)は、次を試さずに投げる", async () => {
  const tried = [];
  const inner = Object.assign(new Error("Cannot find package 'vscode-jsonrpc' imported from sdk"), { code: "ERR_MODULE_NOT_FOUND" });
  await assert.rejects(loadSdk(async (n) => { tried.push(n); throw inner; }), (e) => e === inner);
  assert.deepEqual(tried, ["@microsoft/copilot-sdk/extension"]);
  const syntax = new SyntaxError("Unexpected token");
  await assert.rejects(loadSdk(async () => { throw syntax; }), (e) => e === syntax);
});

test("joinOptions: コマンドを、新しい SDK の interaction.commands と古い SDK の commands の両方に置く", () => {
  const commands = [{ name: "cxxref-index", handler: () => { } }];
  const o = joinOptions(commands);
  assert.equal(o.commands, commands);
  assert.equal(o.interaction.commands, commands);
});

test("projectChoices: フルパスの行を、ルートからの相対パスの選択肢にする", () => {
  const root = join(tmpdir(), "cxxref-lib-test");
  const out = `${join(root, "runtime", "runtime.slnx")}\r\n${join(root, "tools", "t", "q.sln")}\r\n\r\n`;
  assert.deepEqual(projectChoices(root, out), [
    { label: join("runtime", "runtime.slnx"), path: join(root, "runtime", "runtime.slnx") },
    { label: join("tools", "t", "q.sln"), path: join(root, "tools", "t", "q.sln") },
  ]);
  assert.deepEqual(projectChoices(root, ""), []);
});

test("savedProject: 前の作り方に残した -Project(ファイルがあるときだけ)", () => {
  const d = mkdtempSync(join(tmpdir(), "cri"));
  try {
    assert.equal(savedProject(d), "");
    mkdirSync(join(d, ".index"));
    const sln = join(d, "a.slnx");
    const recipe = (p) => writeFileSync(join(d, ".index", "index.tsv.recipe.json"), "﻿" + JSON.stringify({ makeCdb: { Root: d, Project: p } }));
    recipe(sln);
    assert.equal(savedProject(d), "");  // ファイルが無い
    writeFileSync(sln, "<Solution />");
    assert.equal(savedProject(d), sln);
    writeFileSync(join(d, ".index", "index.tsv.recipe.json"), JSON.stringify({ makeCdb: null }));
    assert.equal(savedProject(d), "");  // 手元の compile_commands.json を使った
  } finally { rmSync(d, { recursive: true, force: true }); }
});

test("escapeMarkdown: 記号の前の \\ だけを二重にする", () => {
  assert.equal(escapeMarkdown("E:\\src\\Proj\\.index\\a.log"), "E:\\src\\Proj\\\\.index\\a.log");
  assert.equal(escapeMarkdown("scripts\\get-ltidx.ps1(C:\\_x)"), "scripts\\get-ltidx.ps1(C:\\\\_x)");
});

function tree() {
  const d = mkdtempSync(join(tmpdir(), "cri"));
  mkdirSync(join(d, "plugin", "scripts"), { recursive: true });
  writeFileSync(join(d, "plugin", "plugin.json"), "{}");
  writeFileSync(join(d, "plugin", "scripts", "index-project.ps1"), "");
  mkdirSync(join(d, "plugin", "com.github.copilot", "extensions", "cxxref"), { recursive: true });
  mkdirSync(join(d, "repo", ".index"), { recursive: true });
  mkdirSync(join(d, "repo", "src", "sub"), { recursive: true });
  return d;
}

test("pluginRoot は拡張のフォルダから plugin.json のあるフォルダまでさかのぼる", () => {
  const d = tree();
  try {
    assert.equal(pluginRoot(join(d, "plugin", "com.github.copilot", "extensions", "cxxref")), join(d, "plugin"));
    assert.throws(() => pluginRoot(join(d, "repo", "src")));
  } finally { rmSync(d, { recursive: true, force: true }); }
});

test("findLtidx: LTIDX が無いファイルなら取ってこない(error)。LOCALAPPDATA が無ければ相対パスを探さない", () => {
  const d = tree();
  try {
    const plugin = join(d, "plugin");
    assert.match(findLtidx(plugin, { LTIDX: join(d, "none.exe") }).error, /LTIDX/);
    assert.deepEqual(findLtidx(plugin, {}), {});
    const got = join(d, "app", "cxxref", "ltidx");
    mkdirSync(got, { recursive: true });
    writeFileSync(join(got, "ltidx.exe"), "");
    assert.equal(findLtidx(plugin, { LOCALAPPDATA: join(d, "app") }).path, join(got, "ltidx.exe"));
    mkdirSync(join(plugin, "indexer", "build"), { recursive: true });
    writeFileSync(join(plugin, "indexer", "build", "ltidx.exe"), "");
    assert.equal(findLtidx(plugin, { LOCALAPPDATA: join(d, "app") }).path, join(plugin, "indexer", "build", "ltidx.exe"));
  } finally { rmSync(d, { recursive: true, force: true }); }
});

test("findLtidx: 取ってきた ltidx の版がプラグインと違えば stale(ビルドしたもの・LTIDX のものは見ない)", () => {
  const d = tree();
  try {
    const plugin = join(d, "plugin");
    writeFileSync(join(plugin, "plugin.json"), '{"name": "cxxref", "version": "0.6.0"}');
    const got = join(d, "app", "cxxref", "ltidx");
    mkdirSync(got, { recursive: true });
    writeFileSync(join(got, "ltidx.exe"), "");
    const env = { LOCALAPPDATA: join(d, "app") };
    assert.deepEqual(findLtidx(plugin, env).stale, { have: "不明", want: "0.6.0" });  // version.txt の無い古い配布物
    writeFileSync(join(got, "version.txt"), "0.5.1\n");
    assert.deepEqual(findLtidx(plugin, env).stale, { have: "0.5.1", want: "0.6.0" });
    writeFileSync(join(got, "version.txt"), "0.6.0\n");
    assert.deepEqual(findLtidx(plugin, env), { path: join(got, "ltidx.exe") });
    writeFileSync(join(got, "version.txt"), "0.5.1\n");
    assert.deepEqual(findLtidx(plugin, { ...env, LTIDX: join(got, "ltidx.exe") }), { path: join(got, "ltidx.exe") });
    mkdirSync(join(plugin, "indexer", "build"), { recursive: true });
    writeFileSync(join(plugin, "indexer", "build", "ltidx.exe"), "");
    assert.equal(findLtidx(plugin, env).stale, undefined);
  } finally { rmSync(d, { recursive: true, force: true }); }
});

test("parseIndexArgs: 先頭の full で最初から作り直す。残りはルート", () => {
  assert.deepEqual(parseIndexArgs(""), { full: false, root: "" });
  assert.deepEqual(parseIndexArgs("full"), { full: true, root: "" });
  assert.deepEqual(parseIndexArgs(" FULL  E:\\src\\proj "), { full: true, root: "E:\\src\\proj" });
  assert.deepEqual(parseIndexArgs("fullpath"), { full: false, root: "fullpath" });
  assert.deepEqual(parseIndexArgs("./full"), { full: false, root: "./full" });
  assert.deepEqual(parseIndexArgs('"E:\\a b"'), { full: false, root: '"E:\\a b"' });
});

test("lastCwd は最後の作業フォルダを返す", () => {
  const ev = [{ type: "session.start", data: { context: { cwd: "C:\\a" } } }, { type: "session.context_changed", data: { cwd: "C:\\b" } }];
  assert.equal(lastCwd(ev), "C:\\b");
  assert.equal(lastCwd([]), "");
});

test("targetRoot: 引数(引用符・相対)か、親へさかのぼって .index のあるフォルダ", () => {
  const d = tree();
  try {
    const repo = join(d, "repo");
    assert.equal(indexRoot(join(repo, "src", "sub")), repo);
    assert.equal(targetRoot("", join(repo, "src", "sub")).root, repo);
    assert.equal(targetRoot(`"${join(repo, "src")}"`, d).root, join(repo, "src"));
    assert.equal(targetRoot("src", repo).root, join(repo, "src"));
    // 区切りが重なった絶対パス(アプリが \ を二重にして渡したとき)も、ふつうの形に揃える
    assert.equal(targetRoot(join(repo, "src").replaceAll(sep, sep + sep), d).root, join(repo, "src"));
    assert.match(targetRoot("none", repo).error, /フォルダがありません/);
  } finally { rmSync(d, { recursive: true, force: true }); }
});

test("parseEnableArgs: on は省くとローカル、off は省くと両方、引数なしは状態", () => {
  assert.deepEqual(parseEnableArgs(""), { action: "Status", scope: "", root: "" });
  assert.deepEqual(parseEnableArgs("on"), { action: "Add", scope: "Local", root: "" });
  assert.deepEqual(parseEnableArgs("ON Project"), { action: "Add", scope: "Project", root: "" });
  assert.deepEqual(parseEnableArgs("off"), { action: "Remove", scope: "", root: "" });
  assert.deepEqual(parseEnableArgs("off local D:\\src\\game"), { action: "Remove", scope: "Local", root: "D:\\src\\game" });
  assert.deepEqual(parseEnableArgs('on "D:\\my src"'), { action: "Add", scope: "Local", root: '"D:\\my src"' });
  assert.deepEqual(parseEnableArgs("status project"), { action: "Status", scope: "", root: "" });
  assert.match(parseEnableArgs("enable").error, /使い方/);
});

test("statusHint: 入っていれば off を案内し、入っていなければ要らないと言う(on は勧めない)", () => {
  const state = (p, l) => `プロジェクト(E:\\src\\.github\\copilot-instructions.md): ${p}\r\nローカル(E:\\src\\.github\\instructions\\cxxref.instructions.md): ${l}`;
  const none = statusHint(state("入っていません", "入っていません"));
  assert.match(none, /要りません/);
  assert.doesNotMatch(none, /cxxref-enable on/);
  assert.match(statusHint(state("入っています", "入っていません")), /off/);
  assert.match(statusHint(state("入っていません", "入っています")), /off/);
  assert.equal(statusHint("何か別の出力"), "");
});

test("ENABLE_ON_GONE: on を断るときに、手で登録する人の手順と off を示す", () => {
  assert.match(ENABLE_ON_GONE, /setup-instructions\.bat/);
  assert.match(ENABLE_ON_GONE, /cxxref-enable off/);
});

test("enableRoot: 引数が無ければ git のルート、リポジトリでなければ作業フォルダ", () => {
  const d = mkdtempSync(join(tmpdir(), "cri"));
  try {
    const real = (p) => realpathSync.native(p).toLowerCase();
    mkdirSync(join(d, "plain"));
    assert.equal(real(enableRoot("", join(d, "plain")).root), real(join(d, "plain")));
    let git = true;
    try { execFileSync("git", ["init", "-q", join(d, "repo")], { stdio: "ignore" }); } catch { git = false; }
    if (git) {
      mkdirSync(join(d, "repo", "src"));
      assert.equal(real(gitTop(join(d, "repo", "src"))), real(join(d, "repo")));
      assert.equal(real(enableRoot("", join(d, "repo", "src")).root), real(join(d, "repo")));
      assert.equal(enableRoot("src", join(d, "repo")).root, join(d, "repo", "src"));  // 引数があればそこ
    }
    assert.equal(gitTop(join(d, "none")), "");
  } finally { rmSync(d, { recursive: true, force: true }); }
});

test("parseProgress: 終わったものの数(行の数)と、解析エラーの数(ほかの行は無視)", () => {
  // 並列なので n は前後する。いちばん大きい n(9)ではなく、終わった行の数(3)を使う
  const log = "[3/322] ok a.cpp\r\nwarning: something\r\n[9/322] NG b.cpp\r\n[7/322] ok c.cpp\r\n[ 10/322] ok d.cpp\r\n";
  assert.deepEqual(parseProgress(log), { done: 3, total: 322, ng: 1 });
  // 最後の TU の行が出ても、時間のかかる TU がまだ残っていれば全体に届かない
  assert.deepEqual(parseProgress("[1/3] ok a.cpp\n[3/3] ok c.cpp\n"), { done: 2, total: 3, ng: 0 });
  // 差分パス(altpass.py --progress)の形も同じ
  assert.deepEqual(parseProgress("alt pass: preparing\nalt pass: 2 files to analyze\n[1/2] NG x/a.h\n"), { done: 1, total: 2, ng: 1 });
  assert.equal(parseProgress("alt pass: preparing\n"), null);
  assert.equal(parseProgress("compile_commands.json: x\r\n"), null);
  assert.equal(parseProgress(""), null);
});

test("progressNote: 段階が変わったとき・1 割ごと(10 秒あけて)・数の無い段階は 1 分ごとに出す", () => {
  let s = {};
  const step = (snap, now, sec) => { const r = progressNote(s, snap, now, sec); s = r.state; return r.note; };
  assert.match(step({ phase: "cdb" }, 0, 0), /compile_commands\.json を用意しています$/);
  assert.equal(step({ phase: "cdb" }, 30000, 30), "");
  assert.match(step({ phase: "cdb" }, 61000, 61), /\(61 秒\)/);
  assert.match(step({ phase: "tu", done: 5, total: 100, ng: 0 }, 62000, 62), /5\/100\(5%、62 秒\)/);
  assert.equal(step({ phase: "tu", done: 9, total: 100, ng: 0 }, 80000, 80), "");      // 1 割に届かない
  assert.equal(step({ phase: "tu", done: 30, total: 100, ng: 2 }, 65000, 65), "");     // 10 秒たっていない
  assert.match(step({ phase: "tu", done: 30, total: 100, ng: 2 }, 73000, 73), /30\/100\(30%、解析エラー 2、73 秒\)/);
  assert.equal(step({ phase: "tu", done: 100, total: 100, ng: 2 }, 99000, 99), "");    // 終わりは "finish" で出す
  // TU の解析の後: 仕上げ → 差分パスの対象選び → 差分パスの解析。どれも黙らない
  assert.match(step({ phase: "finish" }, 100000, 100), /TU の解析が終わりました。索引を仕上げています$/);
  assert.equal(step({ phase: "finish" }, 130000, 130), "");
  assert.match(step({ phase: "finish" }, 161000, 161), /索引を仕上げています\(161 秒\)/);
  assert.match(step({ phase: "alt" }, 162000, 162), /索引の本体ができました\(もう検索に使えます\)。続けて差分パス.*対象を選んでいます$/);
  assert.match(step({ phase: "alt" }, 223000, 223), /差分パスの対象を選んでいます\(223 秒\)/);
  const first = step({ phase: "alt", done: 5, total: 40, ng: 0 }, 224000, 224);         // 数が出始めた(本体のことは繰り返さない)
  assert.match(first, /差分パス.*5\/40\(12%、224 秒\)/);
  assert.doesNotMatch(first, /本体/);
  assert.equal(step({ phase: "alt", done: 7, total: 40, ng: 0 }, 260000, 260), "");     // 次の 1 割に届かない
  assert.match(step({ phase: "alt", done: 8, total: 40, ng: 1 }, 270000, 270), /8\/40\(20%、解析できないもの 1、270 秒\)/);
});

test("progressNote: 差分パスへ対象選びを見ずに入ったら、本体ができたことも言う。TU が無く終わったら仕上げを出す", () => {
  const r = progressNote({ phase: "finish", at: 0 }, { phase: "alt", done: 1, total: 10, ng: 0 }, 5000, 5);
  assert.match(r.note, /索引の本体ができました\(もう検索に使えます\)。続けて差分パス.*1\/10\(10%、5 秒\)/);
  assert.match(progressNote({ phase: "cdb", at: 0 }, { phase: "finish" }, 1000, 1).note, /索引を仕上げています$/);
});

test("runsUnderCli: 拡張に渡された SDK が CLI の配布物の中なら CLI、そうでなければアプリ", () => {
  const no = () => false;  // SDK の親を見る判定は使わない
  const dist = String.raw`C:\tools\copilot\pkg\win32-x64\1.0.0`;
  assert.equal(runsUnderCli({ COPILOT_SDK_PATH: String.raw`${dist}\copilot-sdk`, COPILOT_CLI_DIST_DIR: dist }, no), true);
  // \\?\ 付き・/ 区切り・大文字小文字・末尾の \ の違いは同じ場所とみなす
  const slashed = dist.toUpperCase().replaceAll("\\", "/");
  assert.equal(runsUnderCli({ COPILOT_SDK_PATH: "\\\\?\\" + slashed + "/copilot-sdk", COPILOT_CLI_RESOLVED_DIST_DIR: `${dist}\\` }, no), true);
  assert.equal(runsUnderCli({ COPILOT_SDK_PATH: String.raw`C:\Program Files\GitHub Copilot\sdk`, COPILOT_CLI_RESOLVED_DIST_DIR: dist, COPILOT_CLI_PATH: "x" }, no), false);
  // 名前の頭が同じだけの別のフォルダは中とみなさない
  assert.equal(runsUnderCli({ COPILOT_SDK_PATH: String.raw`${dist}-other\sdk`, COPILOT_CLI_DIST_DIR: dist }, no), false);
  // 指定された場所(DIST_DIR)が読めずに別の場所を読み込んだ: 実際に読み込んだ場所(RESOLVED)の下なら CLI
  assert.equal(runsUnderCli({ COPILOT_SDK_PATH: String.raw`${dist}\copilot-sdk`, COPILOT_CLI_DIST_DIR: String.raw`C:\broken`, COPILOT_CLI_RESOLVED_DIST_DIR: dist }, no), true);
  // 配布物の場所が渡されなければ、SDK の親が CLI の配布物に見えるかで決める
  assert.equal(runsUnderCli({ COPILOT_SDK_PATH: String.raw`${dist}\copilot-sdk` }, (d) => pathKey(d) === pathKey(dist)), true);
  assert.equal(runsUnderCli({ COPILOT_SDK_PATH: String.raw`C:\app\copilot-sdk`, COPILOT_CLI_PATH: "x" }, no), false);
  // SDK の場所が分からなければアプリとみなす(CLI の印があっても。アプリも渡す)
  assert.equal(runsUnderCli({ COPILOT_CLI_PATH: String.raw`C:\copilot.exe` }, no), false);
  assert.equal(runsUnderCli({}, no), false);
});

test("runsUnderCli: ジャンクション越しの配布物も実体で比べる。npm-loader.js のある親は CLI の配布物とみなす", () => {
  const d = mkdtempSync(join(tmpdir(), "cri"));
  try {
    const real = join(d, "pkg", "1.0.0");
    mkdirSync(join(real, "copilot-sdk"), { recursive: true });
    const link = join(d, "current");
    symlinkSync(real, link, "junction");
    const no = () => false;
    assert.equal(runsUnderCli({ COPILOT_SDK_PATH: join(real, "copilot-sdk"), COPILOT_CLI_DIST_DIR: link }, no), true);
    assert.equal(runsUnderCli({ COPILOT_SDK_PATH: join(link, "copilot-sdk"), COPILOT_CLI_RESOLVED_DIST_DIR: real }, no), true);
    assert.equal(runsUnderCli({ COPILOT_SDK_PATH: join(real, "copilot-sdk") }), false);
    writeFileSync(join(real, "npm-loader.js"), "");
    assert.equal(runsUnderCli({ COPILOT_SDK_PATH: join(link, "copilot-sdk") }), true);
  } finally { rmSync(d, { recursive: true, force: true }); }
});

test("wantDialog: CXXREF_DISPLAY で決める。auto(既定)はアプリのときだけ", () => {
  const cli = { COPILOT_SDK_PATH: String.raw`C:\d\sdk`, COPILOT_CLI_DIST_DIR: String.raw`C:\d` };
  const app = { COPILOT_SDK_PATH: String.raw`C:\app\sdk`, COPILOT_CLI_DIST_DIR: String.raw`C:\d` };
  assert.equal(wantDialog(cli), false);
  assert.equal(wantDialog(app), true);
  assert.equal(wantDialog({ ...app, CXXREF_DISPLAY: "timeline" }), false);
  assert.equal(wantDialog({ ...cli, CXXREF_DISPLAY: "dialog" }), true);
  assert.equal(wantDialog({ ...cli, CXXREF_DISPLAY: "auto" }), false);
  assert.equal(wantDialog({}, true), true);  // アプリかどうかを渡せば、それを使う
});

// makeNotifier に渡す偽のセッション。ダイアログは、テストが close() を呼ぶまで開いたまま
function fakeSession({ elicitation = true, fail = "" } = {}) {
  const s = { logs: [], dialogs: [], opens: [], capabilities: { ui: { elicitation } } };
  s.log = async (message, options) => { s.logs.push({ message, ...options }); };
  s.ui = {
    elicitation: async (p) => {
      s.dialogs.push(p.message);
      if (fail) throw new Error(fail);
      return new Promise((done) => s.opens.push(() => done({ action: "accept" })));
    },
  };
  s.close = () => s.opens.shift()();
  return s;
}
const tick = () => new Promise((r) => setImmediate(r));
const appEnv = { COPILOT_SDK_PATH: String.raw`C:\no-such\app\sdk`, COPILOT_CLI_DIST_DIR: String.raw`C:\no-such\cli` };
const cliEnv = { COPILOT_SDK_PATH: String.raw`C:\no-such\cli\copilot-sdk`, COPILOT_CLI_DIST_DIR: String.raw`C:\no-such\cli` };

test("makeNotifier: アプリでは tell の文をダイアログにも出す(error と進み具合は出さない)。\\ は二重にする", async () => {
  const s = fakeSession();
  const n = makeNotifier(() => s, appEnv);
  await n.tell("できました E:\\p\\.index");
  await n.tell("取り直せません", "warning");
  await n.tell("失敗", "error");
  await n.say("10%");
  await tick();
  assert.deepEqual(s.dialogs, ["できました E:\\p\\\\.index"]);  // 前のを閉じるまで次は出さない
  s.close(); await tick();
  assert.deepEqual(s.dialogs, ["できました E:\\p\\\\.index", "取り直せません"]);
  s.close(); await n.settled();
  assert.equal(s.dialogs.length, 2);
  assert.deepEqual(s.logs.map((l) => [l.message, l.level]), [
    ["できました E:\\p\\\\.index", undefined], ["取り直せません", "warning"], ["失敗", "error"], ["10%", undefined]]);
});

test("makeNotifier: アプリでダイアログを出せなければ、error で出し直す。出せるようになったらダイアログに戻る", async () => {
  const s = fakeSession({ elicitation: false });
  const n = makeNotifier(() => s, appEnv);
  await n.tell("状態 E:\\p\\.index");
  await n.settled();
  assert.deepEqual(s.dialogs, []);
  assert.deepEqual(s.logs.at(-1), { message: "状態 E:\\p\\.index\n(このホストはダイアログを出せません)", ephemeral: true, level: "error" });
  s.capabilities.ui.elicitation = true;
  await n.tell("次");
  await tick();
  assert.deepEqual(s.dialogs, ["次"]);
  s.close(); await n.settled();

  const f = fakeSession({ fail: "timeout" });
  const m = makeNotifier(() => f, appEnv);
  await m.tell("できました");
  await m.settled();
  assert.equal(f.logs.at(-1).message, "できました\n(ダイアログを出せませんでした: timeout)");
  assert.equal(f.logs.at(-1).level, "error");
});

test("makeNotifier: CLI ではダイアログを出さない。CXXREF_DISPLAY=dialog なら出す(\\ はそのまま、失敗しても出し直さない)", async () => {
  const s = fakeSession();
  const n = makeNotifier(() => s, cliEnv);
  await n.tell("できました");
  await n.settled();
  assert.deepEqual(s.dialogs, []);

  const f = fakeSession({ fail: "x" });
  const m = makeNotifier(() => f, { ...cliEnv, CXXREF_DISPLAY: "dialog" });
  await m.tell("E:\\p\\.index");
  await m.settled();
  assert.deepEqual(f.dialogs, ["E:\\p\\.index"]);
  assert.deepEqual(f.logs.map((l) => l.level), [undefined]);  // タイムラインに出ているので、error で出し直さない
});
