# MCP surface keep-or-drop inventory for Paraphe v1

- **Date:** 2026-08-25
- **Decision already locked:** [ADR 0001](../adr/0001-compatible-mcp-surface.md) — same tool names and reliability fields; this note inventories them and does not reopen compatibility.

## Question

Which MCP tools and reliability fields must Paraphe v1 keep so existing agent
clients can point at it without renaming tools — and which product-specific
extras can the spec drop?

## Method

Primary sources only. No card was sent to the surface Paraphe inherits, and no
mutating tool (`request_approval`, `ask_question`, `notify_user`,
`request_feedback`, `update_request`, `cancel_request`, `mark_processed`,
`report_execution`) was called on it.

| Source | What it proves |
|---|---|
| A live `how_to_use` call | The published reliability contract, tool list, mirror metadata |
| A live `tools/list` call | The exact 12 tool names and their inputSchema field sets |
| The deferred client-side schemas | Descriptions, required fields, enums, maxLengths |
| [ADR 0001](../adr/0001-compatible-mcp-surface.md) | Compatibility already accepted; extras named as drop candidates |
| [ADR 0002](../adr/0002-telegram-private-bot-tap-surface.md), [ADR 0003](../adr/0003-dual-path-wake.md), [ADR 0004](../adr/0004-cutover-hermes-and-launchers.md) | Telegram tap, poll fallback, v1 caller set |
| [`CONTEXT.md`](../../CONTEXT.md) | Card / Tap / Inbox / Wake / Cutover vocabulary |
| The existing answer-intake and escalation procedures | Recovery fields clients already parse, and the create/closeout field set they require |

## Verdict

Keep the **12 tool names** and the **reliability + recovery envelope** so
existing agent clients can retarget the endpoint without renaming calls. Drop
**mobile-app presentation and auto-approve mechanics** already named by ADR 0001:
bulk approve, lock-screen chrome, and `rule_key` / “Always allow”.

Telegram replaces the phone-app surface
([ADR 0002](../adr/0002-telegram-private-bot-tap-surface.md)). That is a
presentation swap, not a tool rename. Clients still create, poll, cancel,
report, and mark processed with the same names.

## Live tool inventory (12)

A live `tools/list` returned exactly these names, and the live `how_to_use`
lists the same twelve. Clients currently expose them namespaced as
`<server>__<name>`.

| Tool | Live required args | Verdict | Why |
|---|---|---|---|
| `how_to_use` | none | **Keep** | Live guide: “Call this first.” Carries the full reliability contract. Cutover clients already know this name. |
| `ask_question` | `question` | **Keep** | ADR 0001; the create-side field set; unresolved-fork surface. |
| `request_approval` | `title` | **Keep** | ADR 0001. Yes/no owner decision. Keep the tool; drop lock-screen Approve/Deny chrome. |
| `get_response` | `request_id` | **Keep** | Official poll; [ADR 0003](../adr/0003-dual-path-wake.md) fallback; the durable store is the source of the answer. |
| `list_unprocessed` | none | **Keep** | Restart recovery. Reads do not consume answers (`how_to_use`). |
| `list_pending` | none | **Keep** | Intake step 1; distinguishes waiting vs already tapped. |
| `mark_processed` | `request_id` | **Keep** | Idempotent closeout. The proof loop is detected → delivered → executed → reported → marked processed ([ADR 0004](../adr/0004-cutover-hermes-and-launchers.md)). |
| `report_execution` | `request_id`, `outcome` | **Keep** | Live contract. Outcomes stay `accepted` / `rejected` / `completed` / `failed`. |
| `update_request` | `request_id`, `expected_version` | **Keep** | Reliability: bump `version` instead of re-creating; stale answers rejected (`how_to_use`). |
| `cancel_request` | `request_id` | **Keep** | Withdraw pending items. Reasons stay `cancelled` / `resolved_elsewhere`. |
| `notify_user` | `title` | **Keep name, drop app chrome** | Status, never a decision card. Keep the tool so callers do not rename a connectivity probe. Do not require lock-screen ack UI. |
| `request_feedback` | `title` | **Keep name, optional in v1 behavior** | Live tool; no intake or escalation requirement. Accept the name so `tools/list` stays compatible. The spec need not invent a Telegram feedback product. |

