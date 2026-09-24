# Paraphe v1 spec

Parent: the v1 specification map

Vocabulary from `CONTEXT.md`. Decisions from ADR 0001–0011.
Keep/drop source of truth: `docs/research/mcp-surface-keep-or-drop.md`
(inventoried 2026-08-25 from a live `tools/list` + `how_to_use`;
no card was sent).

Jude r1 (`5a288ad`) failed five P1s. This revision closes them.

Revision (2026-09-11): the return path is the waited call, the waiter command
and the poll read (ADR 0011, superseding ADR 0003 and ADR 0006). The wake
sections those ADRs carried are kept there as history; where this spec and
ADR 0011 disagree, ADR 0011 wins.

Revision (2026-09-11, the card): the card renders as ordered rich sections
(identity line, kind, bold title, context, numbered options with notes,
Recommended, If approved, Limits, links, reply hint, expiry) under a
deterministic message budget; provenance (`runtime`/`repo`/`worktree`/`ticket`)
travels with the ask and renders as the identity line; per-choice notes ride
`choice_notes`. The owner can answer a card with a long-press reply — the
text is recorded verbatim (`response.text`, `responded_via` `telegram-reply`)
and the card closes exactly as a tap. The answer authority is unchanged:
owner-side answers only.

Revision (2026-09-24, the writing contract): every card states its origin
and leads with its purpose, and its taps are plain — origin stated (which
agent/session and where it runs), purpose first (why the action is needed
before the action detail), tap semantics plain (what the answer authorises
and what it does not). The contract is taught wherever card composition is
taught — the return-path skill, `docs/tools.md`, the served `how_to_use`
text — and shown on the demo card.

## Problem Statement

The owner already decides hard-to-reverse work with a phone tap. The previous inbox does that job today, and the writing contract is good: purpose first, a few short choices, one recommended. What is not good is that the previous inbox is closed-source, he has no guarantee it will stay up or keep his decisions safe, and a tap often does not wake the session that asked. Desktop tools are frequently blocked, so cards go out through a helper with no origin, closeout fails, and he is told to come back to chat. He will not. He wants an inbox he owns, on his box, that he taps in Telegram, without bouncing to another app for the same yes.

## Solution

Paraphe is a self-hosted owner decision inbox. Agents create a Card through the same MCP tool names they already use. The owner taps it in a dedicated Telegram bot private chat. The Inbox on the deployment host is the source of the Tap. The answer returns through the ask the agent already holds: a waited call inside a bounded window, the `paraphe wait` command for the card's whole lifetime, or a read at the next boundary. The same session that asked resumes and acts; if it is gone, the next run drains the answer — no fresh session executes a dead origin. The previous inbox stays until one real high-risk Paraphe card completes tap → same session acts → report → mark processed. Then the approved callers flip together in one owner-approved window and never send that class of decision to both inboxes.

## User Stories

