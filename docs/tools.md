# Paraphe tool surface

The contract of the MCP surface Paraphe serves. It is Paraphe's own contract:
the tool names and fields are what they are because the clients that raise
decisions already speak them, and this document is the authority on what they
mean and which lifecycle they take part in.

Transport: Streamable HTTP (JSON responses, or SSE when the client asks for
`text/event-stream`) at `/mcp`, authenticated with the create bearer. The MCP
session id is returned on the `Mcp-Session-Id` header. Responses wrap the tool
result in `result.content[0].text` as JSON.

Every tool rejects unknown parameters, so send exactly the documented fields.

## The credential

One shared create bearer. It can create a card, read a card, update it, cancel
it, report on it and mark it processed. **It cannot answer one.** Answering
requires the owner's own identity, and the only path that accepts it is the
owner-only answer path, which the create bearer is refused on. That asymmetry
is the product: an inbox an agent can approve on its owner's behalf is not an
owner-decision inbox.

## Lifecycle

```
create → the owner sees the card → the owner answers → the agent reads the answer → report → mark processed
```

- **create** — any of `ask_question`, `request_approval`, `request_feedback`,
  `notify_user`. Every create but `notify_user` needs a stable `external_id`;
  a repeat with the same `external_id` returns the existing card and does not
  notify again.
- **record** — the create result carries the `request_id` and names the reading
  path (`read_with: get_response`). Keep the id with the work; a session ending
  with an open card records it and its continuation so the next run can act.
- **see** — the configured destination shows the card to the owner: the tap
  adapter on a phone, or the console destination on the machine running
  Paraphe.
- **answer** — the owner answers on that destination, or through the owner-only
  answer path. On Telegram the owner's long-press reply to a card message is
  an answer too: the text is recorded verbatim as the card's answer and the
  card closes exactly as a tap would. An answer is bound to the card's
  `version`; a superseded version is refused and the card is unchanged.
- **read** — `get_response` and `list_unprocessed` show the answer without
  consuming it. The answer is nested at `response.choice`. When you next run,
  drain the answers that are yours.
- **wait** — `ask_question`, `request_approval` and `get_response` accept
  `wait_seconds` (0–60): the call parks until the owner answers inside the
  window or the window ends, then returns the reading envelope — answered, or
  still pending. The `paraphe wait <request_id>` command repeats that bounded
  window until the card is answered or can no longer be answered, and prints
  the answer when it arrives (exit 0 answered, 3 expired or not answerable,
  4 unknown). Any runner that can background a command is returned by the
  answer; polling stays sufficient on its own.
- **report** — `report_execution` records the outcome of an approval.
- **close** — `mark_processed` is idempotent.

## Tools

| Tool | Required | Optional | Kind |
|---|---|---|---|
| `how_to_use` | — | — | read this first |
| `ask_question` | `question` | `context`, `choices`, `choice_notes`, `allow_freeform` | create, open `question` |
| `request_approval` | `title` | `details` | create, open `approval` |
| `request_feedback` | `title` | `details` | create, open `feedback` |
| `notify_user` | `title` | `message`, `result` | status only, never a decision |
| `get_response` | `request_id` | `wait_seconds` | read |
| `list_pending` | — | — | read, cards waiting |
| `list_unprocessed` | — | — | read, answers not yet processed |
| `update_request` | `request_id`, `expected_version` | `title`, `details`, `choices`, `choice_notes`, `renotify`, and the shared fields | revise in place |
| `cancel_request` | `request_id` | `reason` (`cancelled` \| `resolved_elsewhere`) | withdraw |
| `report_execution` | `request_id`, `outcome` | `note`, `result` | approval closeout |
| `mark_processed` | `request_id` | — | idempotent closeout |

The three asking tools and `update_request` also accept the provenance fields
below; `choice_notes` is a `choices`-parallel array of one-line notes.

## Shared create fields

Accepted on every create, and on `update_request`.

