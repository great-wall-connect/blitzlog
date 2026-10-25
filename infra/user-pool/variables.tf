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
  description = "Auth key for per-run EC2 enrollment in the user's Tailscale Tailnet. Generate at https://login.tailscale.com/admin/settings/keys with Ephemeral: enabled and Tags: tag:blitzlog-agent. The same key can be reused across many runs; rotate by re-running terraform apply. Leave empty to skip per-run enrollment (e.g. when using a static subnet router, AWS Client VPN)."
  type        = string
  default     = ""
  sensitive   = true
}

variable "opencode_model" {
  description = "This buyer's default OpenCode model, in `provider/model` form (e.g. anthropic/claude-sonnet-4). Empty means 'no per-user model' — the org default (var.opencode_model in the core module) is used instead. Issue #165: this is level 2 of the three-level model selection (task label > per-user default > org default)."
  type        = string
  default     = ""
}

variable "opencode_api_key" {
  description = "This buyer's own inference-provider credential (BYOK). Empty means 'no per-user credential' — the org credential is used instead. When set, this key supersedes the org credential and is bound to the selected model's provider: a mismatch fails the run fast rather than sending the key to the wrong provider. Stored as a SecureString; the Lambda never receives the key material, only the path the EC2 host reads it from."
  type        = string
  default     = ""
  sensitive   = true
}

variable "opencode_api_key_provider" {
  description = "The provider this buyer's credential belongs to (e.g. anthropic). Defaults to the provider prefix of opencode_model when that is set. Set it explicitly when opencode_model is empty — the binding rule needs a declared provider to check the credential against (e.g. a buyer with their own key and no model of their own, running the org default model on their own key)."
  type        = string
  default     = ""
}

variable "opencode_base_url" {
  description = "Optional base URL of a self-hosted / BYO inference endpoint (e.g. https://llm.internal/v1). Requires opencode_api_key — the org credential is never sent to a custom endpoint. Must resolve to a public IP or, with opencode_base_url_allow_private_cidrs = true, a private one. Loopback and link-local (the instance metadata service) are always rejected."
  type        = string
  default     = ""
}

variable "opencode_base_url_allow_private_cidrs" {
  description = "Set true when opencode_base_url resolves to a private IP (RFC1918, ULA, or Tailscale CGNAT 100.64.0.0/10) — e.g. a self-hosted endpoint on the user's Tailnet. Public endpoints need no opt-in."
  type        = bool
  default     = false
}
