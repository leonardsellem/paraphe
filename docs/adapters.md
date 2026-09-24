# Adapters

One seam carries everything that is specific to a runtime or a destination.
It is a two-method contract with a worked example in this repository you can
copy.

## Destination: where the owner sees the card and answers it

Paraphe asks a destination to do two things. Nothing else is required.

```python
class Destination:
    def notify(self, payload: dict) -> None:
        """Show the card to the owner. `payload` is the card's fields:
        request_id, version, kind, question/title, details, choices,
        choice_notes, recommendation, consequence, prohibitions, links,
        agent_name, runtime, repo, worktree, ticket, risk, priority,
        expires_at."""

    def edit_and_strip(self, card_id: str, version: int) -> None:
        """The card is no longer answerable. Remove whatever invites an
        answer (buttons, a prompt) so no stale control stays on screen."""
```

That is the whole contract. The card's content is composed by the asking
agent per the writing contract in [`tools.md`](tools.md) — origin stated,
purpose first, tap semantics plain — and the destination renders what it is
given. The payload is structured and the destination
owns its layout: the Telegram adapter renders the card as ordered rich
sections (identity line, kind, bold title, context, numbered options with
notes, Recommended, If approved, Limits, links, reply hint, expiry) as
Telegram HTML under a deterministic message budget, while the console
destination prints its own plain layout from the same fields. `paraphe.adapters.console` is the smallest
complete implementation: it prints the card and the command that answers it,
with no network call, which is why a first run needs no external service.

Owner replies enter through the same Telegram adapter: a private-chat message
from the owner that replies to a card message is resolved by the replied-to
message id over the loaded card state — so it survives a restart — and
recorded as the card's text answer with the same lifecycle writes a tap
makes, the reply channel marked on the answer envelope. A reply to a status
message resolves to nothing, and no tool and never the create credential can
record an answer.

`paraphe.adapters.telegram` is one implementation of the same contract, not
the contract itself. It is the runtime-specific mapping this product was built
against: it encodes the card into an inline keyboard, strips the keyboard when
the card closes, and turns a callback into a claim. A contributor adding a
destination writes those two methods and injects the object; nothing in the
inbox changes.

The composition root (`paraphe.inbox.runtime`) chooses the destination from
configuration: a bot token means the tap adapter, its absence means the
console.

## The return path

There is no delivery seam. The answer returns through the ask itself — a
bounded `wait_seconds` on the call, the `paraphe wait` command, or the
`get_response` / `list_unprocessed` read (ADR 0011). A destination only shows
the card (`notify`) and removes the controls when the card closes
(`edit_and_strip`); it never carries the answer back.
