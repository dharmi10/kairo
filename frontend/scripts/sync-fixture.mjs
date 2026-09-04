// Copies backend/metrics/output/multi_seed_range.json into
// frontend/public/ so the dashboard can fetch it as a plain static asset.
//
// This is the ONE piece of dashboard data that is NOT read live from the
// API (see DECISIONS.md, "M8 dashboard: precomputed fixture, not a live
// sweep endpoint") -- the 20-seed range is expensive to compute (20 full
// 500-event simulation runs) and is explicitly meant to be committed
// context, not something the "Run simulation" button ever triggers.
//
// Runs automatically before `npm run dev` and `npm run build` (see
// package.json's predev/prebuild) specifically so the copy can never go
// silently stale: editing the source of truth in backend/ and then
// running the frontend always re-syncs, with no separate manual step to
// forget. A Node script rather than a shell `cp`/`copy` because this
// project's contributors are on both Windows (PowerShell) and POSIX
// shells -- fs.copyFileSync works identically on both.
import { copyFileSync, existsSync, mkdirSync } from "node:fs";
import { dirname, join } from "node:path";
import { fileURLToPath } from "node:url";

const here = dirname(fileURLToPath(import.meta.url));
const source = join(here, "..", "..", "backend", "metrics", "output", "multi_seed_range.json");
const destDir = join(here, "..", "public");
const dest = join(destDir, "multi_seed_range.json");

if (!existsSync(source)) {
  console.error(
    `[sync-fixture] ${source} does not exist.\n` +
      "Generate it first: cd backend && python -m metrics.multi_seed"
  );
  process.exit(1);
}

mkdirSync(destDir, { recursive: true });
copyFileSync(source, dest);
console.log(`[sync-fixture] ${source} -> ${dest}`);
