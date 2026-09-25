---
name: paraphe-owner-decision-writing
description: "Use when composing a Paraphe card for the owner: purpose first in plain words, three or four tap choices with a plus and a minus each, one recommended, honest risk, plain limits."
---

# Paraphe owner decision writing

Use this skill every time you compose a card: `ask_question`,
`request_approval`, `request_feedback`, and any revision through
`update_request`. The owner reads the card away from the code — often on a
phone — and should understand the end purpose in one glance and find a real
option to tap. How a card is written lives here. When to ask, and how to
hold the answer: the [return-path skill](../paraphe-return-path/SKILL.md).

## Install

Same as the return-path skill — one copy step, nothing else is wired: copy
this directory into the runtime's skills directory, next to it.

## Write for a reader who just unlocked their phone

Explain as if to a smart 15-year-old: one concrete situation — what the
owner is looking at or trying to do, what is wrong — then what each choice
changes for them. Write in the language the owner reads every day.
Translating technical nouns into everyday words is not simplification; it
is the job: the displayed figures, dates, actions and outcomes replace the
scope, bounds and lifecycle language that produced them.

- One decision per card. Two decisions are two cards, asked in sequence.
- Do not argue with yourself on the card, and do not recap the
  investigation.
- Do not ask the owner to pick a mechanism; propose one and let them react.
- Say what is done, what is not, and what the tap authorises — and what it
  does not.

## The card, in order

1. **Purpose first.** `question` / `title` says why the action is needed,
   in one line, before the action detail — a verb plus the outcome, never a
   status, never a ticket key. `context` / `details` opens with the why
   (`Pourquoi : …`), then the origin (`D'où ça vient : …` — who asks and
   from where; a worktree names its path in plain words; true values only),
   then the exact command or change.
2. **Choices are actions.** Three or four mutually exclusive things the
   owner can decide, the recommended one first, each label one to three
   ordinary words.
3. **A plus and a minus each.** One real trade-off per choice, one line
   each, in `choice_notes` — not filler.
4. **Why that recommendation.** One sentence in `recommendation`. Then
   stop.
5. **Tap semantics plain.** `consequence` (the *If approved:* section) says
   what the answer sets in motion and what happens if the owner does
   nothing; `prohibitions` (the *Limits:* section) says what it does not
   authorise.

## Choices

Good labels name the outcome, not the machinery:

```
Publish now     Publish tomorrow     Review first     Don't publish
```

A label that needs the card's context to be understood is not a label:
`Approve the rebase of PROJ-123 onto release` makes the owner decode. The
recommended label goes first.

- `ask_question` when real alternatives exist: three or four taps.
- `request_approval` only when the honest answers are yes or no for one
  named action — and even then the body follows the sections above, and
  `consequence` spells out both taps: what Approve does, what Deny does.

## Fields and limits

| Field | Limit | What goes there |
|---|---|---|
| `question` / `title` | 200 | the purpose, one line |
| `context` / `details` | 2000 | why → where → the exact command or change → tap semantics |
| `choices` | 4 × 40 | ordinary words, the recommended one first |
| `choice_notes` | 120 each | the plus and the minus |
| `recommendation` | 1000 | the recommended tap and the one-sentence why |
| `consequence` | 1000 | what the tap sets in motion |
| `prohibitions` | 8 × 200 | what remains forbidden even after yes |
| `risk` | — | honest: production-facing or hard to reverse is never low |
| `external_id` | 200 | stable and descriptive; a retry reuses it |
| `agent_name` / `runtime` / `repo` / `worktree` / `ticket` | 60 / 40 / 120 / 120 / 200 | true values only — never guessed, never invented |

Full field tables: [`docs/tools.md`](../../docs/tools.md).

## Banned on the card

Unless translated into everyday words in the same breath:

- Ticket keys and tracker jargon as the subject — issue, cycle, epic,
  assignee, "move it to In Review".
- Agent-and-repo interior — CI, SHA, rebase, schema, MCP, "the PR is
  green". Say what it means for the owner: "the checks passed", "the
  release is ready".
- A second, unrelated ask bundled into the decision.
- Your own deliberation — pick a recommendation and commit to it.

Names of real things are fine once the purpose is clear: "the public
site", "the family calendar", "the client report". A ticket key belongs at
the end of a sentence as identification, or in `links` — never as the
headline.

## Before you send

- [ ] Sentence one gives the end purpose to someone who never saw the code.
- [ ] One obvious recommended tap, first.
- [ ] Three or four choices, each with a plus and a minus.
- [ ] One decision per card.
- [ ] No untranslated jargon on the card.
- [ ] `risk`, `external_id`, `consequence` and `prohibitions` are filled.
- [ ] You can keep working on something else while the card waits.
