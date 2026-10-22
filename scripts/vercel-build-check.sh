#!/usr/bin/env bash
# Build the whole Vercel project on this machine with the Vercel CLI (`vercel build`) and
# check the output against Vercel's limits. Nothing is deployed and this checkout is not
# modified: the build runs in a temporary copy.
#
#   scripts/vercel-build-check.sh --offline         # no Vercel account or login needed
#   scripts/vercel-build-check.sh                   # a linked checkout (`vercel link`): pulls
#                                                   # the preview settings and env vars first
#   scripts/vercel-build-check.sh --offline --keep  # keep the build directory to inspect it
#
# Steps:
# 1. Copy what a deployment receives (git-tracked files plus untracked files that git does
#    not ignore, minus .vercelignore) into a temporary directory.
# 2. --offline: write a hand-written .vercel/project.json (projectId prj_local, orgId
#    team_local, settings.framework services) and use an empty CLI config directory, so no
#    login is used. This is undocumented: normally `vercel pull` writes that file after a
#    login. Linked: copy .vercel/project.json and run `vercel pull --environment=preview`.
# 3. Run `vercel build` (preview target) with Vercel CLI $VERCEL_CLI_VERSION (default
#    64.5.0, through npx). CLI 64.5.0 refuses `daemons` (DAEMONS_NOT_SUPPORTED): the script
#    then builds again with `daemons` removed from the copy's vercel.json and checks the
#    daemon's files instead. Vercel builds daemon images only in its own production builds.
#    Off Linux x86_64 (macOS, ARM) the api's build command (`migrate --on-build`) is left
#    out: the builder installs Linux x86_64 wheels that the local Python cannot import.
# 4. Report every function in .vercel/output: runtime, files and size, counting the files a
#    function maps in from outside its directory. Python: warn above 225 MiB (above it the
#    Python builder leaves dependencies out of the bundle and installs them at cold start, an
#    undocumented builder detail) and fail above 500 MiB (Vercel's limit for Python
#    functions). Other runtimes: fail above 250 MiB.
# 5. Report whether the crons and the daemons from vercel.json are in .vercel/output/config.json.
#
# The check never migrates a database: the build runs with MIGRATE_ON_BUILD=off, which wins
# over a pulled .vercel/.env.preview.local (CLI 64.5.0 loads it with dotenv, which does not
# override variables already set).
# Needs git, Node.js with npx, tar, and network access to the npm registry and PyPI.
# Exit status: 0 ok (warnings allowed), 1 the build or a check failed, 2 a setup problem,
# 64 a usage error. See docs/deploy-vercel.md.
set -euo pipefail

VERCEL_CLI_VERSION="${VERCEL_CLI_VERSION:-64.5.0}"
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
OFFLINE=0
KEEP=0
for arg in "$@"; do
  case $arg in
    --offline) OFFLINE=1 ;;
    --keep) KEEP=1 ;;
    -h | --help) awk 'NR == 1 { next } /^#/ { sub(/^# ?/, ""); print; next } { exit }' "$0"; exit 0 ;;
    *) echo "unknown argument: $arg (see --help)" >&2; exit 64 ;;
  esac
done

say() { printf '%s\n' "$*"; }
setup_error() { printf '✗ %s\n' "$*" >&2; exit 2; }

for tool in git node npx tar; do
  command -v "$tool" >/dev/null 2>&1 || setup_error "$tool is required"
done
git -C "$ROOT" rev-parse --is-inside-work-tree >/dev/null 2>&1 || setup_error "$ROOT is not a git checkout"
[ -f "$ROOT/vercel.json" ] || setup_error "no vercel.json in $ROOT"
if [ "$OFFLINE" = 0 ] && [ ! -f "$ROOT/.vercel/project.json" ]; then
  setup_error "this checkout is not linked to a Vercel project. Run 'npx vercel@$VERCEL_CLI_VERSION link' (needs a login), or pass --offline."
fi

WORK="$(mktemp -d "${TMPDIR:-/tmp}/swell-vercel-build.XXXXXX")"
SRC="$WORK/project"
cleanup() {
  if [ "$KEEP" = 1 ]; then
    # Linked: `vercel pull` wrote the Preview environment variables (secrets) here.
    rm -f "$SRC"/.vercel/.env.*.local
    say "build directory kept: $SRC"
  else
    rm -rf "$WORK"
  fi
}
trap cleanup EXIT
mkdir -p "$SRC/.vercel" "$WORK/cli-config"

