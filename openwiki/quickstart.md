---
type: guide
title: Quickstart
description: What Paraphe is, the two credentials an operator supplies, every route to a running inbox (local console, shell ask/wait, container, phone), the owner's answer channels, the owner-side `check telegram` and `store relocate` commands, and which wiki page owns each subsystem.
tags: [quickstart, setup, onboarding, routing]
verified:
  - by: openwiki/0.5.0
    at: 2026-09-24T17:26:41.197Z
sources:
  - id: openwiki-source-6d4b4e707b8d60b6ccfa3425
    resource: repo://.github/workflows/openwiki-update.yml
  - id: openwiki-source-ea70eb6c045047448e446296
    resource: repo://.gitignore
  - id: openwiki-source-8037e2358a2c4f9b2c722a11
    resource: repo://AGENTS.md
  - id: openwiki-source-7aa209ee4f993345d7092214
    resource: repo://config.example.toml
  - id: openwiki-source-39c3295efc089133e87a9c80
    resource: repo://CONTEXT.md
  - id: openwiki-source-bb1ebe868e35e9e500714501
    resource: repo://Dockerfile
  - id: openwiki-source-2cdf19b87eb8c780238e9aca
    resource: repo://docs/adapters.md
  - id: openwiki-source-e760953dd96f8649b8974df3
    resource: repo://docs/demo/console-loop.md
  - id: openwiki-source-0bbd43419c0bf3b818cb5a2d
    resource: repo://docs/tools.md
  - id: openwiki-source-05ccef8d4cf1698187f20464
    resource: repo://pyproject.toml
  - id: openwiki-source-23775c3de52f3ab95a13cb8b
    resource: repo://README.md
  - id: openwiki-source-88c5dcf0e67cc0304ebe833c
    resource: repo://src/paraphe/__main__.py
  - id: openwiki-source-3f0923f394ad3a64d983d48d
    resource: repo://src/paraphe/adapters/console.py
  - id: openwiki-source-bc0ad19ae022e944fc077703
    resource: repo://src/paraphe/adapters/telegram.py
  - id: openwiki-source-323578bac7c22161d0113db8
    resource: repo://src/paraphe/check.py
  - id: openwiki-source-83b4724c0939d8570eedb33f
    resource: repo://src/paraphe/cli.py
  - id: openwiki-source-3f834a992df5ac81007614a4
    resource: repo://src/paraphe/inbox/__init__.py
  - id: openwiki-source-872ba00e35eb81073c2713f0
    resource: repo://src/paraphe/inbox/config.py
  - id: openwiki-source-a8049e4c38fe5c6127cbfcaf
    resource: repo://src/paraphe/inbox/http.py
  - id: openwiki-source-ff9e45a3ac72725a5bcca301
    resource: repo://src/paraphe/inbox/runtime.py
  - id: openwiki-source-d39aa17b1580d696b9e0586e
    resource: repo://src/paraphe/inbox/store.py
  - id: openwiki-source-f8eb69b469a332aa25c109f6
    resource: repo://src/paraphe/store_cli.py
  - id: openwiki-source-72bdc2134cc6aed6125ac0b0
    resource: repo://tests/inbox/test_mcp_lifecycle.py
  - id: openwiki-source-17bf8b7b171c9db569415c2e
    resource: repo://tests/inbox/test_setup.py
generated: { by: "openwiki/0.5.0", at: "2026-09-24T17:26:41.197Z" }
---

# Quickstart

Paraphe is the owner-decision inbox for agents: an agent raises one decision, the
owner answers it, and the same asking run picks the answer up and carries on. It
is deliberately not a workflow engine, a task tracker or a chat client.

**The credential an agent holds creates and reads. It cannot answer.** Answering
takes the owner's own credential on a path the agent's cannot reach. That
asymmetry is the product, and it is not a setting.

## What you need

Python 3.11 or newer, and nothing else: the runtime imports only the standard
library, so using Paraphe pulls no dependency tree. Installing it fetches the
build backend (`hatchling`) once, at install time.

