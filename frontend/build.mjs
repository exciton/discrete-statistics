import * as esbuild from "esbuild";

const watch = process.argv.includes("--watch");
const options = {
  entryPoints: ["src/index.ts"],
  bundle: true,
  format: "esm",
  target: "es2022",
  minify: !watch,
  sourcemap: false,
  legalComments: "none",
  outfile:
    "../custom_components/discrete_statistics/frontend/discrete-statistics-card.js",
};

if (watch) {
  const ctx = await esbuild.context(options);
  await ctx.watch();
  console.log("watching...");
} else {
  await esbuild.build(options);
}
