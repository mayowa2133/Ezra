import assert from "node:assert/strict";
import { execFileSync } from "node:child_process";
import { mkdtempSync, mkdirSync, rmSync, writeFileSync } from "node:fs";
import { createRequire } from "node:module";
import { tmpdir } from "node:os";
import { join, resolve } from "node:path";
import { fileURLToPath } from "node:url";
import { test } from "node:test";
import { ESLint } from "eslint";

const require = createRequire(import.meta.url);
const { getRootDirs } = require("@next/eslint-plugin-next/dist/utils/get-root-dirs");
const nextPlugin = require("@next/eslint-plugin-next");

test("Next root directory discovery retains its supported glob behavior", (t) => {
  const root = mkdtempSync(join(tmpdir(), "ezra-next-lint-"));
  t.after(() => rmSync(root, { recursive: true, force: true }));
  for (const name of ["one", "two"]) mkdirSync(join(root, "apps", name), { recursive: true });
  writeFileSync(join(root, "apps", "not-a-directory"), "");
  // The engines format directory paths differently; Next consumes them via path.join.
  const discover = (rootDir) => getRootDirs({ cwd: root, settings: { next: { rootDir } } })
    .map((dir) => resolve(dir)).sort();
  const expected = [join(root, "apps", "one"), join(root, "apps", "two")].sort();

  assert.deepEqual(discover(undefined), [root]);
  assert.deepEqual(discover(join(root, "apps", "*")), expected);
  assert.deepEqual(discover(join(root, "apps", "{one,two}")), expected);
  assert.deepEqual(discover(expected), expected);
  assert.deepEqual(discover(join(root, "apps", "*").replaceAll("/", "\\")), expected);
  assert.deepEqual(discover(join(root, "missing", "*")), []);
});

test("deeply nested brace patterns do not exhaust the lint process stack", () => {
  // Run in a bounded child process so a regression cannot hang the test runner.
  execFileSync(process.execPath, ["-e", `
    const { getRootDirs } = require('@next/eslint-plugin-next/dist/utils/get-root-dirs');
    // 9,807 characters: below braces' 10,000-character limit, but enough nesting
    // to crash its recursive AST walker with a RangeError.
    const pattern = '{'.repeat(4900) + 'missing' + '}'.repeat(4900);
    getRootDirs({ cwd: process.cwd(), settings: { next: { rootDir: pattern } } });
  `], { cwd: fileURLToPath(new URL("..", import.meta.url)), timeout: 10000, stdio: "pipe" });
});

test("Next rules still find pages through a configured root directory glob", async (t) => {
  const root = mkdtempSync(join(tmpdir(), "ezra-next-rule-"));
  t.after(() => rmSync(root, { recursive: true, force: true }));
  const pages = join(root, "apps", "site", "pages");
  mkdirSync(pages, { recursive: true });
  writeFileSync(join(pages, "about.js"), "");
  const eslint = new ESLint({
    cwd: root,
    overrideConfigFile: true,
    overrideConfig: {
      languageOptions: { parserOptions: { ecmaFeatures: { jsx: true } } },
      plugins: { "@next/next": nextPlugin },
      settings: { next: { rootDir: join(root, "apps", "*") } },
      rules: { "@next/next/no-html-link-for-pages": "error" },
    },
  });
  const [result] = await eslint.lintText('const link = <a href="/about">About</a>;', {
    filePath: join(pages, "index.js"),
  });
  assert.equal(result.errorCount, 1);
  assert.equal(result.messages[0].ruleId, "@next/next/no-html-link-for-pages");
});
