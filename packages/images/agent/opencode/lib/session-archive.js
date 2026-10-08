export async function archiveSession({ $, archiveDir = "/workspace/.blitzlog", sessionId, directory }) {
  await $`mkdir -p ${archiveDir}`.quiet();
  const dir = directory || process.cwd();
  const branch = (await $`git -C ${dir} branch --show-current`.text()).trim();
  const commit = (await $`git -C ${dir} rev-parse HEAD`.text()).trim();

  // Write metadata + working-tree WIP bundle BEFORE attempting
  // `opencode export`. The export can fail or yield 0 bytes on the
  // session-archive path (e.g., when the opencode server races the
  // session.deleted event during a tool-rejection episode). Writing
  // these first means the operator's recovery path is intact even when
  // the JSON export comes back empty. The host's watchdog uploads
  // /workspace/.blitzlog/* wholesale
  // (infra/packer/scripts-docker-ubuntu/watchdog.sh:243-250), so anything
  // we land in this directory lands in the operator's recovery path
  // automatically.
  const metadata = JSON.stringify({ sessionId, branch, commit, timestamp: Date.now() });
  await $`echo ${metadata} > ${archiveDir}/metadata.json`.quiet();

  try {
    // `add -A` stages every WIP file (tracked + untracked).
    // `diff --cached` exports the staged tree against HEAD. `--binary`
    // keeps binary hunks intact so test fixtures and prebuilt images
    // survive in the recovery artifact. `--unified=10` matches git's
    // default context so the patch applies cleanly.
    await $`git -C ${dir} add -A`.quiet();
    await $`git -C ${dir} diff --cached --binary --unified=10 > ${archiveDir}/working-tree.patch`.quiet();
  } catch {}

  // `opencode export` last, isolated in its own try/catch so a failure
  // here (truncated output, non-zero exit, server already torn down)
  // can no longer take metadata.json / working-tree.patch down with it.
  // The 0-byte session-archive.json file may still be uploaded alongside
  // a populated recovery bundle — that's better than today's behavior
  // where the empty archive masks an otherwise-recoverable run.
  try {
    await $`opencode export ${sessionId} > ${archiveDir}/session-archive-${sessionId}.json`.quiet();
  } catch {}

  return { dest: `${archiveDir}/session-archive-${sessionId}.json`, branch, commit };
}