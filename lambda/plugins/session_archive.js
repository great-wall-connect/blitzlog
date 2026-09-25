// Container-side plugin: writes session archives to /workspace/.blitzlog/
// (a host-mounted dir). The host's watchdog uploads them to S3 after the
// container exits — the container itself has no AWS credentials.
export const SessionArchive = async ({ project, client, $, directory }) => {
  async function archiveSession(sessionId) {
    try {
      const dest = `/workspace/.blitzlog/session-archive-${sessionId}.json`;
      await $`mkdir -p /workspace/.blitzlog`.quiet();
      await $`opencode export ${sessionId} > ${dest}`.quiet();

      const branch = (await $`git -C ${directory} branch --show-current`.text()).trim();
      const commit = (await $`git -C ${directory} rev-parse HEAD`.text()).trim();
      const metadata = JSON.stringify({
        sessionId,
        branch,
        commit,
        timestamp: Date.now(),
      });
      await $`echo ${metadata} > /workspace/.blitzlog/metadata.json`.quiet();

      await client.app.log({
        body: { service: "session-archive", level: "info", message: `Session archived: ${sessionId}` },
      });
    } catch (e) {
      try {
        await client.app.log({
          body: { service: "session-archive", level: "error", message: `Failed to archive session: ${e?.message || e}` },
        });
      } catch {}
    }
  }

  return {
    event: async ({ event }) => {
      const sessionId = event?.properties?.sessionID;
      if (!sessionId) return;
      if (event.type === "session.created") {
        try {
          await client.app.log({
            body: { service: "session-archive", level: "info", message: `Session created: ${sessionId}` },
          });
        } catch {}
      }
      if (event.type === "session.idle" || event.type === "session.compacted") {
        await archiveSession(sessionId);
      }
      if (event.type === "session.deleted") {
        await archiveSession(sessionId);
      }
    },
  };
};