1. As the owner, I want to tap Approve or a short choice in Telegram, so that I do not unlock another app for the same yes.
2. As the owner, I want the card to say the end purpose in the first sentence, so that I know what this is for before I read mechanism.
3. As the owner, I want three or four short choices and one recommended, so that I can tap without decoding Linear or git jargon.
4. As the owner, I want a yes/no card when the only honest answers are approve or deny, so that I am not forced into a fake multiple choice.
5. As the owner, I want silence to mean nothing, so that an unread notification cannot become a yes.
6. As the owner, I want a late tap on an expired or already-answered card to refuse, so that a stale button cannot approve dead work.
7. As the owner, I want a leftover button from before a card revision to refuse even if the label matches, so that `update_request` cannot be bypassed.
8. As the owner, I want buttons to disappear after I tap, cancel, or the card expires, so that the chat does not keep offering a dead choice.
9. As the owner, I want a default card life of several hours, so that reading notifications late does not expire the card.
10. As the owner, I want no card to live less than fifteen minutes, so that a demo-short TTL cannot ship.
11. As the owner, I want the same Hermes session that asked to resume and act after I tap, so that I do not have to announce the tap in chat.
12. As the owner, I want a missing session to fail the wake honestly, so that a fresh agent session does not execute a tap in the wrong conversation.
13. As the owner, I want agents to keep working on independent tasks while a card is open, so that one tap does not freeze the house.
14. As the owner, I want notify-only messages to be status, never a yes/no, so that a pager cannot pretend to be a decision.
15. As the agent, I want to call `request_approval` and `ask_question` with the names I already know, so that existing skills do not have to be rewritten.
16. As the agent, I want `external_id` to make a retry return the same Card, so that I do not double-notify.
17. As the agent, I want `update_request` to bump version and reject stale answers, so that I can revise a card instead of minting a second one.
18. As the agent, I want `get_response` and `list_unprocessed` to show a tap without consuming it, so that a crash does not lose the answer.
19. As the agent, I want `report_execution` and `mark_processed` to close the loop, so that the inbox knows the tap was used.
20. As the agent, I want a nested `.response.choice` envelope, so that I do not miss a tap that only lives under `.response`.
21. As the agent, I want `ask_question` to take `question`/`context`/`choices` and `request_approval` to take `title`/`details`, so that mixing those fields does not 400.
22. As the agent, I want to send `recommendation`, `consequence`, `prohibitions`, and honest `risk`, so that the card stays ELI15.
23. As the agent, I want Desktop and WebUI creates to store `hermes:session:api_server:<id>`, so that a wake can target api_server and never `platform=webui`.
24. As the agent, I want a Telegram Hermes create to store `hermes:session:telegram:<id>`, so that a chat topic is not treated as the origin.
25. As the agent, I want create with `origin=none` to be a valid poll-only Card, so that a blocked Desktop session can still ask.
26. As a Codex/Claude/Grok launcher, I want the same tool names and the same bearer after cutover, so that I point at a new endpoint and keep working.
27. As a Codex/Claude/Grok launcher, I want poll to remain enough if I cannot be woken, so that I still honour a tap.
28. As the batch caller, I want to keep using the previous inbox for batch/git work (its own `project` value), so that I do not join the v1 cutover.
29. As the batch caller, I want to be forbidden from sending a decision the flipped callers would send, so that I cannot create a double yes.
30. As the owner, I want one owner-approved cut window for the approved callers, so that those four never run both inboxes.
31. As the owner, I want the previous inbox stays live until one real high-risk Paraphe card completes tap → same session acts → report → mark processed, so that a notify ping cannot authorize the flip.
32. As the owner, I want no fallback to the previous inbox after those four flip, so that a Paraphe outage does not quietly reopen a second inbox.
33. As the owner, I want cards stored as `/var/lib/paraphe/inbox.sqlite` owned by user `paraphe`, so that a vendor disappearing cannot take the history.
34. As the owner, I want a consistent snapshot at `/var/lib/paraphe/inbox.sqlite.bak` before daily restic, so that restore is a path I already practice.
35. As the owner, I want `/var/lib/paraphe` added to the live restic include list before the proof card, so that the store is actually off-box.
36. As the owner, I want the private GitHub repo to stay code only, so that card state is not committed.
37. As the owner, I want bot token, owner Telegram id, default TTL, and floor TTL in a config file or env, so that I can set up without editing code.
38. As the owner, I want env to win over the file, so that a host secret can override a file without rewriting it.
39. As the owner, I want missing required setup to refuse start, so that a half-configured bot cannot listen.
40. As the owner, I want an owner-only `/config` command to change default TTL and floor TTL later, so that I do not need a shell for knobs.
41. As the owner, I want `/config` never to show the current bot token, so that chat history cannot leak it.
42. As the owner, I want first-run still to need the file or env, so that `/config` is not the only way to birth the bot.
43. As the owner, I want only my Telegram user id to count as a Tap, so that a forwarded or foreign callback cannot approve.
44. As the owner, I want strangers who message the bot not to create cards, so that the tap surface is not a second create path.
45. As an attacker with a leaked MCP bearer, I should be able to create cards but not tap them, so that create and tap stay split.
46. As an attacker with a stolen Telegram account, I should be able to tap but not create via MCP, so that one leak is not the whole inbox.
47. As the agent, I want `rule_key` accepted and ignored, so that current clients do not 400 after the swap.
48. As the agent, I want `notify_user` and `request_feedback` to keep their names, so that `tools/list` stays compatible even if Telegram does not grow a feedback product.
49. As the agent, I want `cancel_request` with `resolved_elsewhere` when the owner already did the thing, so that a redundant card dies cleanly.
50. As the agent, I want expiry to make a card unanswerable, so that a week-old approval cannot ship.
51. As the agent, I want to keep working after send, so that waiting is not blocking.
52. As the owner, I want the Inbox never treated as source of truth, so that agents still revalidate live repo and task state before acting.
53. As the owner, I want chat topics to stay alerts, so that cron failure pings do not become the decision inbox.
54. As the owner, I want the repo private until my own use proves Paraphe working, safe, and useful, so that open-source is a later choice.
55. As a future implementer, I want one Inbox test seam, so that MCP lifecycle and tap/refuse/poll/closeout are proven without driving Telegram or the Hermes gateway in unit tests.
56. As a cutover operator, I want each of the approved callers to add Paraphe next to the previous inbox, prove `tools/list`, then switch endpoint and block the previous inbox in one window, so that dual-write is impossible.

