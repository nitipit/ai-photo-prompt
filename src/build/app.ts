import * as esbuild from "esbuild";

const projectRoot = new URL("../../", import.meta.url);
const sourceRoot = new URL("src/app/templates/", projectRoot);
const outputRoot = new URL("dist/templates/", projectRoot);
const audioSourceRoot = new URL("design/audio/", projectRoot);
const audioOutputRoot = new URL("dist/audio/", projectRoot);
const approvedAudioFiles = [
  "ui-click.wav",
  "prompt-submit.wav",
  "countdown-tick.wav",
  "generation-complete.wav",
  "score-reveal.wav",
  "generation-error.wav",
] as const;

await Deno.remove(outputRoot, { recursive: true }).catch(() => undefined);
await Deno.mkdir(outputRoot, { recursive: true });
await Deno.remove(audioOutputRoot, { recursive: true }).catch(() => undefined);
await Deno.mkdir(audioOutputRoot, { recursive: true });

try {
  const modules = await typeScriptModules(sourceRoot);
  const bridges = modules.filter((module) =>
    module.pathname.endsWith(".bundle.ts")
  );

  await buildModules(modules.filter((module) => !bridges.includes(module)));
  await buildBundles(bridges);
  await publishApprovedAudio();
} finally {
  esbuild.stop();
}

async function buildModules(modules: URL[]): Promise<void> {
  if (modules.length === 0) {
    return;
  }

  await esbuild.build({
    bundle: false,
    ...browserBuildOptions(modules),
  });
}

async function buildBundles(bundles: URL[]): Promise<void> {
  if (bundles.length === 0) {
    return;
  }

  await esbuild.build({
    bundle: true,
    ...browserBuildOptions(bundles),
  });
}

function browserBuildOptions(modules: URL[]): esbuild.BuildOptions {
  return {
    entryPoints: modules.map((module) => module.pathname),
    format: "esm",
    outbase: sourceRoot.pathname,
    outdir: outputRoot.pathname,
    platform: "browser",
    target: "es2022",
  };
}

async function publishApprovedAudio(): Promise<void> {
  await Promise.all(
    approvedAudioFiles.map((filename) =>
      Deno.copyFile(
        new URL(filename, audioSourceRoot),
        new URL(filename, audioOutputRoot),
      )
    ),
  );
}

async function typeScriptModules(directory: URL): Promise<URL[]> {
  const modules: URL[] = [];
  for await (const entry of Deno.readDir(directory)) {
    const path = new URL(entry.name, directory);
    if (entry.isDirectory) {
      modules.push(
        ...await typeScriptModules(new URL(`${entry.name}/`, directory)),
      );
    } else if (
      entry.isFile && entry.name.endsWith(".ts") &&
      !entry.name.endsWith(".test.ts")
    ) {
      modules.push(path);
    }
  }
  return modules;
}
