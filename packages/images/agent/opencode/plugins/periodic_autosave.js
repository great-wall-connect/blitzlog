import { makeLogger } from "../lib/log.js";
import { gitAutosave } from "../lib/git-autosave.js";

export const PeriodicAutosave = async ({ client, $, directory }) => {
  const log = makeLogger("periodic-autosave", client);
  let saveInterval = null;

  async function periodicSave() {
    try {
      const { autosaveBranch } = await gitAutosave({
        $, directory, commitMessage: "autosave: periodic checkpoint",
      });
      if (!autosaveBranch) return;
      await log("info", `Periodic autosave pushed to ${autosaveBranch}`);
    } catch (e) {
      await log("error", `Periodic autosave failed: ${e?.message || e}`);
    }
  }

  await log("info", "PeriodicAutosave plugin initialized");

  return {
    event: async ({ event }) => {
      const sessionId = event?.properties?.sessionID;

      if (event.type === "session.created") {
        saveInterval = setInterval(() => periodicSave(), 5 * 60 * 1000);
        await log("info", `Periodic autosave started for session: ${sessionId}`);
      }

      if (event.type === "session.deleted") {
        if (saveInterval) { clearInterval(saveInterval); saveInterval = null; }
      }
    },
  };
};