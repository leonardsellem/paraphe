# Paraphe — agent operating notes

The owner-decision inbox for agents. This file is the only instruction
file for this repository; a tracked `CLAUDE.md` is a defect (CI guard rejects
it).

## CodeGraph

If `.codegraph/` is missing, run `CODEGRAPH_TELEMETRY=0 codegraph init -y` in this checkout.
Before grep or file reads, run `codegraph explore "<symbol names or question>"` (same output as MCP `codegraph_explore` when present).
Do not run `codegraph install`. Do not commit `.codegraph/`.

## Layout

| Path | Role |
|---|---|
| `src/paraphe/inbox/` | the card lifecycle, the store, the MCP surface, the composition root |
| `src/paraphe/adapters/` | destinations (`console`, `telegram`) and the wake port |
| `tests/inbox/` | the suite |
| `docs/` | specification, decision records, published contracts, demo, launch drafts |
| `tools/` | repository tooling (`scan_private_terms.py`) |
| `config.example.toml` | the documented configuration surface |

## Card surface

`src/paraphe/adapters/telegram.py` renders a card as ordered rich sections
(identity line, kind with a risk word, bold title, context, numbered options
with notes, Recommended, If approved, Limits, links, reply hint, expiry) as
Telegram HTML under a deterministic message budget, and accepts the owner's
long-press reply as the card's text answer on the same claim path as a tap
(`response.text`, `responded_via` `telegram-reply`). The reply is an
owner-side surface: it never gives the create credential a way to answer.

## Run the suite

```bash
python3 -m unittest discover -s tests -p 'test_*.py'
```

A bare `python3 -m unittest` runs zero tests and exits 0. Always pass `-s tests
-p 'test_*.py'`, and read the count.

## Rules

- Nothing outside the standard library at runtime. A dependency needs a reason
  that survives review.
- The suite is the witness. It stays green in every commit, and a change to the
  create/answer credential boundary needs a test that proves the boundary holds.
- The create credential creates and reads. No commit may give it the ability to
  answer a decision, on any surface.
- Sending to a destination is best effort; the store is the source of the
  answer. Never treat a missed wake as no answer.
- No secrets in issues, commits, logs, tests or chat. Credential *names* are
  fine; values never appear.
- No private reference in a tracked file: no personal name, no host or machine
  name, no absolute path from anyone's machine, no private tracker key. The
  scan below enforces it, and its term list lives outside this repository.

## Branches

`dev` is the integration branch: every change lands through a pull request
whose base is `dev`, and `main` accepts pull requests from `dev` only — the
`main-source` check refuses anything else. Both branches refuse direct pushes,
force-pushes and deletion. Commit messages are scanned like the tree: no
private reference, no tracker key.

## Tracker

This repository's own issue tracker, with the triage labels in
`docs/agents/triage-labels.md`. See `docs/agents/issue-tracker.md`.

## Domain docs

Single context: root `GLOSSARY.md` plus `docs/adr/`. See `docs/agents/domain.md`.

## Private-term scan

```bash
python3 tools/scan_private_terms.py
```

Run it before every push. It reports the file and the category, never a matched
value, and it reports honestly when it has no term list rather than passing.

<!-- OPENWIKI:START -->

## OpenWiki

This repository has a generated `openwiki/` evidence index. It is optional just-in-time context, not required startup reading.

- Treat source code and tests as authoritative. A brief's unknowns and review items are verification gaps, not automatic requirements.
- Prefer the narrowest quiet validation that proves the changed behavior. Preserve complete failure output.

The scheduled OpenWiki GitHub Actions workflow refreshes the repository wiki. Do not hand-edit generated OpenWiki pages unless explicitly asked; prefer updating source code/docs and letting OpenWiki regenerate.

<!-- OPENWIKI:END -->