## Implementation Decisions

- One module: the Inbox. It owns cards, MCP tool dispatch, tap claims, closeout, and setup. Telegram and Hermes wake are adapters behind that module, not a second product.
- MCP surface keeps the twelve live tool names and required args (ADR 0001). Kind split stays: questions use `question`/`context`/`choices`; approvals use `title`/`details`.
- Full keep/drop inventory is `docs/research/mcp-surface-keep-or-drop.md`. Summarized here; the file is normative if this list and that file disagree.

### Tools (keep all 12 names)

| Tool | Required args | v1 product |
|---|---|---|
| `how_to_use` | none | Keep |
| `ask_question` | `question` | Keep |
| `request_approval` | `title` | Keep |
| `get_response` | `request_id` | Keep |
| `list_unprocessed` | none | Keep |
| `list_pending` | none | Keep |
| `mark_processed` | `request_id` | Keep |
| `report_execution` | `request_id`, `outcome` | Keep; `outcome` ∈ `accepted\|rejected\|completed\|failed` |
| `update_request` | `request_id`, `expected_version` | Keep; bumps `version` |
| `cancel_request` | `request_id` | Keep; `reason` ∈ `cancelled\|resolved_elsewhere` |
| `notify_user` | `title` | Keep name; no lock-screen chrome |
| `request_feedback` | `title` | Keep name; no Telegram feedback product required |

### Reliability / body / envelope fields to keep

Create/update identity: `external_id` (≤200, required by contract), `version`, `expected_version` (≥1 on update), `request_id`, `expires_in_seconds` (live the previous inbox 60–2_592_000; Paraphe floor 900, default 14400), create `duplicate` flag on read-back.

Card body: `question`≤200, `context`≤2000, `choices`≤4×≤40, `choice_notes`≤4×≤120 (parallel to `choices`), `allow_freeform` (schema only), `title`≤200, `details`≤2000, `message`≤2000 on notify, `recommendation`≤1000, `consequence`≤1000, `prohibitions`≤8×≤200, `risk` ∈ `low\|medium\|high\|critical` (honest; no bulk-approve meaning), `project`≤120, `source_thread`≤200, `links`≤8 URIs, `priority` ∈ `low\|normal\|high\|urgent` (no Focus chrome), `agent_name`≤60, `wait_seconds` 0–60. Written per the 2026-09-24 writing contract above: origin stated, purpose first, tap semantics plain.

Provenance (optional, on the asking tools and `update_request`): `runtime`≤40, `repo`≤120, `worktree`≤120, `ticket`≤200; rendered as the card's identity line, never guessed.

Closeout: `note`≤1000, `result` object (`outcome` `success\|failure\|partial`, `files_changed`, `tests_passed`, `tests_failed`, `commit_message`, `duration_seconds`) as JSON only, `renotify` boolean on update (Telegram = edit/resend, not APNs).

Response envelope after unwrapping MCP `result.content[].text`:

```
request_id, status, version,
response.choice, response.text, response.responded_at, response.responded_via,
processed_at, execution_status, kind, pending
```

`responded_via` keeps live `app` / `auto`. Telegram records `telegram` (tap)
and `telegram-reply` (owner long-press reply); the local owner answer path
records `answer-path`. Additive, not a rename. `auto` is not a product
(Always-allow is dropped).

### Drop as product (accept-and-ignore if clients still send)

Bulk approve; lock-screen chrome; `rule_key` / Always-allow; iOS Focus/silent/time-sensitive push; iPhone result-card UI; Connect-tab onboarding; `responded_via: auto` as a feature.

- Tap surface is a dedicated Telegram bot in a private chat (ADR 0002). the chat topic is not the inbox.
- Card states: `open`, `tapped`, `expired`, `cancelled`. A Telegram callback is a claim. Only `open` plus owner `from.id` plus matching opaque `callback_data` may become a Tap.
- `callback_data` ≤64 bytes and **must bind `card_id` and current `version`** (or equivalent revision nonce). After `update_request`, leftover buttons with the old version refuse even if the action string matches. Card id + action alone is forbidden (ADR 0007, Jude r1 P1-1).
- On tap, cancel, or expiry: edit the bot message and strip the keyboard. Edit failure does not authorize. A late callback still refuses.