A local run needs exactly two values you make up:

| Value | Who holds it |
|---|---|
| `mcp_create_bearer` | the agent. It creates cards and reads answers. |
| `owner_answer_token` | you. It answers them, and it is never given to an agent. |

Both come from `paraphe.toml` or the environment
(`PARAPHE_MCP_CREATE_BEARER`, `PARAPHE_OWNER_ANSWER_TOKEN`), with the
environment winning per key. Setup refuses an answer credential equal to the
create bearer, and refuses to start a local instance with no answer credential
at all — an inbox that cannot answer is not an inbox.
`config.example.toml` is the documented minimum, and `paraphe.toml` is in
`.gitignore`, so the file you fill in is not the file you commit. `.gitignore`
also keeps the local-only run artifacts out of commits —
`docs/plans/*.companion.md`, `docs/plans/*.goal.txt` and the in-flight
`openwiki/.run.json` — so a commit from a working checkout carries none of them.

These are the working forms of the `paraphe` command, and each one maps to a
route or an owner-side step described below. The dispatch order, the usage text
and the refusal for anything else are owned by
[composition root and runtime](/openwiki/architecture/composition-root-and-runtime.md).

| Invocation | What it is here |
|---|---|
| `paraphe [--config PATH]` | start the server — routes A to D |
| `paraphe ask QUESTION` / `paraphe wait REQUEST_ID` | the shell half of the loop — route B |
| `paraphe check telegram` | the phone preflight — route D |
| `paraphe store relocate [--move]` | the store upgrade — route A |
| `paraphe --version`, `paraphe --help` | version and usage |

## Route A — run it locally, answering on the console

```bash
python3 -m venv .venv
.venv/bin/pip install .

cp config.example.toml paraphe.toml
# fill in the two values above, and delete the bot_token / owner_telegram_id
# placeholder lines: a bot token, however bogus, selects the phone destination

.venv/bin/paraphe --config paraphe.toml
```

```
Paraphe ready: mcp http://127.0.0.1:8787/mcp answer-path on
```

With no bot token the destination is the console: the card is printed where you
are looking, together with the `curl` command that answers it. The default bind
is loopback `127.0.0.1:8787` — `runtime.DEFAULT_MCP_PORT` and `cli.DEFAULT_PORT`
are both `8787` — and the ready line is printed from the socket that was
actually bound, so a run that sets `PARAPHE_MCP_PORT` prints that port instead;
the recorded demo's `8899` is one such run.
[Composition root and runtime](/openwiki/architecture/composition-root-and-runtime.md)
owns the bind, the ready line and the startup checks.

The store is a SQLite file in your per-user data directory
(`$XDG_DATA_HOME/paraphe`, else `~/.local/share/paraphe`) unless `store_path`
points elsewhere. Upgrading from a release that kept it in `/var/lib/paraphe`?
Stop the running server and run the relocation:

```console
paraphe store relocate           # copies /var/lib/paraphe/inbox.sqlite to the
                                 # per-user default, verifying it first and
                                 # keeping the old file as a backup
paraphe store relocate --move    # the same copy, then remove the old store
```

Setting `store_path` to the old location — `store_path =
"/var/lib/paraphe/inbox.sqlite"` — remains available when relocation is not
wanted, because Paraphe refuses to start on the default while a store exists
only there, rather than beginning a second, empty inbox.
[Owner-side commands](/openwiki/operations/owner-side-commands.md) owns the
relocation steps, its refusals and its exit codes;
[data location and backup](/openwiki/operations/data-location-and-backup.md)
owns the resolver, the modes and backups.

## Route B — ask from a shell

A runner that can only run commands still gets its answer back:

```bash
request_id=$(paraphe ask "Deploy to production?")
paraphe wait "$request_id" &   # prints the answered envelope when the owner answers
```

