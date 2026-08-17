export const SessionArchive = async ({ project, client, $, directory }) => {
  const bucket = process.env.SESSION_ARCHIVE_BUCKET;
  if (!bucket) return {};

  const prefix = process.env.SESSION_ARCHIVE_PREFIX || "";

  async function archiveSession(sessionId) {
    try {
      const tmpFile = `/tmp/session-archive-${sessionId}.json`;
      await $`opencode export ${sessionId} > ${tmpFile}`.quiet();
      const s3Key = `${prefix}/sessions/${sessionId}.json`;
      await $`aws s3 cp ${tmpFile} s3://${bucket}/${s3Key}`.quiet();

      const branch = (await $`git -C ${directory} branch --show-current`.text()).trim();
      const commit = (await $`git -C ${directory} rev-parse HEAD`.text()).trim();
      const metadata = JSON.stringify({ sessionId, branch, commit, timestamp: Date.now() });
      await $`echo ${metadata} > /tmp/session-archive-metadata.json`.quiet();
      await $`aws s3 cp /tmp/session-archive-metadata.json s3://${bucket}/${prefix}/metadata.json`.quiet();

      await client.app.log({
        body: {
          service: "session-archive",
          level: "info",
          message: `Session archived: ${sessionId}`,
        },
      });
    } catch (e) {
      try {
        await client.app.log({
          body: {
            service: "session-archive",
            level: "error",
            message: `Failed to archive session: ${e?.message || e}`,
          },
        });
      } catch {}
    }
  }

  return {
    event: async ({ event }) => {
      const sessionId = event?.properties?.id;
      if (!sessionId) return;

      if (event.type === "session.created") {
        try {
          await client.app.log({
            body: {
              service: "session-archive",
              level: "info",
              message: `Session created: ${sessionId}`,
            },
          });
        } catch {}
      }

      if (event.type === "session.idle" || event.type === "session.compacted") {
        await archiveSession(sessionId);
      }

      if (event.type === "session.deleted") {
        try {
          await client.app.log({
            body: {
              service: "session-archive",
              level: "info",
              message: `Session deleted: ${sessionId}`,
            },
          });
        } catch {}
      }
    },
  };
};
