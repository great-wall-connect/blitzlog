export default {
  description:
    "Shut down this assisted agent instance. Archives the session, uploads logs to S3, and terminates the EC2 instance. Use when the user says they are done, wants to shut down, or no longer needs the agent.",
  args: {},
  async execute() {
    await Bun.$`_SHUTDOWN_REASON=agent_requested /usr/local/bin/assisted-shutdown.sh`;
    return "Shutdown initiated. Session archived, logs uploaded. Instance terminating.";
  },
}