`paraphe ask` creates the card over the same MCP surface and prints the request
id on stdout. A backgrounded `paraphe wait <request_id>` holds the card's
lifetime in repeated bounded windows and ends when the owner answers: exit `0`
answered, `3` expired or not answerable, `4` unknown request id. Where the
endpoint and the create bearer come from, and every other option, is on
[the ask and wait commands](/openwiki/integrations/ask-and-wait-cli.md); the
parking and waking those windows are built on is
[the wait engine](/openwiki/architecture/wait-engine.md).

## Route C — run it in a container

```bash
mkdir -p paraphe-data && chmod 0777 paraphe-data
docker build -t paraphe .
docker run --rm -v "$PWD/paraphe-data:/data" \
  -e PARAPHE_MCP_CREATE_BEARER=... -e PARAPHE_OWNER_ANSWER_TOKEN=... paraphe
```

The image keeps its data outside itself (`PARAPHE_STORE_PATH=/data/inbox.sqlite`
on a mounted `VOLUME /data`) and runs as the unprivileged `paraphe` user, which
is why the mounted directory has to be made writable as in the first line. It
keeps the default loopback bind inside the container, so publishing the port is
not a route to an answerable inbox.

Reaching it from outside is where the two startup checks bite. `Runtime.start`
refuses to start at all when an answer credential is configured with a host that
is not loopback, and the HTTP surface refuses any bind that is neither loopback
nor a Tailnet address. A deployment that must be reachable from elsewhere
therefore configures the phone destination, leaves the answer credential unset,
and binds a loopback or Tailnet address; the README's outside-reachable recipe
names `PARAPHE_MCP_HOST=0.0.0.0`, which that bind rule refuses as a public
wildcard rather than serving.
[Composition root and runtime](/openwiki/architecture/composition-root-and-runtime.md)
owns both checks and
[the credential boundary](/openwiki/security/credential-boundary.md) explains
why the answer path is loopback-only.

## Route D — point it at a phone

Add a Telegram bot token and your Telegram user id to `paraphe.toml` (or
`PARAPHE_BOT_TOKEN` / `PARAPHE_OWNER_TELEGRAM_ID`) and decisions arrive as a
message with buttons instead of on the console. The README's three setup steps
are: create the bot with BotFather and keep the token private, send the bot
`/start` and find your own numeric Telegram user id, then set both values and
prove the pair before starting the server:

```bash
.venv/bin/paraphe check telegram
.venv/bin/paraphe --config paraphe.toml
```

The check names the bot and sends one plain setup message to your phone; it
prints no token and creates no card, and every way it can refuse is on
[owner-side commands](/openwiki/operations/owner-side-commands.md). The bot
token's presence is what selects the destination, and with a phone destination
the answer credential becomes optional, because the owner answers by tapping.
Tapping is not the only gesture: the card ends with a reply hint, and a
long-press reply to it answers that card in the owner's own words — a reply that
is a question rather than a decision is explained rather than executed.
[The rendered card](/openwiki/integrations/telegram-card-rendering.md) is the
message the owner reads;
[owner replies as answers](/openwiki/integrations/owner-reply-intake.md) is what
becomes of the reply; the transport mechanics are on
[the Telegram tap surface](/openwiki/integrations/telegram-tap-surface.md).

Every route serves the same tool surface — see
[the served MCP surface](/openwiki/architecture/mcp-surface.md) — so the tools
do not change, only where the owner sees the card.

## The loop, end to end

```
ask → you see the card → you answer → the asking run resumes
```

The owner answers on whichever destination the run chose, and all three channels
are the same kind of event: a tap on the phone
([the Telegram tap surface](/openwiki/integrations/telegram-tap-surface.md)), a
long-press reply to the card message in the owner's own words
([owner replies as answers](/openwiki/integrations/owner-reply-intake.md)), or
the local answer path that the console route prints a `curl` line for. The card
records which channel the answer actually arrived through; why only the owner
can use any of them is
[the credential boundary](/openwiki/security/credential-boundary.md).