Do not add new required tool names in v1. Live `how_to_use` etiquette:
“unknown parameters are rejected, so send exactly what's documented.”

## Reliability fields to keep

These are the fields ADR 0001 means by “reliability fields”. Existing skills
and the live guide already write or parse them. Lengths below are the live
schema maxLengths.

### Create / update identity

| Field | Live constraint | Verdict | Evidence |
|---|---|---|---|
| `external_id` | string ≤200 | **Keep, required by contract** | `how_to_use`: duplicate creates return the existing item. Callers always set it. |
| `version` | integer, bumped by `update_request` | **Keep** | Stale answers rejected. Clients must read it back. |
| `expected_version` | integer ≥1 on `update_request` | **Keep** | Optimistic concurrency on revise-in-place. |
| `request_id` | string | **Keep** | Create proof and every poll/closeout argument. HTTP 200 without `request_id` is not creation. |
| `expires_in_seconds` | 60–2_592_000 | **Keep** | Live contract: stale approvals cannot be answered later. Callers commonly send `3600`. |
| `duplicate` (create response) | boolean / status | **Keep on read-back** | Idempotent retry is success for the same item, not a reason to notify again. |

### Card body (clients already fill these)

| Field | Live constraint | Tools | Verdict | Evidence |
|---|---|---|---|---|
| `question` | ≤200 | `ask_question` | **Keep** | Headline. |
| `context` | ≤2000 | `ask_question` | **Keep** | Writing contract body. |
| `choices` | ≤4 strings, each ≤40 | `ask_question`, `update_request` | **Keep** | Live: up to 4 one-tap options. Writing guidance: 3 or 4, never 2. |
| `allow_freeform` | boolean, default true | `ask_question` | **Keep in schema** | Live tool. Writing guidance prefers taps, not free text. Accept the field; do not require a free-text Telegram composer in v1. |
| `title` | ≤200 | `request_approval`, `notify_user`, `request_feedback`, `update_request` | **Keep** | Approval / notify / feedback headline. Schema is not `question`. |
| `details` | ≤2000 | `request_approval`, `request_feedback`, `update_request` | **Keep** | Approval body. Sending ask fields to approval is HTTP 400. |
| `message` | ≤2000 | `notify_user` | **Keep** | Notify body. Different name from `details` / `context`. |
| `recommendation` | ≤1000 | create/update family | **Keep** | Live: shown first on the card. Required by the writing contract. |
| `consequence` | ≤1000 | create/update family | **Keep** | Writing contract. |
| `prohibitions` | ≤8 strings, each ≤200 | create/update family | **Keep** | Writing contract. |
| `risk` | `low` \| `medium` \| `high` \| `critical` | create/update family | **Keep enum; drop bulk-approve meaning** | Honest risk is required. Default live is `medium`. Do **not** keep “only `low` is bulk-approvable”. |
| `project` | ≤120 | create/update family | **Keep** | Inbox grouping; each caller keeps its own namespace here. |
| `source_thread` | ≤200 | create/update family | **Keep** | Origin for wake / closeout ([ADR 0003](../adr/0003-dual-path-wake.md)). |
| `links` | ≤8 URIs, each ≤500 | create/update family | **Keep** | Writing contract puts ticket keys here, not in the headline. |
| `priority` | `low` \| `normal` \| `high` \| `urgent` | create/update family | **Keep enum; drop Focus chrome** | Live maps values onto silent / default / prominent / Focus-break delivery. Telegram can ignore delivery chrome and still accept the field. |
| `agent_name` | ≤60 | ask / approval / notify / feedback | **Keep** | Who is asking. Useful on Telegram; not app-only. |
| `wait_seconds` | 0–60 | ask / approval / feedback / `get_response` | **Keep** | Official poll contract. Callers commonly send `0` and poll later. |

