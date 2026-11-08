// cxxref の Copilot CLI 拡張。モデルは呼ばない(スクリプトを裏で動かし、結果をセッションに出すだけ)。
//
// /cxxref-index [ルート]  作業中のリポジトリの索引を作る。ltidx.exe が無ければ、先に scripts\get-ltidx.ps1 で取ってくる。
//                         索引がすでにあれば、変わった TU だけを作り直す(-Update)。裏で動かし、終わったらそう出す。
//                         ルートを省くと、作業フォルダから親へさかのぼって .index のあるフォルダ、無ければ作業フォルダ。
//
// 拡張は Copilot CLI の実験的機能で、機能フラグ EXTENSIONS が有効なときだけ読み込まれる。
import { joinSession } from "@github/copilot-sdk/extension";
import { spawn } from "node:child_process";
import { closeSync, existsSync, mkdirSync, openSync } from "node:fs";
import { tmpdir } from "node:os";
import { dirname, join } from "node:path";
import { fileURLToPath } from "node:url";
import { escapeMarkdown, findLtidx, lastCwd, pluginRoot, targetRoot } from "./lib.mjs";

function run(script, args, log) {
  return new Promise((done) => {
    const fd = openSync(log, "a");
    const p = spawn("pwsh", ["-NoProfile", "-ExecutionPolicy", "Bypass", "-File", script, ...args], {
      stdio: ["ignore", fd, fd],
      windowsHide: true,
    });
    p.on("error", (err) => { closeSync(fd); done({ code: null, error: err }); });
    p.on("exit", (code) => { closeSync(fd); done({ code }); });
  });
}

// 終わり方の説明(pwsh を起動できなかったときは、その理由)
const why = (r) => (r.error ? `pwsh を起動できません(PowerShell 7 が要ります): ${r.error.message}` : `終了コード ${r.code}`);

let running = null; // 走っている索引づくり(同時に 1 つだけ)

const tell = (message, level) =>
  session.log(level ? message : escapeMarkdown(message), { ephemeral: true, ...(level ? { level } : {}) });

async function indexCommand(ctx) {
  // await の前に印を付ける(続けて呼ばれても、2 つ目は始めない)
  if (running) {
    await tell(`cxxref: 索引を作っている途中です(${running})。終わるまで待ってください`, "warning");
    return;
  }
  running = "準備中";
  try {
    const plugin = pluginRoot(dirname(fileURLToPath(import.meta.url)));
    const cwd = lastCwd(await session.getEvents()) || process.cwd();
    const t = targetRoot(ctx.args, cwd);
    if (t.error) {
      await tell(`cxxref: ${t.error}`, "error");
      return;
    }
    const root = t.root;
    running = root;
    const t0 = Date.now();
    let lt = findLtidx(plugin);
    if (lt.error) {
      await tell(`cxxref: ${lt.error}(LTIDX を直すか消してください)`, "error");
      return;
    }
    if (!lt.path) {
      // 取ってくる前は、ルートに .index を作らない(失敗したときに空の .index が残ると、そこが索引のルートと見なされる)
      const log = join(tmpdir(), "cxxref-get-ltidx.log");
      await tell("cxxref: ltidx.exe が無いので、先にリリースから取ってきます(scripts\\get-ltidx.ps1)");
      const r = await run(join(plugin, "scripts", "get-ltidx.ps1"), [], log);
      lt = findLtidx(plugin);
      if (r.code !== 0 || !lt.path) {
        await tell(`cxxref: ltidx を取ってこられませんでした(${why(r)})。ログ: ${log}`, "error");
        return;
      }
    }
    const dir = join(root, ".index");
    mkdirSync(dir, { recursive: true });
    const log = join(dir, "cxxref-index.log");
    const update = existsSync(join(dir, "index.tsv.deps.tsv"));
    await tell(`cxxref: 索引を${update ? "作り直し" : "作り"}始めました(${root})。裏で動かすので、作業を続けてかまいません。ログ: ${log}`);
    // index-project.ps1 は、索引ができたら 0、できなければ 2 で終える(ltidx の失敗や異常終了も 2)
    const r = await run(join(plugin, "scripts", "index-project.ps1"), ["-Root", root, ...(update ? ["-Update"] : [])], log);
    const sec = Math.round((Date.now() - t0) / 1000);
    if (r.code === 0) {
      await tell(`cxxref: 索引ができました(${sec} 秒、${dir})。以後は MCP サーバーの refs / check がこの索引を使い、コードを変えても自動で作り直します`);
    } else {
      await tell(`cxxref: 索引づくりに失敗しました(${why(r)}、${sec} 秒)。ログ: ${log}`, "error");
    }
  } finally {
    running = null;
  }
}

const session = await joinSession({
  commands: [
    {
      // 拡張のコマンドには引数のヒントを付けられないので、説明文の先頭に引数を書く
      name: "cxxref-index",
      description: "[ルート] — C++ の参照索引(cxxref)を作る・作り直す。裏で動かす(モデルは呼ばない)",
      handler: (ctx) => {
        indexCommand(ctx).catch((err) => tell(`cxxref: ${err?.message ?? err}`, "error"));
      },
    },
  ],
});