The answer then returns through the ask itself, by any of three equal routes: the
call parks with `wait_seconds`, the backgrounded `paraphe wait` above holds the
card's lifetime, or a run that missed both drains the answer with `get_response`
and `list_unprocessed` at its next boundary. Nothing has to be said in chat.
Showing the card is best effort; the durable store is the source of the answer,
so a notification or a wake that never arrived is not a lost decision.
[The wait engine](/openwiki/architecture/wait-engine.md) owns the parking, and
[card lifecycle](/openwiki/architecture/card-lifecycle.md) owns what a card is
while all this happens.

`docs/demo/console-loop.md` is a recorded run of that on a clean checkout, with
no third-party credential and no external service:

```
1. asked      : the agent called ask_question with its create credential
2. refused    : the same credential presented to the answer path returned 401,
                no claim was recorded, and the card was unchanged
3. answered   : the owner answered through the owner-only path
4. resumed    : the agent polled and found the answer, responded_via answer-path
5. closed     : mark_processed acknowledged the card
```

Step 2 is the one worth reading twice. Everything else is plumbing; that line is
the product.
[The ask → answer → resume workflow](/openwiki/workflows/ask-answer-resume.md)
traces the whole loop with every hop named.

## Where to read next

Quickstart routes; these pages are the detail.

| If you are… | Read |
|---|---|
| learning the words (card, revision, tap, return path, owner) | [domain vocabulary and superseded decisions](/openwiki/concepts/domain-vocabulary.md) |
| writing an agent against it | [the served MCP surface](/openwiki/architecture/mcp-surface.md) and `docs/tools.md` (the in-repo tool surface contract) |
| tracing one decision end to end | [the ask → answer → resume workflow](/openwiki/workflows/ask-answer-resume.md) |
| changing what a card is or how it closes | [card lifecycle](/openwiki/architecture/card-lifecycle.md) |
| changing how a call parks until the owner answers | [the wait engine](/openwiki/architecture/wait-engine.md) |
| asking why the boundary is where it is | [the credential boundary](/openwiki/security/credential-boundary.md) |
| wiring a shell-only runner | [the ask and wait commands](/openwiki/integrations/ask-and-wait-cli.md) |
| running or changing the Telegram bot | [the Telegram tap surface](/openwiki/integrations/telegram-tap-surface.md) |
| changing what the owner sees on the phone | [the rendered card](/openwiki/integrations/telegram-card-rendering.md) |
| changing how a long-press reply becomes the answer | [owner replies as answers](/openwiki/integrations/owner-reply-intake.md) |
| adding a destination | [adapters: the destination seam](/openwiki/extension/adapters-and-tool-surface.md) and `docs/adapters.md` |
| changing startup, settings or the ready line | [composition root and runtime](/openwiki/architecture/composition-root-and-runtime.md) |
| running the owner-side commands (`check telegram`, `store relocate`) | [owner-side commands](/openwiki/operations/owner-side-commands.md) |
| moving the data or backing it up | [data location and backup](/openwiki/operations/data-location-and-backup.md) |
| running the tests or landing a change | [suite and integration](/openwiki/testing/suite-and-integration.md) and `CONTRIBUTING.md` |

`AGENTS.md` is the repository's canonical instruction file. Its `Card surface`
section summarizes the Telegram card's ordered sections and the owner's
long-press reply entering the same claim path as a tap — the detail is on
[the rendered card](/openwiki/integrations/telegram-card-rendering.md) and
[owner replies as answers](/openwiki/integrations/owner-reply-intake.md) — and
its OpenWiki note records that the generated `openwiki/` index is optional
just-in-time context refreshed by a scheduled workflow, not required startup
reading, with source and tests staying authoritative.

Alongside these pages, in the repository: `CONTEXT.md` for the vocabulary,
`docs/adr/` for the decisions behind the shape, `docs/specs/paraphe-v1.md` for
the v1 specification, `docs/roadmap.md` for what is next, and
`skills/paraphe-return-path/SKILL.md` for the async return protocol.

The README's documentation table now points at the generated wiki —
`openwiki/index.md`, `openwiki/quickstart.md` and `openwiki/architecture/` —
rather than only at in-repo files; `docs/tools.md` and `docs/adapters.md` were
dropped from that table but remain in the repository and are still cited by the
demo and by the rows above.
