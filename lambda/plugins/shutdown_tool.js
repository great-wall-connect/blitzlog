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
    await Bun.$`touch /workspace/.shutdown`;
    await Bun.$`_SHUTDOWN_REASON=agent_requested /usr/local/bin/assisted-shutdown.sh`;
    return "Shutdown signal sent. Host will finalize (S3 upload + terminate).";
  },
};
