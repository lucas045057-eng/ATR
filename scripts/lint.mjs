#!/usr/bin/env node
// 零依赖的仓库体检（make lint）。这个仓库是【公开】的（Pages 免费版的要求），所以第一条就是防泄密。
//   1. 密钥/私人标识扫描：GitHub 令牌、私钥、云厂商密钥、飞书 open_id 等一律不许出现在仓库里
//   2. 语法：*.sh 用 bash -n，*.mjs 用 node --check，*.json 必须能解析
//   3. ops/delivery.json：按交付插件（plugins/delivery/project.ts 的 settings）同样的规则自检，
//      并确认 review.points 指向的文件里真有 "## 审查要点"
//   4. 工作流：job 名必须叫 check（交付插件按这个名字找）；所有第三方 action 必须钉到 40 位提交 SHA
//   5. index.html：没有外部资源、没有调试残留
//   6. features.json：格式 + passing 项的 digest
import { spawnSync } from "node:child_process";
import { existsSync, readFileSync, readdirSync, statSync } from "node:fs";
import path from "node:path";
import { fileURLToPath } from "node:url";
import { ROOT } from "./site-build.mjs";
import { load as loadFeatures, validate as validateFeatures } from "./features.mjs";

// ---------- 纯函数（tests/ 里直接测） ----------
const SECRET_PATTERNS = [
  ["GitHub 令牌", /\b(?:ghp|gho|ghu|ghs|ghr)_[A-Za-z0-9]{30,}\b/],
  ["GitHub 细粒度令牌", /\bgithub_pat_[A-Za-z0-9_]{20,}\b/],
  ["私钥", /-----BEGIN [A-Z ]*PRIVATE KEY-----/],
  ["AWS 密钥 ID", /\bAKIA[0-9A-Z]{16}\b/],
  ["sk- 开头的 API 密钥", /\bsk-[A-Za-z0-9_-]{20,}\b/],
  ["飞书 open_id/chat_id/message_id", /\b(?:ou|oc|om)_[0-9a-f]{20,}\b/],
  ["飞书应用 ID", /\bcli_[0-9a-f]{16}\b/],
  ["带密码的网址", /\bhttps?:\/\/[^\s/:@]+:[^\s/@]+@[^\s/]+/],
];

/** 返回命中的 [名称, 行号] 列表。 */
export function scanSecrets(text) {
  const hits = [];
  text.split("\n").forEach((line, index) => {
    for (const [name, pattern] of SECRET_PATTERNS) if (pattern.test(line)) hits.push([name, index + 1]);
  });
  return hits;
}

const isWords = (v) => Array.isArray(v) && v.every((w) => typeof w === "string" && w.length > 0);
const isPaths = (v) => isWords(v) && v.every((p) => !p.startsWith("/") && !p.split("/").includes(".."));

