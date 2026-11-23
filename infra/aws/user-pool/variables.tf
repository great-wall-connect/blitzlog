variable "aws_region" {
  description = "AWS region for SSM parameters"
  type        = string
  default     = "ap-east-1"
}

variable "owner_login" {
  description = "GitHub login of the user who owns this bot pool"
  type        = string
}

variable "git_author_name" {
  description = "Git author name every agent run on this pool uses for its commits (EC2 runtime + GHA worker). Defaults to var.owner_login when unset. The default is the auditable operator identity for the pool — overrides should be rare. To attribute commits to a bot (e.g. for the canonical [skip ci] release-bot carve-out), set this to the bot's name and set git_author_email to the matching <login>@users.noreply.github.com form. Empty disables the audit-trail identity entirely (commits fall back to root@<hostname> on EC2 / runner hostname on GHA)."
  type        = string
  default     = ""
}

variable "git_author_email" {
  description = "Git author email every agent run on this pool uses for its commits (EC2 runtime + GHA worker). Defaults to '<owner_login>@users.noreply.github.com' when unset — the public-facing noreply form. To use the audit-friendly <id>+<login>@users.noreply.github.com form (find the id with 'gh api /users/<login> | jq .id'), override here. Must be set when git_author_name is set explicitly."
  type        = string
  default     = ""
}

variable "telegram_bot_tokens" {
  description = "Map of Telegram bot names to tokens for this user's pool"
  type        = map(string)
  sensitive   = true
}

variable "telegram_allowed_user_id" {
  description = "Telegram user ID allowed to interact with this user's bots"
  type        = string
}

variable "local_llm_endpoint" {
  description = "Base URL of a local LLM the user's agents should reach (e.g. http://100.x.y.z:11434). Must resolve to a private IP. Public IPs are hard-rejected."
  type        = string
  default     = ""
}

variable "local_llm_model" {
  description = "Model id served by the local LLM endpoint (e.g. qwen2.5-coder:32b). Used as the suffix of OPENCODE_MODEL=local/<model> when the local LLM is configured."
  type        = string
  default     = ""
}

variable "local_llm_api_key" {
  description = "Optional API key for the local LLM endpoint. Empty is allowed for fully open endpoints (Ollama with no auth)."
  type        = string
  default     = ""
  sensitive   = true
}

variable "local_llm_endpoint_allow_private_cidrs" {
  description = "Required true when local_llm_endpoint resolves to a private IP (RFC1918, ULA, or Tailscale CGNAT 100.64.0.0/10). Set true only if you intentionally serve the LLM on a private network reachable from the EC2 instance via VPN/Tailnet/VPC."
  type        = bool
  default     = false
}

variable "local_llm_fallback" {
  description = "Behaviour when the local LLM is unreachable in assisted mode. closed = retry/abort only; cloud = also offer cloud fallback on Telegram. Ignored in autonomous mode (always closed)."
  type        = string
  default     = "closed"

  validation {
    condition     = contains(["closed", "cloud"], var.local_llm_fallback)
    error_message = "Must be 'closed' or 'cloud'."
  }
}

variable "tailscale_auth_key" {
  description = "Auth key for per-run EC2 enrollment in the user's Tailscale Tailnet. Generate at https://login.tailscale.com/admin/settings/keys with Ephemeral: enabled and Tags: tag:blitzlog-agent. The same key can be reused across many runs; rotate by re-running terraform apply. Leave empty to skip per-run enrollment (e.g. when using a static subnet router or AWS Client VPN)."
  type        = string
  default     = ""
  sensitive   = true
}
