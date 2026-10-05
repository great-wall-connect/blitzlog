import { makeLogger } from "../lib/log.js";
import { gitAutosave } from "../lib/git-autosave.js";

export const SpotWatchdog = async ({ client, $, directory }) => {
  const log = makeLogger("spot-watchdog", client);
  const signalPath = "/workspace/.interrupted";
  let pollInterval = null;
  let triggered = false;

  async function emergencySave(sessionId) {
    if (triggered) return;
    triggered = true;

    try {
      await log("warn", "Spot interruption signal received — emergency save started");

      // 1. Force a fresh git autosave (preserves work-in-progress on
      //    the autosave branch, force-overwriting the previous tip).
      await gitAutosave({
        $, directory, commitMessage: "autosave: spot interruption",
      });

      // 2. Trigger the same shutdown flow the user-invoked shutdown
      //    tool uses. assisted-shutdown.sh does:
      //      - exports session to /workspace/.blitzlog/session-archive-*.json
      //      - writes /workspace/.blitzlog/metadata.json with
      //        shutdownReason=spot_interruption
      //      - notifies Telegram
      //      - touches /workspace/.shutdown (host-side watcher detects
      //        this and SIGTERMs the container)
      // Single path for all shutdown triggers — the host's watchdog.sh
      // forks a [ -f .shutdown ] watcher that handles the SIGTERM kick
      // for both user-invoked and spot-interrupt shutdowns.
      await $`_SHUTDOWN_REASON=${"spot_interruption"} /usr/local/bin/assisted-shutdown.sh`.quiet();
    } catch (e) {
      await log("error", `Emergency save failed: ${e?.message || e}`);
    }
  }

  async function checkInterruptFlag(sessionId) {
    try {
      const fs = await import("fs/promises");
      await fs.access(signalPath);
      clearInterval(pollInterval);
      pollInterval = null;
      await emergencySave(sessionId);
    } catch {}
  }

  await log("info", "SpotWatchdog plugin initialized (watching /workspace/.interrupted)");

  return {
    event: async ({ event }) => {
      const sessionId = event?.properties?.sessionID;

      if (event.type === "session.created") {
        pollInterval = setInterval(() => checkInterruptFlag(sessionId), 5000);
        await log("info", `Spot-interrupt polling started for session: ${sessionId}`);
      }

      if (event.type === "session.deleted") {
        if (pollInterval) { clearInterval(pollInterval); pollInterval = null; }
      }
    },
  };
};