/** 与 plugins/delivery/project.ts 的 settings() 同样的规则；fileText(path) 读仓库内文件，不存在返回 null。 */
export function checkDelivery(value, fileText) {
  const problems = [];
  const KEYS = ["repo", "internal", "admin_merge", "check", "ship", "review", "health"];
  if (!value || typeof value !== "object") return ["ops/delivery.json 不是对象"];
  if (!/^[\w.-]+\/[\w.-]+$/.test(value.repo ?? "")) problems.push("repo 要写成 owner/name");
  if (!isPaths(value.internal)) problems.push("internal 要是相对路径(glob)的列表");
  for (const key of Object.keys(value)) if (!KEYS.includes(key)) problems.push(`不认识的字段 ${key}`);
  const { review, health } = value;
  if (!review || !isPaths([review.points])) problems.push("review.points 要是仓库内的相对路径");
  else {
    if (review.xhigh !== undefined && !isPaths(review.xhigh)) problems.push("review.xhigh 要是相对路径(glob)的列表");
    const text = fileText(review.points);
    if (text === null) problems.push(`review.points 指向的 ${review.points} 不存在`);
    else if (!/^## 审查要点\r?\n([\s\S]*?)(?=^## |$(?![\s\S]))/m.exec(text)?.[1]?.trim()) {
      problems.push(`${review.points} 里没有内容非空的 "## 审查要点"`);
    }
  }
  if (value.admin_merge !== undefined && !isWords(value.admin_merge)) problems.push("admin_merge 要是字符串列表");
  for (const [name, workflow] of [["check.workflow", value.check?.workflow], ["ship.workflow", value.ship?.workflow]]) {
    if (workflow !== undefined && !/^[\w.-]+\.ya?ml$/.test(workflow)) problems.push(`${name} 要写成 xxx.yml`);
  }
  if (health) {
    try {
      const url = new URL(health.url);
      if (url.protocol !== "https:" || url.username || url.password) problems.push("health.url 必须是不带账号密码的 HTTPS 地址");
    } catch {
      problems.push("health.url 不是合法网址");
    }
    if (typeof health.field !== "string" || !health.field) problems.push("health.field 要是字符串（本模板用 commit）");
    if (typeof health.pattern !== "string" || !/\(\?(?:P)?<sha>/.test(health.pattern)) problems.push("health.pattern 要带命名分组 (?P<sha>…)");
    else {
      try {
        new RegExp(health.pattern.replace("(?P<sha>", "(?<sha>"));
      } catch {
        problems.push("health.pattern 不是合法正则");
      }
    }
    if (health.require !== undefined && (typeof health.require !== "object" || health.require === null || Array.isArray(health.require))) {
      problems.push("health.require 要是对象");
    }
  }
  return problems;
}

/** 工作流文件体检。name 是文件名（check.yml / ship.yml / 其它）。 */
export function checkWorkflow(name, text) {
  const problems = [];
  for (const m of text.matchAll(/^\s*-?\s*uses:\s*(\S+)/gm)) {
    const ref = m[1];
    if (ref.startsWith("./")) continue;
    if (!/@[0-9a-f]{40}$/.test(ref)) problems.push(`${name}: ${ref} 没有钉到 40 位提交 SHA（用 SHA + 行尾注释写版本号）`);
  }
  if (/^\s*permissions:\s*write-all/m.test(text)) problems.push(`${name}: 不要用 write-all`);
  if (name === "check.yml") {
    if (!/^  check:\s*$/m.test(text)) problems.push("check.yml: job 必须叫 check（交付插件按名字找）");
    if (!/^\s*pull_request:/m.test(text)) problems.push("check.yml: 必须在 pull_request 上触发");
    if (/matrix:/.test(text)) problems.push("check.yml: 不要用 matrix——job 名会变成 check (…)，交付插件找不到");
    if (!/^\s*run:\s*make check\s*$/m.test(text)) problems.push("check.yml: 要有一步 `run: make check`（不是注释里提到）");
  }
  if (name === "ship.yml") {
    if (!/^\s*push:\s*\n\s*branches:\s*\[?\s*main/m.test(text)) problems.push("ship.yml: 只在 main 的 push 上运行");
    if (!/KKKKhazix\/agent-autoloop\/plugins\/delivery@[0-9a-f]{40}/.test(text)) problems.push("ship.yml: 要调用钉死 SHA 的 agent-autoloop/plugins/delivery Action");
    if (!/fetch-depth:\s*0/.test(text)) problems.push("ship.yml: checkout 要 fetch-depth: 0");
    if (!/deploy-pages@/.test(text)) problems.push("ship.yml: 要用官方 deploy-pages 发布");
  }
  return problems;
}

/** 页面体检：外部资源、调试残留。 */
export function checkHtml(text) {
  const problems = [];
  if (!/^<!doctype html>/i.test(text.trimStart())) problems.push("index.html 要以 <!doctype html> 开头");
  if (!/<html[^>]*\blang=/.test(text)) problems.push("index.html 的 <html> 要写 lang");
  if (!/<title>[^<]+<\/title>/.test(text)) problems.push("index.html 缺 <title>");
  if (/(?:src|href)\s*=\s*["']https?:\/\/(?!github\.com\/)/i.test(text)) problems.push("index.html 引用了外部资源（纯静态站点不许）");
  if (/<script[^>]+src=/i.test(text) || /<link[^>]+rel=["']?stylesheet/i.test(text)) problems.push("index.html 不要引用外部脚本/样式文件");
  if (/\bdebugger\b|console\.(log|debug)\(/.test(text)) problems.push("index.html 有调试残留（debugger / console.log）");
  if (!/id="commit"/.test(text) || !/version\.json/.test(text)) problems.push("index.html 要有 id=\"commit\" 并读取 version.json");
  return problems;
}

// ---------- 实际体检 ----------
function trackedFiles() {
  const git = spawnSync("git", ["ls-files", "-z", "--cached", "--others", "--exclude-standard"], { cwd: ROOT, encoding: "utf8" });
  if (git.status === 0 && git.stdout) return git.stdout.split("\0").filter(Boolean);
  const out = [];
  const walk = (dir) => {
    for (const entry of readdirSync(path.join(ROOT, dir), { withFileTypes: true })) {
      if ([".git", "node_modules", "dist"].includes(entry.name)) continue;
      const rel = path.posix.join(dir, entry.name);
      entry.isDirectory() ? walk(rel) : out.push(rel);
    }
  };
  walk(".");
  return out.map((p) => p.replace(/^\.\//, ""));
}

function main() {
  const problems = [];
  const files = trackedFiles().filter((f) => existsSync(path.join(ROOT, f)) && statSync(path.join(ROOT, f)).isFile());
  const read = (f) => readFileSync(path.join(ROOT, f), "utf8");

  for (const file of files) {
    if (file === "uv.lock" || file.startsWith("third_party/licenses/") || /\.(png|jpe?g|gif|ico|woff2?|pdf|mp4|webm|mov|zip|gz|tar|7z|ttf|bin|onnx)$/i.test(file)) continue;
    let text;
    try {
      if (readFileSync(path.join(ROOT, file)).includes(0)) continue;
      text = read(file);
    } catch {
      continue;
    }
    // 已逐项核对为安全测试的伪凭据；仅放行这一行的这一类命中，其他内容仍扫描。
    const fixtureExceptions = {
      "tests/ab_v1/test_health_observer.py": { name: "带密码的网址", text: "http" + "://u:" + "secret@localhost/health" },
      "tests/test_additional_security.py": { name: "私钥", text: 'private = "-----' + 'BEGIN PRIVATE KEY-----\\nprivate-material\\n-----END PRIVATE KEY-----"' },
    };
    for (const [name, line] of scanSecrets(text)) {
      const exception = fixtureExceptions[file];
      if (exception?.name === name && text.split("\n")[line - 1].includes(exception.text)) continue;
      problems.push(`${file}:${line} 疑似${name}——公开仓库里不许有`);
    }
    if (file.endsWith(".json")) {
      try {
        JSON.parse(text);
      } catch (error) {
        problems.push(`${file} 不是合法 JSON：${error.message}`);
      }
    }
    if (file.endsWith(".sh") && spawnSync("bash", ["-n", file], { cwd: ROOT }).status !== 0) problems.push(`${file} 没通过 bash -n`);
    if (file.endsWith(".mjs") && spawnSync(process.execPath, ["--check", file], { cwd: ROOT }).status !== 0) problems.push(`${file} 没通过 node --check`);
    if (/\.(sh|mjs|json|ya?ml|md|html)$/.test(file) && text.includes("\r")) problems.push(`${file} 含 CRLF 换行，统一用 LF`);
  }

  try {
    const delivery = JSON.parse(read("ops/delivery.json"));
    problems.push(...checkDelivery(delivery, (p) => (existsSync(path.join(ROOT, p)) ? read(p) : null)).map((p) => `ops/delivery.json: ${p}`));
    const host = delivery.health ? new URL(delivery.health.url).pathname : "";
    const name = (delivery.repo ?? "").split("/")[1];
    if (delivery.health && name && !host.startsWith(`/${name}/`) && name !== `${(delivery.repo ?? "").split("/")[0]}.github.io`) {
      problems.push(`ops/delivery.json: health.url 的路径 ${host} 应以 /${name}/ 开头（Pages 项目站点在 /<仓库名>/ 下）`);
    }
  } catch (error) {
    problems.push(`ops/delivery.json 读不了：${error.message}`);
  }

  for (const file of files.filter((f) => /^\.github\/workflows\/[^/]+\.ya?ml$/.test(f))) {
    problems.push(...checkWorkflow(path.basename(file), read(file)));
  }
  for (const required of [".github/workflows/check.yml", ".github/workflows/ship.yml"]) {
    if (!files.includes(required)) problems.push(`缺少 ${required}`);
  }

  if (existsSync(path.join(ROOT, "site/index.html"))) problems.push(...checkHtml(read("site/index.html")));
  else problems.push("缺少 site/index.html");

  try {
    problems.push(...validateFeatures(loadFeatures()).map((p) => `features.json: ${p}`));
  } catch (error) {
    problems.push(`features.json 读不了：${error.message}`);
  }

  if (problems.length) {
    console.error(`lint 不通过（${problems.length} 处）：\n- ${problems.join("\n- ")}`);
    return 1;
  }
  console.log(`lint 通过：${files.length} 个文件（密钥扫描、语法、交付设置、工作流、页面、功能清单）`);
  return 0;
}

if (process.argv[1] && path.resolve(process.argv[1]) === fileURLToPath(import.meta.url)) process.exit(main());