| Field | Constraint | Meaning |
|---|---|---|
| `external_id` | string ≤200, required on creates except `notify_user` | the caller's own stable key; makes retries idempotent |
| `risk` | `low` \| `medium` \| `high` \| `critical` (default `medium`) | honest risk, not a display hint |
| `priority` | `low` \| `normal` \| `high` \| `urgent` (default `normal`) | the caller's own ordering |
| `project` | string ≤120 | which work the card belongs to |
| `source_thread` | string ≤200 | the session or thread the card came from; descriptive metadata, nothing is woken from it |
| `recommendation` | string ≤1000 | what the agent advises |
| `consequence` | string ≤1000 | what the approval sets in motion, and what happens if the owner does nothing; renders as *If approved:* |
| `prohibitions` | up to 8 strings, each ≤200 | what the answer does not authorise; renders as *Limits:* |
| `links` | up to 8 URIs, each ≤500 | evidence |
| `agent_name` | string ≤60 | which agent asked |
| `wait_seconds` | 0–60 | on `ask_question`, `request_approval` and `get_response`, the call parks until the owner answers or the window ends, then returns the answer envelope; elsewhere accepted and not slept on |
| `expires_in_seconds` | 900–2 592 000 (default 14400) | card life; a card past it is unanswerable |
| `rule_key` | accepted and ignored | always-allow is not product |

## Provenance

Optional on `ask_question`, `request_approval`, `request_feedback` and
`update_request`. It travels with the ask — supplied by the caller, never
guessed — and renders as the card's identity line in this order, absent
fields omitted.

| Field | Constraint | Meaning |
|---|---|---|
| `runtime` | string ≤40 | which runtime asks (`Hermes`, `Claude Code`, `Codex`, …) |
| `repo` | string ≤120 | which repository the ask is about |
| `worktree` | string ≤120 | which checkout; omit for the canonical checkout |
| `ticket` | string ≤200 | which ticket the ask is about; a URL renders as a tappable link |

## Writing the card

Every card carries its origin and its purpose first, and its tap semantics
are plain. Three rules — this is the writing contract:

1. **Origin stated.** Who is asking and where it runs: `agent_name` and the
   provenance fields, which render as the identity line. Never guessed:
   supply what is true, leave out the rest. A destination that prints body
   text only gets the same in the first body lines.
2. **Purpose first.** Why the action is needed comes before the action
   detail. `question` / `title` is the purpose in one line;
   `context` / `details` opens with the why, then names the exact command
   or change.
3. **Tap semantics plain.** What the approve tap authorises and what it
   does not: `consequence` (the *If approved:* section) says what the
   approval sets in motion; `prohibitions` (the *Limits:* section) says
   what it does not authorise. On an approval, spell out both taps — what
   Approve does, what Deny does — so the owner never decodes a button.

Body copy in that order — purpose, origin, the exact command, the tap
semantics:

```
Pourquoi : the release cannot ship with the store at the old path.
D'où ça vient : agent Hermes · runtime CLI · repo paraphe · worktree
store-move · ticket card-3119.
Command: paraphe store relocate
Approve authorises that one move and nothing else. Deny leaves the store
where it is and the release waits.
```

## The rendered card

On the phone the card is ordered rich text: identity line, kind (with a risk
word when high or critical), bold title, context, numbered options with their
one-line notes, Recommended, If approved, Limits, links, a reply hint and the
expiry. Long content trims with a visible marker under the platform's message
limit; all field text is escaped, so nothing on a card can inject markup. A
long-press reply on a card message answers it in the owner's own words or
asks a question; a reply to a status message records nothing.

## Answers and outcomes

`get_response` returns:

```json
{
  "request_id": "…",
  "status": "pending | answered | acknowledged | cancelled | expired",
  "version": 2,
  "response": {
    "choice": "Approve",
    "text": null,
    "responded_at": "2026-09-10T10:39:03Z",
    "responded_via": "telegram | answer-path"
  },
  "processed_at": null,
  "execution_status": null,
  "kind": "approval",
  "pending": false
}
```

`responded_via` reports how the answer actually arrived, so a locally answered
card is never reported as a phone tap: `telegram` is a tap, `telegram-reply`
is the owner's long-press reply, `answer-path` is the local owner answer. A
reply that is a question or not a decision is not executed: explain, then
re-ask with a fresh card.

`report_execution` takes `outcome` (`accepted` | `rejected` | `completed` |
`failed`) and an optional `result` object: `outcome`
(`success` | `failure` | `partial`), `files_changed`, `tests_passed`,
`tests_failed`, `commit_message`, `duration_seconds`.
