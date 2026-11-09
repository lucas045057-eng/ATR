import assert from "node:assert/strict";
import { existsSync, mkdtempSync, readFileSync, rmSync } from "node:fs";
import http from "node:http";
import { tmpdir } from "node:os";
import path from "node:path";
import test from "node:test";
import { build, resolveCommit, resolveRepo, versionJson } from "../scripts/site-build.mjs";
import { rename } from "../scripts/set-repo.mjs";
import { startServer } from "../scripts/serve.mjs";

const SHA = "a".repeat(40);

test("commit 优先取 GITHUB_SHA，且必须是完整 40 位", () => {
  assert.deepEqual(resolveCommit({ GITHUB_SHA: SHA }), { commit: SHA, dirty: false });
  const fallback = resolveCommit({ GITHUB_SHA: "abc1234" }); // 短 SHA 不接受，退回 git HEAD 或全 0
  assert.match(fallback.commit, /^[0-9a-f]{40}$/);
  assert.notEqual(fallback.commit, "abc1234");
});

test("repo 取 GITHUB_REPOSITORY，否则读 ops/delivery.json", () => {
  assert.equal(resolveRepo({ GITHUB_REPOSITORY: "me/app" }), "me/app");
  assert.match(resolveRepo({}), /^[\w.-]+\/[\w.-]+$/);
});

test("version.json 的字段", () => {
  const v = versionJson({ GITHUB_SHA: SHA, GITHUB_REPOSITORY: "me/app" }, new Date("2026-11-04T00:00:00Z"));
  assert.deepEqual(v, { ok: true, name: "app", repo: "me/app", commit: SHA, dirty: false, built_at: "2026-11-04T00:00:00.000Z" });
});

test("build() 产出 index.html、.nojekyll、version.json（完整 SHA）", () => {
  const out = mkdtempSync(path.join(tmpdir(), "dist-"));
  try {
    const v = build(path.join(out, "dist"), { GITHUB_SHA: SHA, GITHUB_REPOSITORY: "me/app" });
    assert.equal(v.commit, SHA);
    for (const f of ["index.html", ".nojekyll", "version.json"]) assert.ok(existsSync(path.join(out, "dist", f)), f);
    const written = JSON.parse(readFileSync(path.join(out, "dist", "version.json"), "utf8"));
    assert.match(written.commit, /^[0-9a-f]{40}$/);
    assert.equal(written.commit, SHA);
  } finally {
    rmSync(out, { recursive: true, force: true });
  }
});

test("set-repo：改 repo 和 health.url，其它不动，且幂等", () => {
  const before = JSON.parse(readFileSync(new URL("../ops/delivery.json", import.meta.url), "utf8"));
  const after = rename(before, "Some-Owner/my-app");
  assert.equal(after.repo, "Some-Owner/my-app");
  assert.equal(after.health.url, "https://some-owner.github.io/my-app/version.json");
  assert.deepEqual({ ...after, repo: 0, health: 0 }, { ...before, repo: 0, health: 0 });
  assert.deepEqual(rename(after, "Some-Owner/my-app"), after);
  assert.throws(() => rename(before, "不合法"), /owner\/name/);
});

test("serve：正常取文件、404、../ 穿越被拒", async () => {
  const dir = mkdtempSync(path.join(tmpdir(), "serve-"));
  build(dir, { GITHUB_SHA: SHA, GITHUB_REPOSITORY: "me/app" });
  const { server, port } = await startServer(dir, 0);
  const status = (p) =>
    new Promise((resolve, reject) => {
      http.get({ host: "127.0.0.1", port, path: p }, (r) => (r.resume(), r.on("end", () => resolve(r.statusCode)))).on("error", reject);
    });
  try {
    assert.equal(await status("/"), 200);
    assert.equal(await status("/version.json"), 200);
    assert.equal(await status("/nope"), 404);
    assert.notEqual(await status("/../package.json"), 200);
    assert.notEqual(await status("/%2e%2e/ops/delivery.json"), 200);
    assert.notEqual(await status("/..%2f..%2fetc/passwd"), 200);
    assert.equal(await status("/%00"), 400);
  } finally {
    server.close();
    rmSync(dir, { recursive: true, force: true });
  }
});