Kind-specific argument names are load-bearing: `ask_question` takes
`question`+`choices`; `request_approval` takes `title`+`details` (no `choices`).
Paraphe must not collapse those names.

### Closeout and execution

| Field | Live constraint | Verdict | Evidence |
|---|---|---|---|
| `outcome` on `report_execution` | `accepted` \| `rejected` \| `completed` \| `failed` | **Keep** | `how_to_use`; stale-choice closeout uses `rejected`. |
| `note` | ≤1000 | **Keep** | Plain-language explanation. |
| `result` | object: `outcome` `success`\|`failure`\|`partial`, `files_changed`, `tests_passed`, `tests_failed`, `commit_message`, `duration_seconds` | **Keep schema; drop app result-card chrome** | Live on `notify_user` and `report_execution`. Rendering as a mobile result card can drop. |
| `reason` on `cancel_request` | `cancelled` \| `resolved_elsewhere` | **Keep** | Live schema + `how_to_use`. |
| `renotify` | boolean, default false | **Keep in schema** | `update_request` only. Means “send a fresh push”. Telegram can treat it as “edit/resend the bot message”. |

### Response envelope clients already parse

Paraphe adds `external_id`, offered `choices` and `expires_at` (Unix epoch
seconds, nullable for legacy cards) to the shared read envelope. `get_response`
accepts exactly one of `request_id` or `external_id`; the latter recovers a
lost create acknowledgement without re-notifying. The original external id
remains immutable across revisions. See [the current contract](../tools.md).

The live `get_response` shape, after unwrapping `result.content[].text`:

```json
{
  "request_id": "…",
  "status": "answered",
  "version": 2,
  "response": {
    "choice": "Approve activation",
    "text": null,
    "responded_at": "2026-08-24T14:40:02.036Z",
    "responded_via": "app"
  },
  "processed_at": null,
  "execution_status": null
}
```

| Field | Verdict | Why |
|---|---|---|
| Nested MCP `result.content[].text` JSON | **Keep** | HTTP 200 is not enough; the envelope carries the answer. |
| Top-level `status` (`pending` / `answered` / `acknowledged` and kin) | **Keep** | Never claim waiting from a transcript. |
| Nested `.response` object | **Keep** | Top-level `choice` can be null while `.response.choice` is the tap. |
| `.response.choice` | **Keep** | The answered signal. |
| `.response.text` | **Keep** | Freeform / feedback. |
| `.response.responded_at` | **Keep** | Authorship / recency checks. |
| `.response.responded_via` | **Keep values that exist; add a tap value later** | Live values are `app` and `auto`. Paraphe needs a tap-surface value; that is additive, not a rename. |
| `processed_at` | **Keep** | Read-back after `mark_processed`. |
| `execution_status` | **Keep** | Authoritative after `report_execution`. |
| `kind` (`question` vs approval) | **Keep** | `report_execution` is approval-kind; question-kind closeout is `mark_processed` alone. |
| `pending` on create | **Keep** | Creation proof field. |

SSE vs JSON Streamable HTTP and the optional `Mcp-Session-Id` are transport
facts, not product fields. Both are already accepted by callers. The spec should
keep that transport flexibility so launcher harnesses do not need a new client.

## Extras the spec can drop

Named by [ADR 0001](../adr/0001-compatible-mcp-surface.md) as out of scope.
Confirmed against the live guide and schemas.

| Extra | Where it lives today | Drop means |
|---|---|---|
| Bulk approve | `how_to_use` + `risk` descriptions: only `low` is bulk-approvable in the app | Accept `risk` but do not implement multi-card approve. Never auto-weaken `medium+`. |
| Lock-screen chrome | `request_approval` description: “Approve / Deny buttons on the lock screen”; ask/notify “push notification” / “expanded notification” | The Telegram private bot is the tap ([ADR 0002](../adr/0002-telegram-private-bot-tap-surface.md)). Keep yes/no and 2–4 taps as semantics. |
| `rule_key` / “Always allow this” | Live field on ask/approval/notify/feedback/update. `how_to_use`: low-risk + same agent+project+rule_key auto-approves server-side, `responded_via: auto` | Drop from v1 unless a later ticket proves a client needs it (ADR 0001). Clients may still send the field; unknown-parameter rejection means the compatible choice is **accept and ignore**, not 400. |
| Focus / silent / time-sensitive push | `priority` enum descriptions | Accept the enum; do not implement platform-specific delivery chrome. |
| App result-card UI | `result` “rendered as a result card in the app” | Keep the JSON object; do not clone another product's card chrome. |
| App onboarding / path tokens | `how_to_use` Connect section | Paraphe auth is a separate decision. Do not copy an app Connect tab. |
| `responded_via: auto` as a product | Coupled to `rule_key` | Drops with Always-allow. |

