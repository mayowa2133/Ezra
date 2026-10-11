# Lint dependency override

`package.json` replaces `fast-glob` **only inside `@next/eslint-plugin-next`**
with the pinned `tinyglobby` package. The Next plugin uses its named `globSync`
export with `onlyDirectories: true` to resolve `settings.next.rootDir`.
Tinyglobby supports that interface and avoids the vulnerable
`fast-glob → micromatch → braces` dependency chain.

The [braces advisory](https://github.com/advisories/GHSA-vfj7-8cjw-p6xm)
affects all published braces versions through 3.0.3 and currently has no patched
release. This override removes the affected code rather than suppressing audit
results or downgrading Next's lint rules.

Run `npm run test:lint-deps` when updating the Next lint plugin or this override.
It exercises directory discovery, a real Next lint rule using a configured root
glob, and the deeply nested brace input that caused stack exhaustion. The
replacement covers the plugin's usage, not every fast-glob API; review new
plugin call sites on upgrades. Remove the override once the plugin's upstream
dependency chain no longer includes vulnerable braces.
