// node --test com.github.copilot/extensions/cxxref/lib.test.mjs(mcp/test/test_plugin.py が流す)。/cxxref-index の SDK に依らない部分
import assert from "node:assert/strict";
import { mkdirSync, mkdtempSync, rmSync, writeFileSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { test } from "node:test";
import { escapeMarkdown, findLtidx, indexRoot, lastCwd, pluginRoot, targetRoot } from "./lib.mjs";

test("escapeMarkdown: 記号の前の \\ だけを二重にする", () => {
  assert.equal(escapeMarkdown("E:\\src\\Nox\\.index\\a.log"), "E:\\src\\Nox\\\\.index\\a.log");
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
    assert.match(targetRoot("none", repo).error, /フォルダがありません/);
  } finally { rmSync(d, { recursive: true, force: true }); }
});