`allow_freeform`, `request_feedback`, and `notify_user` are **not** app-only.
They are live tool/field names. Dropping the *names* would break `tools/list`
parity. Dropping app-specific presentation around them is in scope.

## What existing callers actually send

Evidence that the keep set is not theoretical.

**A batch caller** already posts: `question`, `context`, `choices`,
`allow_freeform=false`, `wait_seconds=0`, `agent_name`, `external_id`,
`project`, `recommendation`, `consequence`, `prohibitions`, `source_thread`,
`risk=medium`, `expires_in_seconds=3600`. Poll is
`get_response(request_id, wait_seconds=0)`. Closeout is
`report_execution(request_id, outcome, note)` then `mark_processed(request_id)`.
[ADR 0004](../adr/0004-cutover-hermes-and-launchers.md) leaves that caller class
on the previous inbox in v1; the argument names still document the compatible
surface.

**The owner-decision writing contract** requires the same body fields plus
`links` and honest `risk`, with phone limits that match the live schema
(200 / 2000 / 1000 / 40).

**The escalation procedure** requires `request_approval` / `ask_question` /
`external_id` / expiry / `project` / `recommendation` / `consequence` /
`prohibitions` / `source_thread` / honest `risk` / `priority` / revalidation /
`mark_processed` / `report_execution`.

**Answer intake** requires `list_pending` → `list_unprocessed` →
`get_response`, nested `.response.choice`, then `report_execution` +
`mark_processed`, and read-back of `execution_status` / `processed_at`. A
blocked `mark_processed` does not erase the tap.

## Compatibility rules for the spec (not implementation)

1. **Same names, same required args.** Do not rename `question` to `title` on
   `ask_question`, or fold `mark_processed` into `report_execution`.
2. **Accept documented optional fields even if Telegram ignores them.** The live
   server rejects unknown parameters. The compatible drop for `rule_key` is
   ignore-on-read, not schema deletion that 400s current clients.
3. **Keep the nested answer envelope.** Pollers that only read top-level
   `choice` already failed once.
4. **Keep kind split.** Approval cards take `title`/`details` and
   `report_execution`. Question cards take `question`/`choices` and close with
   `mark_processed`.
5. **Idempotent create + non-consuming reads + explicit processed.** That is the
   reliability contract. Wake is best-effort on top
   ([ADR 0003](../adr/0003-dual-path-wake.md)).
6. **Do not implement Always-allow, bulk approve, or lock-screen chrome in v1.**

## Open items this inventory does not decide

- Telegram button lifetime vs `expires_in_seconds` / stale taps.
- What `responded_via` should be for a Telegram tap.
- Whether accepting-and-ignoring `rule_key` is enough, or a later ticket must
  400 it.
- Auth: who may create a card, and how the bot knows the tap comes from the
  owner.
- Dual-inbox cutover so one decision is never collected on both inboxes.

## Sources

1. A live `how_to_use` call on the surface Paraphe inherits, read-only.
2. A live `tools/list` call on the same surface (12 tools, inputSchema field
   sets).
3. The client-side tool schemas for the same twelve tools.
4. `docs/adr/0001-compatible-mcp-surface.md`,
   `0002-telegram-private-bot-tap-surface.md`, `0003-dual-path-wake.md`,
   `0004-cutover-hermes-and-launchers.md`.
5. `CONTEXT.md`.
