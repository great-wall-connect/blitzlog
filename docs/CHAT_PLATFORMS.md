# Chat platform support for enterprise adoption

**Status:** Evaluation / RFC — no production code changes.
**Issue:** [#189](https://github.com/great-wall-connect/blitzlog/issues/189)

## Executive summary

Blitzlog's only chat platform today is Telegram, and the integration is
hard-coded end-to-end. Telegram is not an enterprise surface, so this blocks
orgs from adopting Blitzlog. This document evaluates the alternatives and
recommends a path.

**The recommendations:**

1. **Adopt the Agent Client Protocol (ACP) as the agent-facing boundary.**
   ACP is a standard protocol; OpenCode ships native support (`opencode acp`).
   Choosing ACP means each additional chat platform is *adapter-only* work
   instead of a rewrite, and it decouples the chat surface from the agent
   surface so we can support multiple harnesses over one integration.

2. **Ship Slack first.** Slack's Socket Mode is a pure outbound WebSocket,
   which mirrors Blitzlog's current long-polling model exactly and fits the
   ephemeral-per-issue EC2 model with no new infrastructure.

3. **Treat Microsoft Teams as out of scope for now — on policy grounds, not
   technical ones.** Teams is technically supportable via ACP and existing
   gateway adapters. It is excluded because the platform's own limitations
   conflict with how Blitzlog agents behave. See [Why not
   Teams](#why-not-teams).

4. **Select the platform at the organization level; lease the bot per user
   within it.** The org picks the platform as a policy. Individual engineers
   keep their own leased bot identity inside it, preserving today's isolation
   guarantees and lease semantics.

5. **Keep Telegram as a first-class fallback.** It costs only the adapter we
   already have, and it preserves a zero-procurement onboarding path for
   contributors and small teams that will never pass a security review.

The most important thing this document establishes is that **the current
Telegram integration cannot simply be extended** — the conversational
experience lives entirely in a third-party npm package, and the platform is
baked into roughly eight separate seams across the Lambda, the bootstrap, the
container, and the Terraform. The work is a refactor with a new capability
attached, not a configuration change.

---

## 1. Current state

### 1.1 Blitzlog does not implement a chat bot

This is the single most important framing for the rest of the document.
Blitzlog **orchestrates** a third-party Telegram bot —
[`@grinev/opencode-telegram-bot`](https://www.npmjs.com/package/@grinev/opencode-telegram-bot) —
installed globally in the agent image:

```dockerfile
# packages/images/agent/Dockerfile:129-136
RUN npm config set prefix /usr/local \
    && npm install -g \
        opencode-ai@${OPENCODE_AI_VERSION} \
        @grinev/opencode-telegram-bot@${OPENCODE_TELEGRAM_BOT_VERSION} \
```

and started by the entrypoint:

```sh
# packages/images/agent/entrypoint.sh:944
setsid npx -y @grinev/opencode-telegram-bot@latest start >>/var/log/blitzlog/telegram-bot.log 2>&1 &
```

Everything a user experiences — the `/projects` and `/status` commands,
streaming replies, inline keyboards, voice-note transcription, project/session
selection — lives in that upstream package. Blitzlog only pre-selects the
project and session by writing the upstream package's config file:

```sh
# packages/images/agent/entrypoint.sh (settings.json write)
cat > "$HOME/.config/opencode-telegram-bot/settings.json" <<SETTINGS_EOF
{"currentProject": $PROJECT_JSON, "currentSession": $SESSION_TITLE}
SETTINGS_EOF
```

**Consequence:** adding Slack or Teams does not mean "writing another
connector." It means writing a new conversational client, because there is no
Blitzlog-side abstraction of the conversation.

### 1.2 The eight Telegram seams

There is **no chat-platform abstraction layer** in Blitzlog. Telegram is
hard-coded at every layer. (Compare this with the cloud-provider abstraction
in `lambda/provider/`, which is a good one and is the template for what we
should build.)

| # | Seam | Location | Telegram-specific shape |
|---|---|---|---|
| 1 | SSM namespace | `lambda/bot_pool.py:47,56` | `/blitzlog/users/<login>/telegram/pool/*`, `.../telegram/allowed-user-id` |
| 2 | Function names | `lambda/bot_pool.py` | `get_telegram_user_id`, `parse_telegram_decision` (reads `result[].callback_query.data`) |
| 3 | Lease return type | `lambda/bot_pool.py:276` | `tuple[str, str]` = `(bot_name, token)` — no platform dimension |
| 4 | User-data kwargs | `lambda/scripts/{aws,alicloud}/assisted.py:108` | flat `bot_name` / `bot_token` / `telegram_user_id` |
| 5 | Env var names | `assisted.py:157-159`, `watchdog.sh:242` | `TELEGRAM_BOT_TOKEN` / `TELEGRAM_USER_ID` / `TELEGRAM_BOT_NAME` |
| 6 | Container bot config | `entrypoint.sh:857-871` | writes `~/.config/opencode-telegram-bot/.env` + `settings.json` (upstream schema) |
| 7 | Notification calls | `entrypoint.sh:917,927`, `assisted-shutdown.sh:37`, `opencode/lib/telegram.js` | raw `curl api.telegram.org/bot{token}/sendMessage` |
| 8 | Bot runtime | `Dockerfile:129-136`, `entrypoint.sh:944` | `npm i -g @grinev/opencode-telegram-bot` + `npx ... start` |

Seam 8 is the crux; seams 1–7 are the work.

### 1.3 The one platform-neutral seam that already exists

The bot talks to OpenCode over a local HTTP/SSE API:

```
opencode serve  →  http://127.0.0.1:4096  (basic auth)
```

configured via `OPENCODE_API_URL`, `OPENCODE_SERVER_USERNAME`,
`OPENCODE_SERVER_PASSWORD` in the container's bot `.env`
(`entrypoint.sh:860-862`). Blitzlog's own plugins use the same API. This
contract is the closest thing to a natural integration boundary, and it is
what a multi-platform design should be built around — or replaced with
something more standard, which is what ACP proposes (§3).

### 1.4 Supporting facts worth carrying forward

- **Bot leasing.** `acquire_bot_token()` (`lambda/bot_pool.py:230`) leases a
  bot from the user's pool using an S3 lock with a 4-hour TTL
  (`bot-pool-locks/<login>/<bot>.json`), released by the host watchdog on
  container exit (`watchdog.sh:513-518`). The lock key is **per-user**.
- **The Lambda never receives chat events.** The GitHub webhook is the only
  inbound trigger. Chat is outbound-only from the Lambda's perspective; the
  bot polls (Telegram) or holds a socket (Slack).
- **Notifications are raw API calls**, duplicated in four places, all
  Telegram-specific: the "agent ready" banner, the "cannot be started"
  failure banner, the idle-watchdog ping (`opencode/lib/telegram.js`), and the
  shutdown notice.
- **Secrets travel in plaintext through user-data.** `assisted.py:158`
  interpolates the raw bot token into the generated EC2 user-data script and
  into `/etc/blitzlog.env`. Slack's credential model is *two* tokens
  (`xoxb-` bot + `xapp-` app token), which compounds this. See
  [Risks](#7-risks).
- **A `getUpdates` race exists.** `entrypoint.sh:519` polls
  `api.telegram.org/.../getUpdates` for the local-LLM fallback decision while
  the upstream bot is polling the same token's update queue. Both consume the
  same queue; the `offset=-1` semantics make this lossy.

---

## 2. Platform comparison

| Dimension | Telegram (current) | Slack | Teams |
|---|---|---|---|
| SDK quality in this stack | N/A (3rd-party pkg) | `@slack/bolt` — excellent TS | `@microsoft/teams-ai` v2 — good TS |
| Transport | Long-poll (outbound) | **Socket Mode** (outbound WS, no inbound ports) | **HTTPS inbound webhook — public endpoint required** |
| Fits ephemeral-EC2 model | Yes | **Yes** — mirrors polling exactly | **No** — needs persistent ingress |
| Credential model | 1 token | `xoxb-` bot + `xapp-` app token | `CLIENT_ID`/`SECRET`/`TENANT_ID` + Entra token refresh |
| Install friction | BotFather (~1 min) | OAuth to workspace | Entra app registration + manifest + admin consent |
| Identity model | Numeric user ID | `U…` user / `C…` channel / `T…` team | AAD GUIDs |
| UI primitives | Inline keyboards | Block Kit | Adaptive Cards |
| Streaming | Live message edits | Native `chat.startStream`/`appendStream`/`stopStream` | 1 update/sec, **2-min hard cap**, **personal chats only** |
| Message mutation | Editable | Editable | **Immutable** (`updateActivity` unsupported) |
| File attachments | Yes | Yes | **No** in the M365 Copilot channel |
| Proactive notifications | Yes | Yes | **No** in the M365 Copilot channel |
| Third-party model support | Yes | Yes | **Tenant-admin gated** ("Agent Blocked") |
| Rate limit | ~30 msg/s | 1 msg/s per method per channel | 1 msg/s, ~30 msg/min per conversation |
| Enterprise fit | Poor | **Good** | **Good** for IT governance, poor for agent extensibility |

### 2.1 Transport is the primary architectural discriminator

This matters more than feature parity because of how Blitzlog runs.

Blitzlog's assisted mode spawns **an ephemeral EC2 box per issue**, with no
ingress and no persistent component. The agent container dials *out* to
`api.telegram.org` today.

- **Slack's Socket Mode** is a pure outbound WebSocket from the container to
  Slack. A self-contained box connects exactly as it connects to Telegram
  today. **No new infrastructure.**
- **Teams requires a public HTTPS endpoint.** A Teams bot must accept inbound
  requests. That endpoint has to outlive the ephemeral box, which means a
  persistent webhook service (API Gateway → Lambda, or a relay) plus a
  mechanism for routing an inbound event to the *specific* running agent that
  owns that conversation. That inverts Blitzlog's deliberately stateless,
  one-shot model and adds a stateful component to the system.

### 2.2 Gap asymmetry

Microsoft's own published gap analysis rates **10 RED gaps for Slack → Teams
and 0 for Teams → Slack**. Teams is the harder migration target in both
directions of porting effort. Combined with §2.1, this means Teams is the
wrong *first* platform even setting aside policy.

---

## 3. The protocol question

The most consequential choice in this document is not "which chat platform" but
**"what interface sits between the chat surface and the agent."**

There are three candidate shapes:

### Option A — Platform-coupled SDK (what we would get from a first-party Slack package)

`@opencode-ai/slack` is a first-party OpenCode package (MIT, in the upstream
monorepo at `packages/slack/`) built on `@slack/bolt`. It is a good piece of
work — Socket Mode, `app_mention` + DM + `/opencode` slash command, buttons,
modals, threaded sessions, streaming.

But it bundles **two concerns together**: it is a Slack client *and* an
OpenCode client. Swapping Slack for another platform means rewriting the
agent half too. The coupling is structural, not incidental.

### Option B — Blitzlog's own HTTP/SSE client (status quo, generalized)

Keep talking to `opencode serve` over its HTTP/SSE API, and write one chat
client per platform that maps HTTP/SSE events to that platform's UI.

This is what the current Telegram bot does (the upstream package talks to
`OPENCODE_API_URL`). It works, and it is well-understood. Its downside is that
every platform needs a bespoke client, and the *agent-facing* half is
re-implemented per platform rather than shared.

### Option C — Agent Client Protocol (recommended)

[ACP](https://opencode.ai/docs/acp/) is an open protocol for driving AI
coding agents. OpenCode ships native support:

```sh
opencode acp    # speaks ACP over stdin/stdout, JSON-RPC, protocol version 1
```

The agent half becomes protocol-standard, so the chat half only has to know
how to render ACP's `session/update` notifications:

| ACP notification | Chat-side rendering |
|---|---|
| `agent_message_chunk` | streamed text |
| `agent_thought_chunk` | reasoning/status |
| `tool_call` / `tool_call_update` | tool-call bubbles with inputs/outputs/diffs |
| `plan` | todo list |
| `usage_update` | token/cost meter |
| `available_commands_update` | command palette |
| `session/request_permission` | approve/deny buttons |
| `elicitation/create` | question form |

With ACP, adding a platform means writing one adapter — and the same adapter
works for **any** ACP-speaking harness (Claude Code, Codex, Gemini CLI), not
just OpenCode.

### 3.1 ACP is real and already widely implemented

This is not a speculative protocol. OpenCode's `initialize` response advertises:

```json
{
  "loadSession": true,
  "mcpCapabilities": { "http": true, "sse": false },
  "promptCapabilities": { "embeddedContext": true, "image": true },
  "sessionCapabilities": {
    "additionalDirectories": {}, "close": {}, "delete": {},
    "fork": {}, "list": {}, "resume": {}
  }
}
```

And the multi-platform gateway ecosystem is already substantial — each of
these implements the chat-adapter pattern over ACP:

| Project | Platforms | Notes |
|---|---|---|
| [`agent-anywhere`](https://github.com/l0ng-ai/agent-anywhere) | Discord, Telegram, **Slack**, Lark, QQ, LINE, WeCom, DingTalk | MIT; Satori-based platform adapters; `opencode acp` preset built in |
| [`openab`](https://github.com/openabdev/openab) | Discord, Slack (+ gateway adapters for Telegram, LINE, Lark, Teams, Google Chat, WeCom) | Rust; ACP over stdio JSON-RPC; K8s-ready |
| [`txtcode`](https://github.com/IMvision12/txtcode) | WhatsApp, Telegram, Discord, Slack, **Teams**, Signal | Slack via Socket Mode; Teams via Bot Framework |
| [`tandem`](https://github.com/jakejimenez/tandem) | Slack, Mattermost | Per-thread sessions, permission modes, worktree isolation |
| [`ode`](https://github.com/odefun/ode) | Slack, Discord, Lark | Session↔thread mapping, per-user git attribution |
| [`ductor`](https://github.com/PleasePrompto/ductor) | Telegram, Matrix, Slack | Modular transport layer |
| [`openacp.ai`](https://openacp.ai/) | Telegram, Discord, Slack | 28+ ACP agents |

**We should not be the project that writes a ninth chat adapter library.** We
should adopt the platform-adapter layer and own the parts that are genuinely
Blitzlog's.

---

## 4. Critical gaps that shape the design

Two verified findings determine the architecture. Both are specific to how
OpenCode behaves under ACP, and both are solvable — but only if we own the
agent-facing layer.

### 4.1 `opencode acp` cannot attach to an existing `opencode serve`

Verified in upstream source for both major versions:

- **V1** (`packages/opencode/src/cli/cmd/acp.ts`): unconditionally calls
  `Server.listen(opts)`. The `Flag` module enumerates every environment
  variable OpenCode reads — **there is no `OPENCODE_URL` or
  `OPENCODE_API_URL`**. (`opencode attach <url>` exists, but it is a **TUI**
  command, not an ACP client.)
- **V2** (`packages/cli/src/commands/commands.ts`): a global `--server` flag
  exists for `run`, `plugin list`, `auth list` and others — but the `acp`
  command spec **does not** include `globalFlags: ServerFlags`. V2's ACP
  always spawns a private `opencode serve --stdio --port 0` child.

**Implication:** a chat client speaking ACP drives a *separate* OpenCode
instance from the `:4096` server Blitzlog's plugins and SDK consumers use.
Blitzlog would run two OpenCode runtimes.

This is not fatal, but it must be a deliberate decision rather than a
surprise. The mitigating factor is that **plugins still load under ACP** —
V1's ACP creates a per-directory instance (so per-directory plugins load), and
V2's ACP explicitly polls `client.plugin.list({location: {directory: cwd}})`
until it returns a non-empty list. So Blitzlog's plugin mechanism remains
available under ACP.

### 4.2 OpenCode does not emit permission requests under ACP

This is the blocker for Blitzlog's assisted-mode approval gate.

The `agent-anywhere` project probed this live (2026-09-11) and recorded the
result:

> `opencode` 1.18.27 sends no reverse request of any kind (not even
> `session/request_permission`), and its model asks in prose and ends the turn
> instead.

Under ACP, OpenCode does not surface the approval requests that the HTTP/SSE
path does. Blitzlog's assisted mode is *interactive by definition* — the human
approves, redirects, and answers questions while the agent works. Losing that
gate would turn assisted mode into an unattended agent with a chat window.

**The escape hatch is the plugin API.** Blitzlog's plugins *do* load under
ACP (§4.1), and the V1 plugin API exposes exactly the hook needed:

```ts
// @opencode-ai/plugin (V1) — Hooks
"permission.ask"?: (input: Permission, output: {status: "ask" | "deny" | "allow"}) => Promise<void>;
```

So the permission bridge is: **the plugin raises the permission request; the
Blitzlog ACP client intercepts it and renders it as chat buttons.** This is
precisely the kind of thing the plugin API is for, and it is why owning the
agent-facing layer is worth the cost.

### 4.3 Adopted gateways auto-approve — which Blitzlog cannot ship

`agent-anywhere`'s permission policy is documented bluntly in its own config
schema:

> *"NOTE: there is no per-call permission policy here. As the ACP client, the
> daemon auto-approves every tool request (`session/request_permission`) —
> agents run with full tool access."*

```ts
export function decidePermission(req: RequestPermissionRequest): RequestPermissionResponse {
  const allow = pick((o) => o.kind === 'allow_once') ?? pick((o) => o.kind.startsWith('allow'));
  return allow ? { outcome: { outcome: 'selected', optionId: allow } } : { outcome: { outcome: 'cancelled' } };
}
```

Its only access control is `access.allowFrom` (who may trigger the agent at
all) — a coarse gate, not a per-action approval.

Blitzlog cannot adopt this as-is. Assisted mode's value proposition is a human
in the loop with a veto. **We adopt the platform adapters, not the permission
policy.**

---

## 5. Target architecture

### 5.1 Shape

```
┌─────────────────────────────────────────────────────────────────┐
│  EC2 box (ephemeral, one per GitHub issue)                       │
│                                                                 │
│   ┌────────────────┐   ACP (stdio JSON-RPC)   ┌──────────────┐  │
│   │ chat client    │ ───────────────────────► │ opencode acp │  │
│   │ (Slack,        │ ◄─────────────────────── │              │  │
│   │  Discord,      │   session/update stream  └──────────────┘  │
│   │  Telegram)     │                                            │
│   └────────────────┘                                            │
│         ▲  owned by Blitzlog                                    │
│         │                                                       │
│   ┌─────┴──────────────────────────────────────────────────┐   │
│   │ Blitzlog agent runtime                                  │   │
│   │  - ACP client: session/new, session/prompt, cwd binding │   │
│   │  - permission bridge: plugin hook ⇄ chat buttons        │   │
│   │  - lease/isolation: S3 lock, one bot per user per issue │   │
│   │  - issue binding: thread ⇄ GitHub issue ⇄ session       │   │
│   │  - notify lifecycle: ready / idle-35min / shutdown      │   │
│   │  - commit attribution (#119)                             │   │
│   │  - STT: existing Whisper shim → agent_message_chunk     │   │
│   └─────────────────────────────────────────────────────────┘   │
└─────────────────────────────────────────────────────────────────┘
```

### 5.2 What we own vs. what we adopt

This is the section that determines the work estimate.

| Layer | Decision | Rationale |
|---|---|---|
| **Platform adapters** (Slack, Discord, …) | **Adopt** | Mature, capability-based, already 8 platforms. Writing a tenth library is waste. |
| **ACP client** (session/new, prompt, update consumption) | **Own** | Small, and it is where issue binding, `cwd`/worktree seeding, and session lifecycle live — all Blitzlog-specific. |
| **Permission bridge** | **Own** | Adopted gateways auto-approve (§4.3). Non-negotiable for assisted mode. |
| **Lease / isolation** | **Own** | Already exists (`lambda/bot_pool.py`); generalize the namespace, keep the semantics. |
| **Notification lifecycle** | **Own** | Already exists for Telegram (`opencode/lib/telegram.js` + `entrypoint.sh`); generalize. |
| **SSM namespace / Terraform** | **Own** | Already exists (`infra/user-pool/`); generalize. |
| **Blitzlog `ChatPlatform` ABC** | **Own** | A thin interface at the *Blitzlog-services* layer — config, lease, notify, decision-parse — mirroring the existing `lambda/provider/` pattern. |

### 5.3 The `ChatPlatform` interface

Following the existing provider abstraction (`lambda/provider/base.py` +
`lambda/provider/__init__.py`, dispatched on `BLITZLOG_PROVIDER`), the new
layer would be `lambda/chat/`:

```python
# lambda/chat/base.py
class ChatPlatform(ABC):
    """Blitzlog-side services for one chat platform.

    Note the scope: this covers configuration, leasing, notification and
    decision parsing — NOT the conversational client. The conversational
    client is a separate concern (see docs/CHAT_PLATFORMS.md §5.1).
    """

    @abstractmethod
    def list_pool(self, org_login: str) -> dict[str, str]: ...

    @abstractmethod
    def get_allowed_user_id(self, org_login: str) -> str | None: ...

    @abstractmethod
    def parse_decision(self, payload: dict, allowed: set[str]) -> str | None: ...

    @abstractmethod
    def notify(self, text: str, blocks: list | None = None) -> bool: ...
```

with `lambda/chat/telegram.py` and `lambda/chat/slack.py` implementations and
dispatch on a `CHAT_PLATFORM` environment variable.

### 5.4 Identity and authorization (organization-level)

**Decision: the organization selects the platform; each user leases their own
bot within it.**

Rationale for the split:

- **Org-level platform selection** matches how enterprises buy and administer
  software. It also makes the platform choice a **one-way door** for the org —
  see [Why not Teams](#why-not-teams) for why that matters.
- **Per-user bot leasing within the platform** preserves Blitzlog's existing
  guarantees: one agent conversation belongs to one engineer, the S3 lock
  semantics are unchanged, and an engineer can run several issues in parallel
  without their conversations bleeding together. It also avoids the
  contention problem that an org-wide shared bot pool would create.

**Namespace.** Today: `/blitzlog/users/<login>/telegram/{pool,allowed-user-id}`.
Proposed: `/blitzlog/orgs/<org>/chat/<platform>/…`, with a read-time
back-compat path for existing per-user Telegram pools so current deployments
keep working without a migration step.

**Resolving the organization.** The GitHub webhook payload carries
`installation_id`, which identifies the GitHub App installation — the closest
thing Blitzlog has to a tenant boundary. Blitzlog currently ignores it.
`repository.owner.login` is the repository owner, which is *not* the same
thing for an enterprise with many repositories under one org. Recommendation:
**key on `installation_id`, with `owner.login` as a fallback** for
self-hosted single-user installs.

**Mapping a chat user to a GitHub user.** Slack and Teams are multi-user by
nature; Telegram's model is "one numeric user ID may talk to this bot." At
org scope we need a **membership allowlist**: which chat identities in this
org may drive the agent.

Recommendation: **an explicit binding table**, e.g.
`/blitzlog/orgs/<org>/chat/slack/bindings/<github_login>` → chat user id.

- **Auditable** — an admin can read exactly who can drive the agent.
- **No PII guessing** — no email-matching against a third-party directory.
- **No bootstrap-trust hole** — "first message binds the identity" would let
  whoever messages first claim a colleague's identity.
- **Cost** — operator setup work, and a rotation process when people join or
  leave. That is a real cost, and it is the price of not guessing.

The existing single-value `allowed-user-id` generalizes to a set.

---

## 6. Why not Teams

**Teams is excluded on policy and platform-behaviour grounds, not because it
is technically hard.** Being precise here matters, because the ecosystem
alignment argument is the durable one and "it's architecturally hard" would
not survive the first competent engineer.

### 6.1 Platform-behaviour conflicts

From Microsoft's own documentation:

| Constraint | Impact on Blitzlog |
|---|---|
| **Streaming: ~1 update/sec, 2-minute hard cap, personal chats only** | Blitzlog agents run long tasks. A 2-minute cutoff means the answer arrives as a second, duplicate message. Group chats and channels get **no streaming at all**. |
| **`updateActivity` unsupported; messages are immutable** | The local-LLM fallback flow (`entrypoint.sh:519`) relies on inline buttons whose state can change. Adaptive Card refresh via `Action.Execute` **does not persist** — reopening the chat shows the original card. |
| **No file/image attachments; no file downloads** (M365 Copilot channel) | Blitzlog agents produce artifacts. Teams cannot deliver them as downloadable files. |
| **Proactive notifications unsupported** | The 35-minute idle watchdog and the "agent ready" ping are core Blitzlog behaviours. Teams actively fights them. |
| **"Agent Blocked" for third-party models** | Blitzlog's value is model-agnostic routing — MiniMax, local LLMs, OpenAI, Claude. A tenant admin can **break the agent by policy**. |
| **Entra app registration + admin consent + app-store packaging** | Every enterprise org needs IT approval. Self-service adoption dies at the admin gate. |

### 6.2 The ecosystem-alignment argument (the durable one)

> Blitzlog's differentiation is that it is **model-agnostic, self-hosted, and
> governed by the customer's own cloud and policies** — not by a third-party
> SaaS's tenant admin. Chat platforms that policy-gate agent extensibility
> are structurally misaligned with that proposition, regardless of how good
> their bot APIs are.

Microsoft's agent surfaces are policy-gated extensions of a productivity
suite. Teams' bot API is a thin surface over Copilot Studio, Entra, Graph,
Power Platform, and tenant data-policy enforcement. **When a platform's agent
surface is a sub-feature of a broader suite, the suite's governance model
leaks into the agent experience.** Every switch listed in §6.1 is a switch that
can be flipped off underneath Blitzlog.

There is a strategic dimension too. Blitzlog's differentiator against
Microsoft Copilot is *not* being inside Microsoft's ecosystem. Shipping
Blitzlog into Teams means competing with Copilot inside Microsoft's own agent
surface — where Copilot has native Graph grounding, native Entra identity,
native document access, and zero install friction. Blitzlog would be the
weaker, harder-to-install option in the place the org already looks first.

### 6.3 The honest counter-argument

If an org is a *Microsoft shop* — M365 for everything, Entra for identity,
Copilot for AI — then Teams is where the users already are, and "chat where
your users are" beats "chat where your users aren't." Blitzlog's Telegram-only
position already loses those orgs to Telegram's consumer-app stigma.

That argument is weaker than it looks, because **Slack already wins the same
slot.** Blitzlog's user is a developer working an issue, not a knowledge
worker in a meeting. Engineering orgs use Slack almost universally; many also
use Teams, but with Slack for the actual engineering work. Slack's native
streaming API, Block Kit, and Socket Mode all serve the dev-agent use case
better than Teams' policy-gated equivalents.

### 6.4 What "out of scope" actually means

Because ACP makes Teams *technically* cheap later — `txtcode` already ships
Teams via Bot Framework, and `openab` offers Teams through a gateway adapter —
the exclusion is a **prioritization decision, not a capability limit.** If an
org insists on Teams, Blitzlog could support it later as a thin adapter
without rewriting the agent layer.

We should record the reasoning here so the decision is durable and is not
re-opened every planning cycle by whoever next asks "why not Teams?"

---

## 7. Risks

| Risk | Severity | Mitigation |
|---|---|---|
| **ACP protocol churn.** ACP is young; the SDK is pre-1.0 in places and implementations reach into non-public shapes. | High | Pin exact versions. Encapsulate ACP behind Blitzlog's own client interface so protocol changes are absorbed in one place. |
| **Adopted-adapter fragility.** `agent-anywhere` documents that it pins `@satorijs/adapter-slack` exactly because "the Slack profile relies on their undocumented internal behavior" and that "no CI covers this path." | High | Vendor/fork the small parts we depend on; add our own contract tests for the capabilities Blitzlog actually uses. |
| **Permission gate regression.** If we adopt a gateway's permission policy wholesale, assisted mode silently becomes unattended. | High | Own the permission bridge explicitly (§4.2, §5.2). Add a test asserting that a denied approval blocks the tool call. |
| **Two OpenCode runtimes.** ACP cannot attach to `:4096` (§4.1); the ACP client and Blitzlog's plugins may observe different instance sets. | Medium | Decide deliberately whether to consolidate on `acp` or accept two runtimes. If plugins are the only `:4096` consumer, migrate them to ACP-native hooks and drop `serve`. |
| **Secret handling.** Bot tokens are interpolated into generated EC2 user-data in plaintext (`assisted.py:158`), and `/etc/blitzlog.env` is not `chmod 600`. Slack's two-token model compounds this. | Medium | Adopt the existing issue-#145 credential-rotation pattern (SSM + host-side poll) for chat credentials instead of baking them into user-data. |
| **Platform lock-in at the org level.** Platform selection is a one-way door for an adopting org. | Medium | Keep Telegram working as a fallback; make the platform selectable at the org level so it is a configuration change, not a migration. |
| **Slack rate limits.** 1 msg/s per method per channel; long outputs and tool-call bubbles can exceed this. | Medium | Use the existing `outbound-pacer` pattern from adopted adapters; chunk by *rendered* length, not raw character count. |
| **Node version fragility.** One community OpenCode Slack plugin documents "Node 22 (`>=22 <23`). Node 26 breaks Bolt Socket Mode." Blitzlog's Dockerfile pins Node 24.4.1, and issue #9 records that a Node version mismatch has already broken the bot once. | Medium | Pin and test the Node version against the adopted adapter; add it to the image build matrix. |
| **Org-level bot contention.** Per-user leasing avoids this, but org-wide pools would hit it immediately. | Low | Keep per-user leasing (§5.4). Monitor lock exhaustion rate. |

### 7.1 An unrelated latent risk this evaluation surfaced

Blitzlog's agent image uses `ARG OPENCODE_AI_VERSION=latest`, which currently
resolves to **OpenCode V1 (1.19.5)**. The V2 plugin package
(`@opencode/plugin`, latest **2.2.7**) is a **breaking rewrite**, and upstream
states explicitly that V1 plugin implementations do not run under V2.

Blitzlog's four plugins (`idle_watchdog`, `periodic_autosave`,
`session_archive`, `spot_watchdog`) are V1-shaped. If `opencode-ai@latest`
moves to 2.x, those plugins would **silently stop loading** — no error, just
no idle-ping, no autosave, no session archive.

This is independent of the chat work and should be tracked separately, but it
should be **pinned** regardless.

---

## 8. Roadmap

Each phase is independently shippable and leaves the system in a working state.

### Phase 0 — Pin and de-risk (no user-visible change)

- Pin `OPENCODE_AI_VERSION` to an explicit V1 version; add a CI check that
  fails if the pinned version's plugin API is incompatible.
- Pin Node exactly; add the adopted adapter's supported Node range to the
  image build matrix.
- Verify ACP behaviour against the *pinned* OpenCode version: confirm whether
  `session/request_permission` is emitted under ACP, and confirm the plugin
  `permission.ask` hook fires in an ACP-created session.

**Exit criteria:** we know exactly which approval mechanism Blitzlog will use.

### Phase 1 — Chat platform abstraction (no behaviour change)

- Introduce `lambda/chat/` (`base.py`, `telegram.py`, `__init__.py`) mirroring
  `lambda/provider/`.
- Refactor the existing Telegram code path behind it, **with zero behaviour
  change** and the existing test suite passing unchanged.
- Generalize `opencode/lib/telegram.js` into a platform-neutral notifier with
  a Telegram implementation and a Slack implementation.
- Generalize the notification call sites (ready banner, failure banner, idle
  watchdog, shutdown notice) behind the notifier.

**Exit criteria:** the platform is a configuration value, not a code fork.

### Phase 2 — Org-level configuration and identity

- Move the SSM namespace to `/blitzlog/orgs/<org>/chat/<platform>/…`, with a
  read-time back-compat path for existing `/blitzlog/users/<login>/telegram/…`
  pools.
- Resolve the org from `installation_id` (fallback `owner.login`).
- Generalize `allowed-user-id` to a binding table of
  `(github_login, chat_user_id)` pairs.
- Update `infra/user-pool/` Terraform to provision the new shape.

**Exit criteria:** two orgs on different platforms can coexist; existing
single-user Telegram deployments keep working untouched.

### Phase 3 — Slack

- Adopt a platform-adapter layer (Slack + the long tail) and write Blitzlog's
  own thin ACP client against it.
- Implement the permission bridge: plugin `permission.ask` → chat buttons →
  ACP `session/request_permission` response.
- Wire the session to the issue: `session/new` with `cwd` pointed at the
  issue's worktree; bind the Slack thread to the GitHub issue.
- Port the STT path (the existing Whisper shim) to Slack audio.
- Contract tests for every adapter capability Blitzlog depends on.

**Exit criteria:** an org can run Blitzlog end-to-end from Slack with a
working approval gate.

### Phase 4 — Enterprise hardening

- Move chat credentials out of generated user-data into the existing SSM +
  host-side rotation pattern (issue #145 style).
- Per-org allowlists and audit logging for agent runs.
- Multi-tenant isolation tests.
- Decide the `opencode serve` vs. `opencode acp` runtime consolidation
  question and act on it.

### Phase 5 — Next platform

Deliberately unspecified. By this point a new platform should be **adapter
work only** — no agent-layer changes. Candidate: Discord (native gateway
support, developer-native) or Google Chat. Driven by org demand.

---

## 9. What we are deliberately not doing

- **Not supporting Teams.** See §6. Recorded so it is not re-litigated every
  planning cycle.
- **Not writing another chat adapter library.** The ecosystem already has
  good ones; we adopt them.
- **Not migrating Blitzlog to a different harness.** ACP means Blitzlog can
  support Claude Code, Codex, and Gemini CLI over the same integration later,
  if that becomes useful. That is a benefit, not a project.
- **Not changing the lease model.** Per-user leasing survives; only the
  platform dimension is added.

---

## 10. Related issues

| Issue | Relationship |
|---|---|
| [#138](https://github.com/great-wall-connect/blitzlog/issues/138) | Per-user Telegram BotFather wiring — same SSM namespace; the org-level change supersedes the per-user model. |
| [#141](https://github.com/great-wall-connect/blitzlog/issues/141) | EC2 agent watcher + Telegram 'agent started' ping — same notification path (seam 7). |
| [#163](https://github.com/great-wall-connect/blitzlog/issues/163) | Configurable, enforced command-permission policy — directly intersects the ACP permission gap (§4.2). |
| [#165](https://github.com/great-wall-connect/blitzlog/issues/165) | Three-level model selection — interacts with the "model-agnostic" positioning in §6.2. |
| [#120](https://github.com/great-wall-connect/blitzlog/issues/120) | Automated rotation for SSM-stored Blitzlog secrets — the mechanism for Phase 4 credential handling. |

## References

- [OpenCode ACP documentation](https://opencode.ai/docs/acp/)
- [OpenCode plugin documentation](https://opencode.ai/docs/plugins/)
- [Agent Client Protocol](https://agentclientprotocol.com/)
- [`agent-anywhere`](https://github.com/l0ng-ai/agent-anywhere) — multi-platform ACP gateway (MIT)
- [`openab`](https://github.com/openabdev/openab) — Rust ACP gateway with Slack/Discord native
- [`txtcode`](https://github.com/IMvision12/txtcode) — six-platform gateway including Teams
- [`tandem`](https://github.com/jakejimenez/tandem) — multi-harness Slack/Mattermost bridge
- [`@opencode-ai/slack`](https://www.npmjs.com/package/@opencode-ai/slack) — first-party OpenCode Slack integration
- [Slack rate limits](https://docs.slack.dev/apis/web-api/rate-limits)
- [Microsoft: Slack vs. Teams feature gaps](https://github.com/microsoft/slack-plus-teams/blob/main/docs/feature-gaps.md)
- [Microsoft: Agent runtime limits and streaming limits](https://learn.microsoft.com/en-us/microsoft-copilot-studio/agents-experience/runtime-limits)
- [Microsoft: Teams agent rate limiting](https://learn.microsoft.com/en-us/microsoftteams/platform/bots/how-to/rate-limit)