# --- helpers (Node.js: the Vercel CLI needs it anyway) -------------------------------------
cat >"$WORK/list-files.cjs" <<'JS'
// Files a deployment receives: tracked + untracked-not-ignored, minus .vercelignore matches.
"use strict";
const { execFileSync } = require("node:child_process");
const fs = require("node:fs");
const path = require("node:path");
const [root, out] = process.argv.slice(2);
const lsFiles = (...args) =>
  execFileSync("git", ["-C", root, "ls-files", "-z", ...args], { maxBuffer: 1 << 30 })
    .toString("utf8")
    .split("\0")
    .filter(Boolean);
const candidates = lsFiles("--cached", "--others", "--exclude-standard");
const ignoreFile = path.join(root, ".vercelignore");
const ignored = fs.existsSync(ignoreFile)
  ? lsFiles("--cached", "--others", "--ignored", "--directory", `--exclude-from=${ignoreFile}`)
  : [];
const ignoredFiles = new Set(ignored.filter((p) => !p.endsWith("/")));
const ignoredDirs = ignored.filter((p) => p.endsWith("/"));
const keep = candidates.filter((p) => {
  if (ignoredFiles.has(p) || ignoredDirs.some((dir) => p.startsWith(dir))) return false;
  try {
    return !fs.lstatSync(path.join(root, p)).isDirectory(); // deleted files and submodules drop out
  } catch {
    return false;
  }
});
fs.writeFileSync(out, keep.map((p) => `${p}\0`).join(""));
console.log(`${keep.length} files (${candidates.length - keep.length} left out by .vercelignore or deleted)`);
JS

cat >"$WORK/edit-config.cjs" <<'JS'
// Edits the copy's vercel.json: "daemons" drops the daemons; "python-build-commands" drops
// the build commands of the Python services. Prints what it removed.
"use strict";
const fs = require("node:fs");
const [file, what] = process.argv.slice(2);
const config = JSON.parse(fs.readFileSync(file, "utf8"));
const removed = [];
if (what === "daemons") {
  removed.push(...Object.keys(config.daemons ?? {}));
  delete config.daemons;
} else if (what === "python-build-commands") {
  for (const [name, service] of Object.entries(config.services ?? {})) {
    if (["fastapi", "flask", "django"].includes(service.framework) && service.buildCommand) {
      removed.push(`${name}: ${service.buildCommand}`);
      delete service.buildCommand;
    }
  }
}
fs.writeFileSync(file, `${JSON.stringify(config, null, 2)}\n`);
console.log(removed.join("; ") || "(nothing)");
JS

cat >"$WORK/report.cjs" <<'JS'
// Function sizes and config checks for .vercel/output. Exit 1 when a check fails.
"use strict";
const fs = require("node:fs");
const path = require("node:path");
const [project, vercelJsonPath, daemonsBuilt, cliVersion] = process.argv.slice(2);
const MiB = 1024 * 1024;
const output = path.join(project, ".vercel", "output");
const problems = [];
const warnings = [];
const mib = (bytes) => `${(bytes / MiB).toFixed(1)} MiB`;

function walk(dir, visit, seen = new Set()) {
  const real = fs.realpathSync(dir);
  if (seen.has(real)) return;
  seen.add(real);
  for (const entry of fs.readdirSync(real)) {
    const full = path.join(real, entry);
    const st = fs.statSync(full, { throwIfNoEntry: false });
    if (!st) continue;
    if (st.isDirectory()) walk(full, visit, seen);
    else visit(full, st.size);
  }
}

const funcDirs = [];
(function find(dir) {
  for (const entry of fs.readdirSync(dir, { withFileTypes: true })) {
    const full = path.join(dir, entry.name);
    if (entry.name.endsWith(".func")) funcDirs.push(full);
    else if (entry.isDirectory()) find(full);
  }
})(output);

const byReal = new Map();
for (const dir of funcDirs) {
  const real = fs.realpathSync(dir);
  if (!byReal.has(real)) byReal.set(real, { names: [], real });
  byReal.get(real).names.push(path.relative(output, dir));
}

