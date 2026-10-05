import { makeLogger } from "../lib/log.js";
import { gitAutosave } from "../lib/git-autosave.js";
import { telegramNotify } from "../lib/telegram.js";

export const IdleWatchdog = async ({ $, client, directory }) => {
  const log = makeLogger("idle-watchdog", client);

  let autosaveTimer = null;
  let pingTimer = null;
  let shutdownTimer = null;
  let idleSessionId = null;

  function clearTimers() {
    if (autosaveTimer) { clearTimeout(autosaveTimer); autosaveTimer = null; }
    if (pingTimer) { clearTimeout(pingTimer); pingTimer = null; }
    if (shutdownTimer) { clearTimeout(shutdownTimer); shutdownTimer = null; }
    idleSessionId = null;
  }

  async function autosave(sessionId) {
    try {
      await log("info", `Autosave timer fired for session: ${sessionId}`);
      const { autosaveBranch } = await gitAutosave({
        $, directory, commitMessage: "autosave: idle checkpoint",
      });
      if (!autosaveBranch) return;
      await log("info", `Autosave pushed to ${autosaveBranch}`);
    } catch (e) {
      await log("error", `Autosave failed: ${e?.message || e}`);
    }
  }

  async function telegramPing() {
    try {
      await log("info", `Telegram ping timer fired`);
      await telegramNotify({
        $,
        text: "\u{1FAE0} Assisted agent idle 35min. Will shut down in ~2h25m without activity.",
      });
    } catch (e) {
      await log("error", `Telegram ping failed: ${e?.message || e}`);
    }
  }

  async function idleShutdown() {
    try {
      await log("info", `Idle shutdown timer fired, initiating shutdown`);
      await $`_SHUTDOWN_REASON=${"idle_timeout"} /usr/local/bin/assisted-shutdown.sh`;
    } catch (e) {
      await log("error", `Idle shutdown failed: ${e?.message || e}`);
    }
  }

  await log("info", "IdleWatchdog plugin initialized");

  return {
    event: async ({ event }) => {
      const sessionId = event?.properties?.sessionID;

      if (event.type === "session.idle") {
        clearTimers();
        idleSessionId = sessionId;
        autosaveTimer = setTimeout(() => autosave(sessionId), 5 * 60 * 1000);
        pingTimer = setTimeout(() => telegramPing(), 35 * 60 * 1000);
        shutdownTimer = setTimeout(() => idleShutdown(), 3 * 60 * 60 * 1000);
        await log("info", `Session idle: ${sessionId}. Timers started.`);
      }

      if (event.type === "message.part.updated" && idleSessionId) {
        await log("info", `Session reactivated (message.part.updated): ${sessionId}. Timers cleared.`);
        clearTimers();
      }

      if (event.type === "session.deleted") {
        clearTimers();
      }
    },
  };
};