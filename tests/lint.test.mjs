import assert from "node:assert/strict";
import { existsSync, readFileSync } from "node:fs";
import path from "node:path";
import test from "node:test";
import { fileURLToPath } from "node:url";
import { checkDelivery, checkHtml, checkWorkflow, scanSecrets } from "../scripts/lint.mjs";

const ROOT = path.resolve(path.dirname(fileURLToPath(import.meta.url)), "..");
const read = (p) => readFileSync(path.join(ROOT, p), "utf8");
const readOrNull = (p) => (existsSync(path.join(ROOT, p)) ? read(p) : null);

test("密钥扫描：各类令牌/私钥/标识都能识别（测试样本运行时拼出来，免得本文件自己被扫出）", () => {
  const samples = {
    "GitHub 令牌": `x = "${"ghp"}_${"a1".repeat(18)}"`,
    "GitHub 细粒度令牌": `${"github"}_${"pat"}_${"A1b2".repeat(8)}`,
    "私钥": `${"-----"}BEGIN OPENSSH PRIVATE KEY${"-----"}`,
    "AWS 密钥 ID": `${"AK"}${"IA"}${"ABCDEFGHIJKLMNOP"}`,
    "sk- 开头的 API 密钥": `${"sk"}-${"x".repeat(30)}`,
    "飞书 open_id/chat_id/message_id": `${"ou"}_${"0123456789abcdef".repeat(2)}`,
    "飞书应用 ID": `${"cli"}_${"0123456789abcdef"}`,
    "带密码的网址": `${"https"}://user:${"pw"}@example.com/x`,
  };
  for (const [name, text] of Object.entries(samples)) {
    assert.ok(scanSecrets(`第一行\n${text}\n`).some(([n, line]) => n === name && line === 2), name);
  }
  assert.deepEqual(scanSecrets("普通文字 https://github.com/owner/repo/commit/abc 没问题\nou_xxx 太短不算"), []);
});

test("交付设置：仓库自己的 ops/delivery.json 合格，review.points 里有审查要点", () => {
  assert.deepEqual(checkDelivery(JSON.parse(read("ops/delivery.json")), readOrNull), []);
});

test("交付设置：坏的会被逐条指出", () => {
  const good = JSON.parse(read("ops/delivery.json"));
  const bad = (change) => checkDelivery({ ...structuredClone(good), ...change }, readOrNull);
  assert.ok(bad({ repo: "没有斜杠" }).some((p) => p.includes("owner/name")));
  assert.ok(bad({ internal: ["/etc/passwd"] }).some((p) => p.includes("internal")));
  assert.ok(bad({ internal: ["../x"] }).some((p) => p.includes("internal")));
  assert.ok(bad({ surprise: 1 }).some((p) => p.includes("不认识")));
  assert.ok(bad({ review: { points: "NOPE.md" } }).some((p) => p.includes("不存在")));
  assert.ok(bad({ review: { points: "README-NOT-THERE.md" } }).length > 0);
  assert.ok(bad({ health: { ...good.health, url: "http://x.example/version.json" } }).some((p) => p.includes("HTTPS")));
  assert.ok(bad({ health: { ...good.health, pattern: "[0-9a-f]{40}" } }).some((p) => p.includes("命名分组")));
  assert.ok(bad({ health: { ...good.health, field: "" } }).some((p) => p.includes("health.field")));
  assert.ok(bad({ check: { workflow: "check" } }).some((p) => p.includes("check.workflow")));
  assert.ok(checkDelivery({ ...good, review: { points: "X.md" } }, () => "# X\n\n## 别的\nabc").some((p) => p.includes("审查要点")));
  assert.ok(checkDelivery({ ...good, review: { points: "X.md" } }, () => "## 审查要点\n\n## 下一节\nabc").some((p) => p.includes("审查要点")));
});

test("工作流：仓库自己的两个文件合格", () => {
  assert.deepEqual(checkWorkflow("check.yml", read(".github/workflows/check.yml")), []);
  assert.deepEqual(checkWorkflow("ship.yml", read(".github/workflows/ship.yml")), []);
});

test("工作流：没钉 SHA、用 matrix、job 名不对、权限放开都会被指出", () => {
  const check = read(".github/workflows/check.yml");
  assert.ok(checkWorkflow("check.yml", check.replace(/actions\/checkout@[0-9a-f]{40}/, "actions/checkout@v4")).some((p) => p.includes("40 位")));
  assert.ok(checkWorkflow("check.yml", check.replace("  check:", "  build:")).some((p) => p.includes("job 必须叫 check")));
  assert.ok(checkWorkflow("check.yml", `${check}\n    strategy:\n      matrix:\n        os: [a]\n`).some((p) => p.includes("matrix")));
  assert.ok(checkWorkflow("check.yml", check.replace("run: make check", "run: echo hi")).some((p) => p.includes("make check")));
  assert.ok(checkWorkflow("check.yml", check.replace("contents: read", "write-all")).some((p) => p.includes("write-all")));
  const ship = read(".github/workflows/ship.yml");
  assert.ok(checkWorkflow("ship.yml", ship.replace(/plugins\/delivery@[0-9a-f]{40}/, "plugins/delivery@main")).length > 0);
  assert.ok(checkWorkflow("ship.yml", ship.replace("fetch-depth: 0", "fetch-depth: 1")).some((p) => p.includes("fetch-depth")));
});

test("页面：合格；外部资源和调试残留会被指出", () => {
  const html = read("site/index.html");
  assert.deepEqual(checkHtml(html), []);
  assert.ok(checkHtml(html.replace("</body>", '<script src="https://cdn.example/x.js"></script></body>')).some((p) => p.includes("外部")));
  assert.ok(checkHtml(html.replace("</head>", '<link rel="stylesheet" href="x.css"></head>')).some((p) => p.includes("外部脚本/样式")));
  assert.ok(checkHtml(html.replace("window.__ready", "console.log(1); window.__ready")).some((p) => p.includes("调试残留")));
  assert.ok(checkHtml(html.replace('id="commit"', 'id="other"')).some((p) => p.includes("commit")));
});
