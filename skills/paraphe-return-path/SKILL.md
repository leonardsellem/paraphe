---
name: paraphe-return-path
description: "Use when an agent must ask the owner before acting. Drive a Paraphe card to a decision: ask, hold the return path, revalidate, act once, report, mark processed."
---

# Paraphe return path

Paraphe is an owner decision inbox. An agent raises a card; the owner answers
on their own surface; the answer comes back to the exact session that asked,
with the owner announcing nothing in chat. This skill is the protocol, and it
needs no code on the runtime side: the return path is a waited call, a
background command, or a read at the next boundary.

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
     with the why (`Pourquoi : …`), then the exact command or change.
   - **Origin stated.** Who is asking and where it runs: `agent_name` and
     `runtime` / `repo` / `worktree` / `ticket` render as the card's
     identity line (the `D'où ça vient : …` line when the body carries
     it). They are optional and never guessed — supply what is true or
     leave them out.
   - **Tap semantics plain.** What the answer authorises and what it does
     not: `consequence` (what the approval sets in motion) and
     `prohibitions` (what it does not authorise). On an approval say what
     Approve does and what Deny does.
   Record the `request_id` the create result returns — it also names the
   reading path (`read_with: get_response`).
2. **Hold the return path.** Use the first that the runtime can do:
   - **Wait in the call** — `wait_seconds` (0–60) on `ask_question`,
     `request_approval` or `get_response`. The call parks until the owner
     answers inside the window or the window ends, then returns the reading
     envelope: answered, or still pending. Use a short window when the owner
     is at hand; issue another bounded call otherwise, or hand off to a
     waiter.
   - **Background the waiter** — `paraphe wait <request_id>` blocks until the
     card is answered or can no longer be answered, and prints the answer.
     Exit codes: `0` answered (envelope JSON on stdout), `3` expired or not
     answerable, `4` unknown request id. Start it in the background and let
     the runtime notify you when it exits.
   - **Sweep at boundaries** — at each new run, read `list_unprocessed` /
     `get_response` for your request ids. This fallback always works: a
     missed wait loses nothing, because the store is the source of the
     answer.
3. **Act correctly.** The card is not the source of truth: revalidate live
   state before acting; silence is never approval; execute once; then
   `report_execution` (for approvals) and `mark_processed`. A reply from the
   owner can be a question or not a decision; that is not an instruction to
   execute — explain, then re-ask with a fresh card.
4. **Hand off when you end.** A session ending with an open card records the
   `request_id` and its continuation in its own notes, so the next run can
   drain the answer and finish the work.

## Per-runtime idioms

| Runtime | Hold the return path | How the answer comes back |
|---|---|---|
| Hermes | terminal task: `paraphe wait <id>` in the background, with a completion notification | the completion notice; read the task's output |
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
