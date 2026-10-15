// Bundle the UI into web/dist (index.html + assets/), which `clif-dash serve` serves.
//   cd web && npm install && npm run build      (npm run watch rebuilds on change)
import * as esbuild from "esbuild";
import { mkdirSync, writeFileSync } from "node:fs";
import { dirname, join } from "node:path";
import { fileURLToPath } from "node:url";

const here = dirname(fileURLToPath(import.meta.url));
const dist = join(here, "dist");
mkdirSync(join(dist, "assets"), { recursive: true });
writeFileSync(join(dist, "index.html"), `<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>ICU Respiratory Failure Quality</title>
<link rel="stylesheet" href="/assets/app.css">
</head>
<body>
<div id="root"></div>
<script src="/assets/app.js"></script>
</body>
</html>
`);

const options = {
  entryPoints: { app: join(here, "src", "app.jsx") },
  bundle: true,
  minify: true,
  sourcemap: true,
  outdir: join(dist, "assets"),
  format: "iife",
  target: ["es2020"],
  jsx: "automatic",
  define: { "process.env.NODE_ENV": '"production"' },
  legalComments: "none",
};
if (process.argv.includes("--watch")) {
  const ctx = await esbuild.context(options);
  await ctx.watch();
  console.log("Watching web/src for changes");
} else {
  await esbuild.build(options);
  console.log("Wrote web/dist");
}
