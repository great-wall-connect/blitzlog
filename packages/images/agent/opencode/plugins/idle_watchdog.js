export const IdleWatchdog = async ({ $, client, directory }) => {
  const TELEGRAM_BOT_TOKEN = process.env.TELEGRAM_BOT_TOKEN;
  const TELEGRAM_USER_ID = process.env.TELEGRAM_USER_ID;

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
      await client.app.log({
        body: { service: "idle-watchdog", level: "info", message: `Autosave timer fired for session: ${sessionId}` },
      });
      const branch = (await $`git -C ${directory} branch --show-current`.text()).trim();
      if (!branch) return;
      const issueNumber = process.env.ISSUE_NUMBER || "unknown";
      const timestamp = new Date().toISOString().replace(/[:.]/g, "-");
      const autosaveBranch = `autosave/issue-${issueNumber}-${timestamp}`;
      await $`git -C ${directory} add -A`.quiet();
      await $`git -C ${directory} commit --no-verify -m ${"autosave: idle checkpoint"}`.quiet().catch(() => {});
      await $`git -C ${directory} branch -f ${autosaveBranch} HEAD`.quiet();
      await $`git -C ${directory} push --force --no-verify origin ${autosaveBranch}`.quiet();
      await $`git -C ${directory} checkout ${branch}`.quiet().catch(() => {});
      await client.app.log({
        body: { service: "idle-watchdog", level: "info", message: `Autosave pushed to ${autosaveBranch}` },
      });
    } catch (e) {
      try {
        await client.app.log({
          body: { service: "idle-watchdog", level: "error", message: `Autosave failed: ${e?.message || e}` },
        });
      } catch {}
    }
  }

  async function telegramPing() {
    if (!TELEGRAM_BOT_TOKEN || !TELEGRAM_USER_ID) return;
    try {
      await client.app.log({
        body: { service: "idle-watchdog", level: "info", message: `Telegram ping timer fired` },
      });
      const text = "\u{1FAE0} Assisted agent idle 35min. Will shut down in ~2h25m without activity.";
      await $`curl -s -X POST https://api.telegram.org/bot${TELEGRAM_BOT_TOKEN}/sendMessage -d chat_id=${TELEGRAM_USER_ID} -d text=${text}`.quiet();
    } catch (e) {
      try {
        await client.app.log({
          body: { service: "idle-watchdog", level: "error", message: `Telegram ping failed: ${e?.message || e}` },
        });
      } catch {}
    }
  }

  async function idleShutdown() {
    try {
      await client.app.log({
        body: { service: "idle-watchdog", level: "info", message: `Idle shutdown timer fired, initiating shutdown` },
      });
      await $`_SHUTDOWN_REASON=${"idle_timeout"} /usr/local/bin/assisted-shutdown.sh`;
    } catch (e) {
      try {
        await client.app.log({
          body: { service: "idle-watchdog", level: "error", message: `Idle shutdown failed: ${e?.message || e}` },
        });
      } catch {}
    }
  }

  try {
    await client.app.log({
      body: { service: "idle-watchdog", level: "info", message: "IdleWatchdog plugin initialized" },
    });
  } catch {}

  return {
    event: async ({ event }) => {
      const sessionId = event?.properties?.sessionID;

      if (event.type === "session.idle") {
        clearTimers();
        idleSessionId = sessionId;
        autosaveTimer = setTimeout(() => autosave(sessionId), 5 * 60 * 1000);
        pingTimer = setTimeout(() => telegramPing(), 35 * 60 * 1000);
        shutdownTimer = setTimeout(() => idleShutdown(), 3 * 60 * 60 * 1000);
        try {
          await client.app.log({
            body: { service: "idle-watchdog", level: "info", message: `Session idle: ${sessionId}. Timers started.` },
          });
        } catch {}
      }

      if (event.type === "message.part.updated" && idleSessionId) {
        try {
          await client.app.log({
            body: { service: "idle-watchdog", level: "info", message: `Session reactivated (message.part.updated): ${sessionId}. Timers cleared.` },
          });
        } catch {}
        clearTimers();
      }

      if (event.type === "session.deleted") {
        clearTimers();
      }
    },
  };
};
