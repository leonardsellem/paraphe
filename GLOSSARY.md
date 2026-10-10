# Paraphe — product glossary

Nouns only. Implementation stays out of this file.

## Language

**Paraphe**:
The self-hosted owner decision inbox this repository will specify and later build. Telegram is the tap surface. Private until the owner's own use proves it working, safe, and useful.
_Avoid_: clone-of-another-inbox as a product name, a second app, a second chat yes

**Card**:
One owner decision sitting in the inbox: a yes/no approval or a short choice. The owner taps it. The agent must revalidate live state before acting. Default life is 4 hours; never shorter than 15 minutes. On tap, cancel, or expiry the Telegram keyboard is edited off; a late callback still refuses and does not approve.
_Avoid_: ticket, Linear issue, notification that sends you elsewhere, 45-second demo TTL as the product default, leaving dead buttons visible on purpose

**Revision**:
The card `version` (or equivalent nonce) bound into Telegram `callback_data`. A leftover button from before `update_request` is a stale claim and must refuse.
_Avoid_: card id + action as the only callback payload

**Tap**:
The owner's answer on the inbox surface. Silence is not a tap. A tap is not an instruction to skip revalidation.
_Avoid_: chat reply as the decision, bouncing to a web console for the same yes

**Inbox**:
The store and surface that holds cards. Secondary to the authoritative conversation and repository. Never the source of truth. The store is a SQLite file under the configured data location, owned by a dedicated service user, and copied off-box by the daily backup after a consistent snapshot.
_Avoid_: pager, source of truth, task tracker, git as the live card store, a third-party cloud inbox

**Tap surface**:
A dedicated Telegram bot in a private chat with the owner. That chat is where cards are tapped. It is not a pager that sends the owner to another app for the same yes.
_Avoid_: a mobile-app card, a web-console bounce, a chat topic as the decision inbox, a public channel

**Return path**:
How the answer comes back to the session or job that asked, without the owner announcing it in chat. The answer returns through the ask itself: a waited call (`wait_seconds`, ≤60 s per call), the `paraphe wait` command holding the card's whole lifetime, or the boundary sweep (`get_response` / `list_unprocessed`) at the next run. The durable store is the source of the answer. If the asking session is gone, the next run drains the answer and acts; no fresh session executes a dead origin.
_Avoid_: "he'll come back and tell us", treating poll silence as no answer, a server-initiated turn, per-runtime wake integration, starting a fresh agent session to execute a dead origin, the owner as the announcement channel

**Setup**:
How Paraphe is pointed at a bot and an owner without editing code. A config file plus environment variables (env wins). After start, an owner-only Telegram `/config` command can change non-secret knobs. The bot token is never shown back in chat.
_Avoid_: hardcoded TTL, token in git, token echoed in Telegram, /config as the only first-run path

**Owner**:
The single human whose Telegram user id may tap a card or run `/config`. v1 has exactly one. Create is not the owner: create is any caller that presents the shared Paraphe MCP bearer.
_Avoid_: multi-user inbox, tap-from-anyone, Telegram bot as a second create path

**Cutover**:
The planned switch of the approved agent callers from the previous inbox to Paraphe. They flip together after one real high-risk Paraphe card completes tap → same session acts → report → mark processed, and never send that class of decision to both inboxes. Unattended batch callers and crons stay on the previous inbox and may only send different work.
_Avoid_: flipping both inboxes off, dual yes on the same decision, fallback to the previous inbox for a flipped caller, notify-only as the proof card, an unattended caller sending a flipped caller's decision
