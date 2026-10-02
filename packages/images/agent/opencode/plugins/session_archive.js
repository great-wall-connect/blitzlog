import { makeLogger } from "../lib/log.js";
import { archiveSession } from "../lib/session-archive.js";

export const SessionArchive = async ({ project, client, $, directory }) => {
  const log = makeLogger("session-archive", client);

  async function writeArchive(sessionId) {
    try {
      await archiveSession({ $, sessionId, directory });
      await log("info", `Session archived: ${sessionId}`);
    } catch (e) {
      await log("error", `Failed to archive session: ${e?.message || e}`);
    }
  }

  return {
    event: async ({ event }) => {
      const sessionId = event?.properties?.sessionID;
      if (!sessionId) return;
      if (event.type === "session.created") {
        await log("info", `Session created: ${sessionId}`);
      }
      if (event.type === "session.idle" || event.type === "session.compacted") {
        await writeArchive(sessionId);
      }
      if (event.type === "session.deleted") {
        await writeArchive(sessionId);
      }
    },
  };
};