const functions = [];
for (const fn of byReal.values()) {
  const vc = JSON.parse(fs.readFileSync(path.join(fn.real, ".vc-config.json"), "utf8"));
  let bytes = 0;
  let files = 0;
  const groups = new Map();
  const add = (dest, size) => {
    bytes += size;
    files += 1;
    const parts = dest.split("/");
    const key = parts[0] === "_vendor" && parts.length > 2 ? `_vendor/${parts[1]}` : parts[0];
    groups.set(key, (groups.get(key) ?? 0) + size);
  };
  walk(fn.real, (full, size) => add(path.relative(fn.real, full), size));
  for (const [dest, src] of Object.entries(vc.filePathMap ?? {})) {
    const st = fs.statSync(path.resolve(project, src), { throwIfNoEntry: false });
    if (st && st.isFile()) add(dest, st.size);
  }
  functions.push({ ...fn, vc, bytes, files, groups });
}
functions.sort((a, b) => b.bytes - a.bytes);

console.log("▶ functions in .vercel/output (sizes include files mapped in from outside .func)");
if (functions.length === 0) problems.push("no functions in .vercel/output");
for (const fn of functions) {
  const runtime = String(fn.vc.runtime ?? "?");
  const python = runtime.startsWith("python");
  const limit = python ? 500 * MiB : 250 * MiB;
  let verdict = "ok";
  if (fn.bytes > limit) {
    verdict = `FAIL: over the ${mib(limit)} limit`;
    problems.push(`${fn.names[0]} is ${mib(fn.bytes)}, over the ${mib(limit)} ${python ? "Python " : ""}limit`);
  } else if (python && fn.bytes > 225 * MiB) {
    verdict = "WARN: over 225 MiB (dependencies would be installed at cold start)";
    warnings.push(`${fn.names[0]} is ${mib(fn.bytes)}, over 225 MiB`);
  }
  const extra = fn.names.length > 1 ? ` (+${fn.names.length - 1} routes sharing it)` : "";
  const duration = fn.vc.maxDuration ? `, maxDuration ${fn.vc.maxDuration} s` : "";
  console.log(`  ${fn.names[0]}${extra}`);
  console.log(`    ${runtime}${duration}: ${fn.files} files, ${mib(fn.bytes)}  ${verdict}`);
  if (python) {
    const top = [...fn.groups.entries()].sort((a, b) => b[1] - a[1]).slice(0, 10);
    console.log(`    largest parts: ${top.map(([k, v]) => `${k} ${mib(v)}`).join(", ")}`);
  }
}

const config = JSON.parse(fs.readFileSync(path.join(output, "config.json"), "utf8"));
const declared = JSON.parse(fs.readFileSync(vercelJsonPath, "utf8"));

console.log("▶ crons");
const builtCrons = config.crons ?? [];
for (const cron of declared.crons ?? []) {
  const found = builtCrons.some((c) => c.path === cron.path && c.schedule === cron.schedule);
  console.log(`  ${cron.path} at "${cron.schedule}" (UTC): ${found ? "in config.json" : "MISSING from config.json"}`);
  if (!found) problems.push(`cron ${cron.path} is not in .vercel/output/config.json`);
}
if (!(declared.crons ?? []).length) console.log("  (none declared)");

console.log("▶ services and routes");
for (const name of Object.keys(declared.services ?? {})) {
  const routed = (config.routes ?? []).some((r) => r.destination && r.destination.service === name);
  console.log(`  ${name}: ${routed ? "routed" : "NO route in config.json"}`);
  if (!routed) problems.push(`service ${name} has no route in .vercel/output/config.json`);
}

console.log("▶ daemons");
const daemons = Object.entries(declared.daemons ?? {});
if (!daemons.length) console.log("  (none declared)");
for (const [name, daemon] of daemons) {
  const root = path.resolve(project, daemon.root);
  const inside = root === project || root.startsWith(project + path.sep);
  const file = path.join(root, daemon.entrypoint);
  const exists = inside && fs.existsSync(file);
  const where = path.relative(project, file) || daemon.entrypoint;
  const inConfig = JSON.stringify(config).includes(`"${name}"`) && "daemons" in config;
  if (daemonsBuilt === "yes") {
    console.log(`  ${name}: ${inConfig ? "in config.json" : "NOT in config.json"}; ${where} ${exists ? "present" : "MISSING"}`);
    if (!inConfig) problems.push(`daemon ${name} is not in .vercel/output/config.json`);
  } else {
    console.log(
      `  ${name}: not built here (Vercel CLI ${cliVersion} refuses daemons; Vercel builds the image in` +
        ` production deployments only); ${where} ${exists ? "present" : "MISSING"}`,
    );
  }
  if (!inside) problems.push(`daemon ${name}: root ${daemon.root} is outside the project`);
  else if (!exists) problems.push(`daemon ${name}: ${where} does not exist, so its image cannot be built`);
}

