---
name: paraphe-return-path
description: "Use when an agent must ask the owner before acting. Drive a Paraphe card to a decision: ask, hold the return path, revalidate, act once, report, mark processed."
---

# Paraphe return path

Paraphe is an owner decision inbox. An agent raises a card; the owner answers
on their own surface; the answer comes back to the exact session that asked,
with the owner announcing nothing in chat. This skill uses existing waited
calls and background commands, with boundary reads for recovery; it needs no
runtime code change. Interactive Hermes holds a tracked waiter after turn end.

## Install

One copy step per runtime — nothing else is wired:

| Runtime | Where the file goes |
|---|---|
| Hermes | copy this directory under the profile's skills directory (e.g. `~/.hermes/skills/`) |
| Claude Code | copy this directory under `~/.claude/skills/` |
| Codex | point the project's `AGENTS.md` at this file, or copy it into the runtime's skills directory when it has one |
| anything else | give the agent the file, or rely on the served tool text alone |

The waiter needs two things in the environment: the create bearer
(`PARAPHE_MCP_CREATE_BEARER`) and the service endpoint (`PARAPHE_MCP_URL`, or
`PARAPHE_MCP_HOST` + `PARAPHE_MCP_PORT`). A secret is never passed on a
command line.

## The protocol

1. **Ask.** `ask_question` or `request_approval`, with a stable `external_id`.
   Compose the card so the phone alone is enough to decide:
   - **Purpose first.** `question` / `title` says why the action is needed,
     in one line, before the action detail; `context` / `details` opens
     with the why (`Pourquoi : …`), then the exact command or change. The
     why is the goal the action unblocks — never a restatement of the
     command, never a guard's flag text.
   - **Origin stated.** Who is asking and where it runs: `agent_name` and
     `runtime` / `repo` / `worktree` / `ticket` render as the card's
     identity line (the `D'où ça vient : …` line when the body carries
     it). Never guessed — but when the ask runs in a worktree or folder,
     naming that path is required (plain words: `dossier de travail …`).
     "Optional" covers what you do not have; it is never a reason to omit
     a true where.
   - **Pre-ask flagged work.** A command the guard will flag (protected
     writes, resets, deploys) is approved through a card you compose —
     with its purpose and origin — before you run it. The runtime's
     auto-approval card is a bare fallback (flagged command, no purpose);
     never plan on it.
   - **Tap semantics plain.** What the answer authorises and what it does
     not: `consequence` (what the approval sets in motion) and
     `prohibitions` (what it does not authorise). On an approval say what
     Approve does and what Deny does.
   Record the `request_id`, `external_id`, `kind`, `version`, origin and
   literal effect scope in your own continuation record. The create result
   names the reading path (`read_with: get_response`). Recover a lost create
   ACK through the same `external_id`, never a duplicate question.
2. **R3** — **Hold the interactive Hermes return path.** After any create
   whose answer you have not consumed, including a duplicate create, start
   `terminal(background=true, notify=true, command="paraphe wait <request_id>")`
   in the same creating turn, before ending the turn. Retain its tracked task
   ID and verify it is running, or consume its already-completed result below;
   reuse an existing owned waiter rather than starting a second one. A finite
   waited read (even 60 seconds) and a boundary sweep are not asynchronous return paths.
   `wait_seconds` (0–60) can return an answer inside its window; the waiter
   repeats those windows until the card is terminal. The store is the source
   of the answer; boundary reads recover missed answers, not future wake-ups.
3. **R4** — **Re-read and admit once.** On the exit `0` completion turn,
   call `get_response` with the exact `request_id`; do not execute stdout.
   Match your own origin/continuation record, `external_id`, `kind`, unchanged
   `version`, actionable `response.choice`, null `processed_at` and current mandate.
   Require answered status and a choice in the current `choices`.
   Revalidate live target state and literal scope before acting once. An
   approval requires `Approve`; silence or a free-text question is not approval.
   No action for a foreign card, stale version, Deny, expired, cancelled,
   unknown, already processed or superseded answer. Leave foreign cards to
   their owner. For your own unprocessed answered approval with invalid
   authority, `report_execution` with `outcome: rejected` and the reason,
   then `mark_processed` and read back; never alter a current pending version
   or an unknown/processed card. Reconcile an existing execution report or
   effect receipt instead of repeating the effect. Only the asking session or
   its verified compressed successor may continue, once; no fresh session executes a dead origin.
   A next-run sweep reconciles ownership before any continuation.
4. **R5** — **Record terminal waits without re-asking.** Exit `3` means
   expired or not answerable; exit `4` means unknown request ID. Take no action,
   record the request ID, exit and reason in the session receipt, and send no
   replacement card without a new reason. A transport/startup failure is
   unknown, not an answer: record it and recover the same card, not a new ask.
5. **R6** — **Close after the effect.** Verify the actual effect and record
   its identity/result, then `report_execution` for an approval with the honest
   `outcome` (`accepted`, `completed`, `failed` or `rejected`), followed by
   `mark_processed`; read back the exact card's `execution_status` and non-null
   `processed_at`. For a question, record its decision and verified result
   before `mark_processed`, then read back `processed_at`; do not report
   approval execution for a question. A waiter exit, notification or transport ACK
   is never effect completion. If closeout fails, reconcile the effect receipt
   and retry closeout only, never the action.
6. **R8** — **Hand off one-shot lanes honestly.** `hermes chat -q` does not
   drain completion notifications: record `request_id`, the binding above and
   continuation for its interactive parent, which starts and owns the tracked
   waiter. In a one-shot lane, never promise automatic return. At session end,
   preserve that record for the owning run's recovery; do not launch a fresh
   executor for a dead origin.

## Per-runtime idioms

| Runtime | Hold the return path | How the answer comes back |
|---|---|---|
| Interactive Hermes | `terminal(background=true, notify=true, command="paraphe wait <request_id>")` in the creating turn | completion turn re-reads the exact own card; R4–R6 |
| One-shot Hermes (`hermes chat -q`) | record request ID + continuation; interactive parent owns the waiter | no automatic return promised by the one-shot lane; R8 |
| Claude Code | Bash `run_in_background` around `paraphe wait <id>` | the background-task completion notice; read its output |
| Codex | background command plus its completion notice, where available | the notice; otherwise the boundary sweep at the next run |
| MCP client with a waitable call | `get_response` with `wait_seconds` | the call returns the envelope itself |
| any shell runner | `paraphe wait <id> &` then `wait $!` | when the process exits |
| no background facility | — | boundary sweep: `list_unprocessed` at each run |

## Rules

- The create credential creates and reads. It cannot answer a card; never try
  to. Only the owner answers.
- Say where you ask from: `runtime` (≤40), `repo` (≤120), `worktree` (≤120)
  and `ticket` (≤200) ride the asking tools and `update_request`; the card
  renders them as its identity line. Never guess them — supply what is true
  or leave them out.
- The owner may answer with a long-press reply: the words arrive as
  `response.text` with `responded_via` `telegram-reply`. If the reply asks a
  question or is not a decision, do not execute it — explain and re-ask with
  a fresh card.
- An answer is bound to the card `version`; after `update_request`, stale
  answers refuse. Re-read before acting if you revised the card.
- `paraphe ask "<question>" --external-id <id>` is the shell-side create; it
  prints the request id. A duplicate `external_id` returns the existing card
  and does not notify twice.
- Keep `wait_seconds` at 60 or below per call; the waiter command loops
  internally, so a decision can take hours.
