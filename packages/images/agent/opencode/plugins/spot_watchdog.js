export const SpotWatchdog = async ({ client, $, directory }) => {
  const bucket = process.env.SESSION_ARCHIVE_BUCKET;
  const prefix = process.env.SESSION_ARCHIVE_PREFIX || "";
  let pollInterval = null;
  let emergencySaveTriggered = false;

  async function emergencySave(sessionId) {
    if (emergencySaveTriggered) return;
    emergencySaveTriggered = true;
    try {
      await client.app.log({
        body: { service: "spot-watchdog", level: "warn", message: "Spot interruption detected — emergency save started" },
      });

      const issueNumber = process.env.ISSUE_NUMBER || "unknown";
      const timestamp = new Date().toISOString().replace(/[:.]/g, "-");
      const autosaveBranch = `autosave/issue-${issueNumber}-interruption-${timestamp}`;

      const branch = (await $`git -C ${directory} branch --show-current`.text()).trim();
      await $`git -C ${directory} add -A`.quiet();
      await $`git -C ${directory} commit --no-verify -m ${"autosave: spot interruption"}`.quiet().catch(() => {});
      await $`git -C ${directory} branch -f ${autosaveBranch} HEAD`.quiet();
      await $`git -C ${directory} push --force --no-verify origin ${autosaveBranch}`.quiet();
      if (branch) {
        await $`git -C ${directory} checkout ${branch}`.quiet().catch(() => {});
      }

      if (sessionId && bucket) {
        try {
          const tmpFile = `/tmp/session-interruption-${sessionId}.json`;
          await $`opencode export ${sessionId} > ${tmpFile}`.quiet();
          await $`aws s3 cp ${tmpFile} s3://${bucket}/${prefix}/sessions/${sessionId}.json`.quiet();
        } catch (e) {
          await client.app.log({
            body: { service: "spot-watchdog", level: "error", message: `Session archive failed during emergency save: ${e?.message || e}` },
          });
        }
      }

      await client.app.log({
        body: { service: "spot-watchdog", level: "warn", message: `Emergency save pushed to ${autosaveBranch}` },
      });
    } catch (e) {
      try {
        await client.app.log({
          body: { service: "spot-watchdog", level: "error", message: `Emergency save failed: ${e?.message || e}` },
        });
      } catch {}
    }
  }

  async function checkSpotInterruption(sessionId) {
    try {
      const result = await $`curl -sf -X PUT 'http://169.254.169.254/latest/api/token' -H 'X-aws-ec2-metadata-token-ttl-seconds: 60'`.text();
      const token = result.trim();
      if (!token) return;
      const action = await $`curl -sf -H ${"X-aws-ec2-metadata-token: " + token} http://169.254.169.254/latest/meta-data/spot/instance-action`.text().catch(() => "");
      if (action.trim()) {
        clearInterval(pollInterval);
        pollInterval = null;
        await emergencySave(sessionId);
      }
    } catch {}
  }

  try {
    await client.app.log({
      body: { service: "spot-watchdog", level: "info", message: "SpotWatchdog plugin initialized" },
    });
  } catch {}

  return {
    event: async ({ event }) => {
      const sessionId = event?.properties?.sessionID;

      if (event.type === "session.created") {
        pollInterval = setInterval(() => checkSpotInterruption(sessionId), 5000);
        try {
          await client.app.log({
            body: { service: "spot-watchdog", level: "info", message: `Spot interruption polling started for session: ${sessionId}` },
          });
        } catch {}
      }

      if (event.type === "session.deleted") {
        if (pollInterval) { clearInterval(pollInterval); pollInterval = null; }
      }
    },
  };
};
