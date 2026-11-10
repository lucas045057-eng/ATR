import assert from "node:assert/strict";
import { spawnSync } from "node:child_process";
import { mkdtempSync, readFileSync, rmSync, writeFileSync } from "node:fs";
import { tmpdir } from "node:os";
import path from "node:path";
import test from "node:test";
import { fileURLToPath } from "node:url";
import { applyResult, digestOf, validate } from "../scripts/features.mjs";

const SCRIPT = path.resolve(path.dirname(fileURLToPath(import.meta.url)), "../scripts/features.mjs");
const item = (extra = {}) => ({ id: "F1", behavior: "页面能打开", verification: "node -e 'process.exit(0)'", state: "not_started", issue: null, ...extra });
const wrap = (...features) => ({ features });
const passing = (extra = {}) => {
  const base = item({ state: "passing", ...extra });
  return { ...base, verified: { at: "2026-11-04T00:00:00.000Z", digest: digestOf(base) } };
};

test("合格的清单没有问题", () => {
  assert.deepEqual(validate(wrap(item(), item({ id: "F2", state: "active", issue: 3 }))), []);
});

test("格式错误会被逐条指出", () => {
  assert.ok(validate({}).length);
  assert.ok(validate(wrap(item({ id: "feature-1" }))).some((p) => p.includes("id 要写成")));
  assert.ok(validate(wrap(item(), item())).some((p) => p.includes("重复")));
  assert.ok(validate(wrap(item({ state: "done" }))).some((p) => p.includes("state 只能是")));
  assert.ok(validate(wrap(item({ extra: 1 }))).some((p) => p.includes("不认识的字段")));
  assert.ok(validate(wrap(item({ issue: "#3" }))).some((p) => p.includes("issue")));
  assert.ok(validate(wrap(item({ behavior: " " }))).some((p) => p.includes("behavior")));
});

test("恒成功的 verification 不算验证", () => {
  for (const command of ["true", ":", "exit 0", " true ; "]) {
    assert.ok(validate(wrap(item({ verification: command }))).some((p) => p.includes("恒成功")), command);
  }
});

test("passing 必须有 verified，且 digest 对得上", () => {
  assert.deepEqual(validate(wrap(passing())), []);
  assert.ok(validate(wrap(item({ state: "passing" }))).some((p) => p.includes("passing 只能由")));
  const forged = { ...passing(), verified: { at: "x", digest: "0".repeat(64) } };
  assert.ok(validate(wrap(forged)).some((p) => p.includes("digest 对不上")));
});

test("passing 之后改了 behavior 或 verification，旧的 passing 立刻失效", () => {
  const done = passing();
  assert.ok(validate(wrap({ ...done, verification: "node -e 'process.exit(0)' # 改弱了" })).some((p) => p.includes("digest")));
  assert.ok(validate(wrap({ ...done, behavior: "范围缩小了" })).some((p) => p.includes("digest")));
});

test("applyResult：成功才变 passing；回归退回 active；其它状态失败不动", () => {
  const data = wrap(item(), item({ id: "F2", state: "active" }), passing({ id: "F3" }));
  applyResult(data, "F1", true, "2026-11-04T01:00:00.000Z");
  assert.equal(data.features[0].state, "passing");
  assert.equal(data.features[0].verified.digest, digestOf(data.features[0]));
  applyResult(data, "F2", false);
  assert.equal(data.features[1].state, "active");
  applyResult(data, "F3", false);
  assert.equal(data.features[2].state, "active");
  assert.equal(data.features[2].verified, undefined);
  assert.deepEqual(validate(data), []);
  assert.throws(() => applyResult(data, "F9", true), /没有功能项/);
});

// ---- 命令行 ----
function cli(file, ...args) {
  return spawnSync(process.execPath, [SCRIPT, ...args], { env: { ...process.env, FEATURES_FILE: file }, encoding: "utf8" });
}
function withFile(data, fn) {
  const dir = mkdtempSync(path.join(tmpdir(), "features-"));
  const file = path.join(dir, "features.json");
  writeFileSync(file, JSON.stringify(data));
  try {
    return fn(file, () => JSON.parse(readFileSync(file, "utf8")));
  } finally {
    rmSync(dir, { recursive: true, force: true });
  }
}

test("CLI：不能手工设置 passing；verify 成功后写入 passing", () => {
  withFile(wrap(item()), (file, read) => {
    const refused = cli(file, "set-state", "F1", "passing");
    assert.equal(refused.status, 1);
    assert.match(refused.stderr, /只能由 verify 写入/);
    assert.equal(read().features[0].state, "not_started");
    const ok = cli(file, "verify", "F1");
    assert.equal(ok.status, 0, ok.stderr);
    assert.equal(read().features[0].state, "passing");
    assert.equal(cli(file, "validate").status, 0);
    assert.equal(cli(file, "recheck").status, 0);
  });
});

test("CLI：recheck 会重跑 passing 项，命令坏了就失败；手工改 state 被 validate 拦住", () => {
  withFile(wrap(passing({ verification: "node -e 'process.exit(1)'" })), (file) => {
    const result = cli(file, "recheck");
    assert.equal(result.status, 1);
    assert.match(result.stderr, /F1/);
  });
  withFile(wrap(item({ state: "passing" })), (file) => {
    assert.equal(cli(file, "validate").status, 1);
    assert.equal(cli(file, "recheck").status, 1);
  });
});

test("CLI：verify 失败且原来是 passing → 退回 active", () => {
  const failing = passing({ verification: "node -e 'process.exit(3)'" });
  withFile(wrap(failing), (file, read) => {
    assert.equal(cli(file, "verify", "F1").status, 1);
    assert.equal(read().features[0].state, "active");
    assert.equal(read().features[0].verified, undefined);
  });
});