```
open + owner + live version + live card → tapped
open + time >= expires_at → expired
update_request → version += 1; old callback_data refuses
any non-open + callback → refuse, no approve
from.id != owner → ignore
```

- Default `expires_in_seconds` is 4 hours (14400). Floor is 15 minutes (900). Below-floor create fails. Live recorded maximum 2_592_000 remains the upper bound.
- Store: user `paraphe`, `/var/lib/paraphe/inbox.sqlite` mode 0600. Snapshot `/var/lib/paraphe/inbox.sqlite.bak` via SQLite online backup before `the daily backup service`. Include `/var/lib/paraphe` in `the backup service's include list` (tracked `the deployment's include list`). That line is **absent today** and is a cutover prerequisite. Keep existing excludes; do not exclude `/var/lib/paraphe`. Restore: restore the directory, prefer `.bak` if live is dirty, start. Git is not the live card store (ADR 0005).
- Return path (ADR 0011; supersedes ADR 0003 and ADR 0006). A waiting call parks until the owner answers or the window ends (≤60 s per call); the `paraphe wait` command repeats that bounded window for the card's whole lifetime; `get_response` / `list_unprocessed` read every answer without a waiter. `source_thread` is descriptive metadata — no wake record is written and no server-initiated turn exists. A session ending with an open card records the request id and its continuation for the next run; no fresh session executes a dead origin.
- Setup: config file plus env (env wins) for bot token, owner Telegram id, default TTL, floor TTL. Owner-only `/config` changes non-secrets later and never echoes the token (ADR 0008).
- Create auth: shared MCP bearer. Tap auth: configured owner Telegram user id only (ADR 0010).
- Cutover (ADR 0004 + 0009), one owner-approved window after the proof card:

  1. Prerequisite: restic include `/var/lib/paraphe` is live.
  2. For **each** of the approved callers — still talking to the previous inbox: add Paraphe bearer + endpoint beside the previous inbox; send no cards to Paraphe; prove `tools/list` equals the twelve names.
  3. Proof card on Paraphe from the primary agent: real high-risk, tap → same Hermes session acts → `report_execution` → `mark_processed`. Notify-only is not this card.
  4. In the same window, switch each harness endpoint to Paraphe and **behaviour-block the previous inbox** (config/deny so that harness cannot create on the previous inbox). Order inside the window: credential already present, then endpoint, then behavior-block. All four in that one window. No dual-write. No fallback to the previous inbox.
  5. the batch caller and crons stay on the previous inbox. Their `external_id` / `project` stay in the batch/git class (its own `project` value). They must not send a flipped caller's decision.

## Testing Decisions

A good test speaks only through the Inbox: create a card, offer a tap claim, poll, report, mark processed. It does not open Telegram, bind a real Hermes session, or read SQLite rows.

The one seam is the Inbox. Drive it with a fake Telegram callback and a fake wake/poll clock.

Must cover, as external behavior:

- Duplicate `external_id` returns the same card and does not notify twice.
- Question vs approval argument names reject a mix.
- Owner tap on `open` with current version records `.response.choice` and does not consume on `get_response`.
- Non-owner `from.id` never records a tap.
- Expired / cancelled / already-tapped claims refuse.
- After `update_request`, a callback carrying the previous version refuses even when action matches.
- Expiry below 15 minutes is rejected; default life is 4 hours when omitted.
- `mark_processed` is idempotent; reads before it still show the tap.
- `report_execution` outcomes are only the four allowed values.
- Missing token or owner id refuses start.
- `/config` from a non-owner is ignored; token is never in a config reply.
- `source_thread` is stored as given and wakes nothing; the answer is read with `get_response` / `list_unprocessed`.
- A waited call returns the answered envelope on a tap inside the window and the pending envelope at window end; `paraphe wait` exits 0 answered, 3 expired or not answerable, 4 unknown.
- `tools/list` returns exactly the twelve names.

No prior test suite exists in this repo. Do not copy the throwaway prototype as production tests; reuse only the state transitions above.

## Out of Scope

- Mobile-app clone
- Public launch or open-source
- the batch caller and unattended cron callers on Paraphe
- Bulk approve, lock-screen chrome, Always-allow as product
- Replacing a Telegram chat topic alerts
- Building or cutting over before this spec is ticketed
- GitHub Issues as a tracker
- Multi-user SaaS
- fallback to the previous inbox after the four callers flip
- Notify-only as the cutover proof card
- Per-caller MCP bearers
- A second encrypted backup destination

## Further Notes

the previous inbox remains live until the proof card in ADR 0009. This document is a specification, not an implementation licence. The next step is `/to-tickets`.
