variable "aws_region" {
  description = "AWS region for SSM parameters"
  type        = string
  default     = "ap-east-1"
}

variable "owner_login" {
  description = "GitHub login of the user who owns this bot pool"
  type        = string
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
