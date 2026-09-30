export const PeriodicAutosave = async ({ client, $, directory }) => {
  let saveInterval = null;

  async function periodicSave() {
    try {
      const issueNumber = process.env.ISSUE_NUMBER || "unknown";
      const autosaveBranch = `autosave/issue-${issueNumber}-latest`;

      const branch = (await $`git -C ${directory} branch --show-current`.text()).trim();
      await $`git -C ${directory} add -A`.quiet();
      await $`git -C ${directory} commit --no-verify --allow-empty -m ${"autosave: periodic checkpoint"}`.quiet().catch(() => {});
      await $`git -C ${directory} branch -f ${autosaveBranch} HEAD`.quiet();
      await $`git -C ${directory} push --force --no-verify origin ${autosaveBranch}`.quiet();
      if (branch) {
        await $`git -C ${directory} checkout ${branch}`.quiet().catch(() => {});
      }

      await client.app.log({
        body: { service: "periodic-autosave", level: "info", message: `Periodic autosave pushed to ${autosaveBranch}` },
      });
    } catch (e) {
      try {
        await client.app.log({
          body: { service: "periodic-autosave", level: "error", message: `Periodic autosave failed: ${e?.message || e}` },
        });
      } catch {}
    }
  }

  try {
    await client.app.log({
      body: { service: "periodic-autosave", level: "info", message: "PeriodicAutosave plugin initialized" },
    });
  } catch {}

  return {
    event: async ({ event }) => {
      const sessionId = event?.properties?.sessionID;

      if (event.type === "session.created") {
        saveInterval = setInterval(() => periodicSave(), 5 * 60 * 1000);
        try {
          await client.app.log({
            body: { service: "periodic-autosave", level: "info", message: `Periodic autosave started for session: ${sessionId}` },
          });
        } catch {}
      }

      if (event.type === "session.deleted") {
        if (saveInterval) { clearInterval(saveInterval); saveInterval = null; }
      }
    },
  };
};
