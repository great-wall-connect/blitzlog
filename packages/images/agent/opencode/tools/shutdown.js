// Container-side tool: triggers the in-container assisted-shutdown.sh
// which writes session artifacts to /workspace/.blitzlog/ and touches
// /workspace/.shutdown. The host's watchdog polls /workspace/.shutdown
// and does the AWS cleanup (S3 upload + EC2 terminate + lock release).
// The container itself has NO AWS credentials.
export default {
  description:
    "Shut down this assisted agent instance. Writes session artifacts to /workspace/.blitzlog/ for the host to upload to S3, and signals the host watchdog to terminate the EC2 instance. Use when the user says they are done, wants to shut down, or no longer needs the agent.",
  args: {},
  async execute() {
    // Run assisted-shutdown.sh, which (in order):
    //   1. exports the session to /workspace/.blitzlog/session-archive-*.json
    //   2. writes /workspace/.blitzlog/metadata.json
    //   3. notifies the user via Telegram
    //   4. touches /workspace/.shutdown LAST, signaling the host
    // The touch is the LAST step so the host doesn't kill the container
    // mid-export. If we touched here first (as a previous version did),
    // the host's shutdown watcher (or production watchdog.sh) would
    // SIGTERM the container before the session could be archived.
    await Bun.$`_SHUTDOWN_REASON=agent_requested /usr/local/bin/assisted-shutdown.sh`;
    return "Shutdown signal sent. Host will finalize (S3 upload + terminate).";
  },
};