for (const w of warnings) console.log(`⚠ ${w}`);
for (const p of problems) console.log(`✗ ${p}`);
console.log(problems.length ? "RESULT: FAILED" : `RESULT: ok${warnings.length ? ` (${warnings.length} warning(s))` : ""}`);
process.exit(problems.length ? 1 : 0);
JS

# --- 1. the copy -----------------------------------------------------------------------------
say "▶ copying the deployment's files to $SRC"
node "$WORK/list-files.cjs" "$ROOT" "$WORK/files.lst"
tar -C "$ROOT" --null -T "$WORK/files.lst" -cf - | tar -C "$SRC" -xf -

# --- 2. project settings ---------------------------------------------------------------------
vercel_cli() {
  if [ "$OFFLINE" = 1 ]; then
    npx --yes "vercel@$VERCEL_CLI_VERSION" "$@" --global-config "$WORK/cli-config"
  else
    npx --yes "vercel@$VERCEL_CLI_VERSION" "$@"
  fi
}
export VERCEL_TELEMETRY_DISABLED=1 NEXT_TELEMETRY_DISABLED=1 MIGRATE_ON_BUILD=off
if [ "$OFFLINE" = 1 ]; then
  say "▶ offline: hand-written .vercel/project.json (undocumented; no login used)"
  printf '%s\n' '{"projectId":"prj_local","orgId":"team_local","settings":{"framework":"services"}}' \
    >"$SRC/.vercel/project.json"
else
  say "▶ linked: vercel pull --environment=preview"
  cp "$ROOT/.vercel/project.json" "$SRC/.vercel/project.json"
  (cd "$SRC" && vercel_cli pull --yes --environment=preview) || setup_error "vercel pull failed (log in with 'npx vercel@$VERCEL_CLI_VERSION login')"
fi
# Outside Vercel's build image the Python builder installs Linux x86_64 wheels (its default
# target), which this machine's Python cannot import on macOS or ARM: a Python build command
# would fail here although it works on Vercel, so it is left out of the local build.
if [ "$(uname -s)" != Linux ] || [ "$(uname -m)" != x86_64 ]; then
  say "⚠ $(uname -s) $(uname -m): the Python dependencies are Linux x86_64 wheels; skipping the build command: $(node "$WORK/edit-config.cjs" "$SRC/vercel.json" python-build-commands)"
fi

# --- 3. the build ----------------------------------------------------------------------------
run_build() {
  local log="$WORK/build-$1.log" started=$SECONDS status=0
  say "▶ vercel build (Vercel CLI $VERCEL_CLI_VERSION, attempt $1)"
  (cd "$SRC" && vercel_cli build) 2>&1 | tee "$log" || status=$?
  say "  attempt $1: exit $status after $((SECONDS - started)) s"
  return "$status"
}
DAEMONS_BUILT=yes
BUILD_OK=1
if ! run_build 1; then
  if grep -q "DAEMONS_NOT_SUPPORTED" "$WORK/build-1.log"; then
    say "▶ this CLI cannot build daemons; building again without: $(node "$WORK/edit-config.cjs" "$SRC/vercel.json" daemons)"
    DAEMONS_BUILT=no
    rm -rf "$SRC/.vercel/output"
    run_build 2 || BUILD_OK=0
  else
    BUILD_OK=0
  fi
fi
if [ "$BUILD_OK" = 0 ]; then
  say "✗ vercel build failed (output above)."
  say "RESULT: FAILED"
  exit 1
fi

# --- 4 and 5. the report ---------------------------------------------------------------------
say "▶ build directory: $(du -sh "$SRC" 2>/dev/null | cut -f1)"
node "$WORK/report.cjs" "$SRC" "$ROOT/vercel.json" "$DAEMONS_BUILT" "$VERCEL_CLI_VERSION"
