#!/usr/bin/env node
// 端到端测试（零依赖、不需要浏览器）：对 dist-site/ 起一个真的 HTTP 服务器，按"访客"的路径走一遍。
//   node scripts/site-e2e.mjs [all|index|version]      先 `node scripts/site-build.mjs`
// - server：/ 返回页面、/version.json 返回合法 JSON、未知路径 404、../ 穿越被拒
// - version：commit 是完整 40 位 SHA，并且等于这次构建的提交（CI 里是 GITHUB_SHA，本地是 HEAD）
// - index：把页面里的内联脚本放进 node:vm 里真的执行（假 document + 真 fetch 到本地服务器），
//          断言页面最终显示的就是 version.json 里的提交。没有真浏览器，但页面逻辑是真跑的。
import { existsSync, readFileSync } from "node:fs";
import http from "node:http";
import path from "node:path";
import vm from "node:vm";
import { fileURLToPath } from "node:url";
import { ROOT, resolveCommit } from "./site-build.mjs";
import { startServer } from "./serve.mjs";

const which = process.argv[2] ?? "all";
if (!["all", "index", "version"].includes(which)) {
  console.error("用法：e2e.mjs [all|index|version]");
  process.exit(2);
}
const dist = path.join(ROOT, "dist-site");
if (!existsSync(path.join(dist, "version.json"))) {
  console.error("dist-site/ 不存在：先运行 node scripts/site-build.mjs");
  process.exit(2);
}

const failures = [];
const check = (name, ok, detail = "") => {
  console.log(`  [${ok ? " OK " : "FAIL"}] ${name}${!ok && detail ? `：${detail}` : ""}`);
  if (!ok) failures.push(name);
};
const rawGet = (port, requestPath) =>
  new Promise((resolve, reject) => {
    // 不能用 fetch：它会在发出前把 /../ 规范化掉，测不到服务器自己的防护
    const request = http.request({ host: "127.0.0.1", port, path: requestPath, method: "GET" }, (response) => {
      response.resume();
      response.on("end", () => resolve(response.statusCode));
    });
    request.on("error", reject);
    request.end();
  });

const { server, port } = await startServer(dist, 0);
const base = `http://127.0.0.1:${port}/`;
try {
  const indexResponse = await fetch(base);
  const indexHtml = await indexResponse.text();
  const versionResponse = await fetch(`${base}version.json`);
  const version = await versionResponse.json();

  if (which === "all") {
    check("GET / → 200 text/html", indexResponse.status === 200 && (indexResponse.headers.get("content-type") ?? "").startsWith("text/html"));
    check("GET /version.json → 200 application/json", versionResponse.status === 200 && (versionResponse.headers.get("content-type") ?? "").startsWith("application/json"));
    check("未知路径 → 404", (await fetch(`${base}nope.txt`)).status === 404);
    check("../ 穿越被拒（不是 200）", (await rawGet(port, "/../package.json")) !== 200 && (await rawGet(port, "/%2e%2e/ops/delivery.json")) !== 200);
    check("页面没有引用任何外部资源", !/(src|href)\s*=\s*["']https?:\/\/(?!github\.com)/i.test(indexHtml));
  }
  if (which === "all" || which === "version") {
    const { commit } = resolveCommit();
    check("version.json 的 ok 为 true", version.ok === true);
    check("version.json 的 commit 是完整 40 位 SHA", /^[0-9a-f]{40}$/.test(version.commit ?? ""), String(version.commit));
    check("version.json 的 commit 等于本次构建的提交", version.commit === commit, `${version.commit} ≠ ${commit}`);
    check("version.json 带 repo 和 built_at", /^[\w.-]+\/[\w.-]+$/.test(version.repo ?? "") && !Number.isNaN(Date.parse(version.built_at)));
  }
  if (which === "all" || which === "index") {
    const script = /<script>([\s\S]*?)<\/script>/.exec(indexHtml)?.[1];
    check("页面里有内联脚本", Boolean(script));
    if (script) {
      // 假 document 只认 HTML 里真有的 id：脚本找的元素在页面里不存在（改了 id、删了元素），getElementById 返回 null，
      // 脚本照浏览器一样抛 TypeError，页面状态就会是"失败"——否则这条测试对 HTML 的改动是瞎的
      const htmlIds = new Set([...indexHtml.matchAll(/\bid="([^"]+)"/g)].map((m) => m[1]));
      const elements = new Map();
      const element = (id) => {
        if (!elements.has(id)) elements.set(id, { id, textContent: "", href: "" });
        return elements.get(id);
      };
      const sandbox = {
        window: {},
        document: { getElementById: (id) => (htmlIds.has(id) ? element(id) : null) },
        fetch: (input, init) => fetch(new URL(input, base), init), // 相对路径按页面所在地址解析，和浏览器一致
        Error,
        String,
      };
      vm.runInNewContext(script, sandbox, { timeout: 5000 });
      await Promise.race([sandbox.window.__ready, new Promise((_, reject) => setTimeout(() => reject(new Error("页面脚本 5 秒没跑完")), 5000))]);
      check("页面显示的短 SHA 等于 version.json 的提交前 7 位", element("commit").textContent === version.commit.slice(0, 7), element("commit").textContent);
      check("页面显示了完整 SHA", element("commit-full").textContent === version.commit);
      check("页面的提交链接指向本仓库的这个提交", element("commit-link").href === `https://github.com/${version.repo}/commit/${version.commit}`, element("commit-link").href);
      check("页面状态不是失败", htmlIds.has("status") && !element("status").textContent.includes("失败"), element("status").textContent);
    }
  }
} finally {
  server.close();
}

if (failures.length) {
  console.error(`端到端测试失败 ${failures.length} 项：${failures.join("；")}`);
  process.exit(1);
}
console.log(`端到端测试通过（${which}）`);
