export async function archiveSession({ $, archiveDir = "/workspace/.blitzlog", sessionId, directory }) {
  await $`mkdir -p ${archiveDir}`.quiet();
  const dest = `${archiveDir}/session-archive-${sessionId}.json`;
  await $`opencode export ${sessionId} > ${dest}`.quiet();
  const dir = directory || process.cwd();
  const branch = (await $`git -C ${dir} branch --show-current`.text()).trim();
  const commit = (await $`git -C ${dir} rev-parse HEAD`.text()).trim();
  const metadata = JSON.stringify({ sessionId, branch, commit, timestamp: Date.now() });
  await $`echo ${metadata} > ${archiveDir}/metadata.json`.quiet();
  return { dest, branch, commit };
}