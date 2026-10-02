export async function gitAutosave({ $, directory, commitMessage }) {
  const issueNumber = process.env.ISSUE_NUMBER || "unknown";
  const currentBranch = (await $`git -C ${directory} branch --show-current`.text()).trim();
  if (!currentBranch) return { branch: null, autosaveBranch: null };
  const autosaveBranch = `autosave/issue-${issueNumber}`;

  await $`git -C ${directory} add -A`.quiet();
  await $`git -C ${directory} commit --no-verify -m ${commitMessage}`.quiet().catch(() => {});
  await $`git -C ${directory} branch -f ${autosaveBranch} HEAD`.quiet();
  await $`git -C ${directory} push --force --no-verify origin ${autosaveBranch}`.quiet();
  await $`git -C ${directory} checkout ${currentBranch}`.quiet().catch(() => {});

  return { branch: currentBranch, autosaveBranch };
}