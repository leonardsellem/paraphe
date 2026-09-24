---
type: integration
title: The ask and wait commands
description: The shell client any runner can background — how paraphe ask creates a card and prints its request id, how paraphe wait holds the card's lifetime in repeated bounded windows, and what each exit code, endpoint and credential resolution rule means.
tags: [cli, return-path, mcp-client, exit-codes, shell-integration]
sources:
  - id: openwiki-source-4248812be758ec7360356412
    resource: repo://docs/adr/0011-answer-returns-through-the-ask.md
  - id: openwiki-source-0bbd43419c0bf3b818cb5a2d
    resource: repo://docs/tools.md
  - id: openwiki-source-05ccef8d4cf1698187f20464
    resource: repo://pyproject.toml
  - id: openwiki-source-88c5dcf0e67cc0304ebe833c
    resource: repo://src/paraphe/__main__.py
  - id: openwiki-source-83b4724c0939d8570eedb33f
    resource: repo://src/paraphe/cli.py
  - id: openwiki-source-3f834a992df5ac81007614a4
    resource: repo://src/paraphe/inbox/__init__.py
  - id: openwiki-source-a8049e4c38fe5c6127cbfcaf
    resource: repo://src/paraphe/inbox/http.py
  - id: openwiki-source-ff9e45a3ac72725a5bcca301
    resource: repo://src/paraphe/inbox/runtime.py
  - id: openwiki-source-17bf8b7b171c9db569415c2e
    resource: repo://tests/inbox/test_setup.py
  - id: openwiki-source-ec516ae95f07d4f7e51ef3b6
    resource: repo://tests/inbox/test_wait_engine.py
generated: { by: "openwiki/0.5.0", at: "2026-09-24T17:26:41.197Z" }
verified:
  - by: openwiki/0.5.0
    at: 2026-09-24T17:26:41.197Z
---

# The ask and wait commands

`src/paraphe/cli.py` is the shell-side half of the return path. `paraphe ask`
creates a card over the MCP surface and prints its request id; `paraphe wait
<request_id>` holds that card's lifetime and prints the answered envelope. A
runner with no tool bindings — a cron job, a background terminal task, any agent
that can only run commands — asks, backgrounds the waiter, and reads the answer
from the command's stdout.

This is a first-class route of the return path, not a fallback. ADR 0011 names
the waiter command beside the waited call and the poll read, and states that no
per-runtime integration exists anywhere: the waiter is a plain command any runner
can background, and a client that cannot background it drains the answer with
`get_response` / `list_unprocessed` at its next boundary. The lifecycle the
commands sit inside — who answers, how the answer is recorded, what the asking
session does next — is [ask, answer, resume](/openwiki/workflows/ask-answer-resume.md);
this page covers only the command itself.

## The surface as parsed

| Command | Positional | Options |
|---|---|---|
| `paraphe ask QUESTION` | exactly one question | `--context TEXT`, `--choice TEXT` (repeatable), `--external-id ID`, `--agent-name NAME`, `--url URL` |
| `paraphe wait REQUEST_ID` | exactly one request id | `--url URL` |

`ask` forwards its options straight into the surface's field names —
`--context` → `context`, `--choice` → `choices`, `--external-id` → `external_id`,
`--agent-name` → `agent_name` — and sends them without counting or validating
them itself. The service's limits therefore arrive as tool errors on stderr
with exit `1`: at most four choices of 40 characters each (`choices is too
long`), `external_id` ≤200, `agent_name` ≤60, `context` ≤2000.

Both commands print the usage text and exit `0` on `--help` or `-h` anywhere in
the tail of the line — `paraphe ask --help` is the discovery path the top-level
usage points at. Called with no arguments at all, `cli.main` prints the same
text (the installed `paraphe` with no arguments instead prints the top-level
usage and, when configuration is present, starts the service). The
parser accepts options **only** in the `--name value` form: there is no
`--name=value` spelling, an unknown name is `paraphe: unknown option --name`, and
a name with no following token is `--name needs a value`. Every one of those, and
a wrong number of positionals (`ask needs exactly one QUESTION`, `wait needs
exactly one REQUEST_ID`), is one `paraphe: <message>` line on stderr with exit
`1`, decided before any HTTP call.

Two limits of the surface follow from the parsing:

- `ask` calls exactly one tool, `ask_question`. There is no way to create an
  approval, feedback or notify card from the shell.
- without `--external-id`, `ask` generates `cli-<uuid4 hex>`, so each invocation
  creates a new card and re-running the same bare command is *not* idempotent.
  Passing a stable `--external-id` is what makes a repeat return the existing
  card and notify the owner only once.

`paraphe.__main__:main` matches `check` and `store` first, and then hands `ask`
and `wait` to `paraphe.cli.main`; every one of those routes is matched *before*
the `--config` loop, and the entry point is the `paraphe` console script
(`paraphe = "paraphe.__main__:main"`) plus `python3 -m paraphe`. The client path
never consumes `--config`, never reads the file for anything but the bearer, and
needs the service already running; handing `--config` to `ask` or `wait` is
`paraphe: unknown option --config` with exit `1`, because neither command
accepts it. The exit code `2` of the top-level service path (`--config` without
a path, a `SetupError` from the server) is not one of the client's codes.

## Where it points

`_endpoint` resolves in this order, and the first non-blank value wins:

1. `--url` on `ask` or `wait`;
2. `PARAPHE_MCP_URL`;
3. `http://PARAPHE_MCP_HOST:PARAPHE_MCP_PORT/mcp`, with `cli.DEFAULT_HOST`
   `127.0.0.1` and `cli.DEFAULT_PORT` `8787` when those variables are unset
   (blank counts as unset).

```bash
paraphe ask "Ship the cut?" --url http://127.0.0.1:8787/mcp
PARAPHE_MCP_URL=http://100.64.1.2:8787/mcp paraphe wait "$REQUEST_ID"
```

The defaults repeat the server's own bind defaults — `PARAPHE_MCP_HOST` and
`PARAPHE_MCP_PORT` are the same two variables the service reads, with the same
`127.0.0.1` and `8787` fallbacks — so a client running on the service's host
with the same environment derives the address the service actually bound. The
client does not range-check the port, where the server validates `0`–`65535`.
The endpoint must end at a path the server accepts as its MCP surface (`/mcp`,
`/mcp/`, `/`), which is what the derived default builds; a URL with no path at
all, such as `http://127.0.0.1:8787`, is not one of those and gets `404`, which
`_call` raises as a `CliError`.

The derived default is still the client's own idea of where the service is. The
ready line the service prints names whatever address it bound to —
`127.0.0.1`, a Tailnet address, or `0.0.0.0` — so a client that is not on that
host with that environment is handed the reachable URL explicitly through
`--url` or `PARAPHE_MCP_URL`. See
[composition root and runtime](/openwiki/architecture/composition-root-and-runtime.md).

## What it authenticates with

`_bearer` resolves the credential in this order, and the command line is never
one of the sources:

1. `PARAPHE_MCP_CREATE_BEARER`, when present and not blank;
2. `mcp_create_bearer` in the TOML file named by `PARAPHE_CONFIG_PATH`, else
   `./paraphe.toml` — read fresh on each invocation, and only that one key.

A missing file, an unreadable file, a file that is not valid TOML, and a key that
is absent or not a non-empty string all resolve to the same `None`. So a broken
config file is not distinguished from an absent credential: both produce
`paraphe: no create bearer: set PARAPHE_MCP_CREATE_BEARER or mcp_create_bearer`
on stderr and exit `1` **before any HTTP request is made**.

The credential is the **create bearer** — the one that creates and reads. Both
commands present it; neither can answer a card, and there is no flag that accepts
the owner credential. That is the boundary described in
[the credential boundary](/openwiki/security/credential-boundary.md), not a gap
in the client.

## What it speaks on the wire

Every HTTP request is a JSON-RPC 2.0 `tools/call` `POST` to the endpoint, with
`Content-Type: application/json`, `Accept: application/json` and
`Authorization: Bearer <create bearer>`. The same Streamable HTTP surface every
other client speaks, and nothing else: the command performs **no `initialize`
handshake**, sends no `Mcp-Session-Id`, and since its `Accept` header names JSON
without `text/event-stream`, the server answers with the JSON framing rather than
an SSE frame. The transport is `urllib.request` — the client is standard library
only, like the rest of Paraphe.

A successful reply must be a JSON object carrying a `result` object; the payload
is `result.content[0].text`, parsed as JSON. `_call` classifies everything else:

| Wire outcome | Raised | Message |
|---|---|---|
| any non-2xx status (401 wrong bearer, 404 wrong path) | `CliError` | `the service refused the call (HTTP <code>)` |
| connection refused, DNS failure, timeout, socket error | `CliError` | `the service is unreachable` |
| a `200` body that is not JSON at all | `CliError` | `the service is unreachable` |
| a JSON-RPC `error` object, or no dict `result` | `CliError` | the error's `message`, else `the service returned an invalid response` |
| a `result` with no usable `content[0].text`, or a `text` that is not JSON | `CliError` | `the service returned an invalid response` |
| `result.isError` | `ToolError` | the server's own refusal text, e.g. `unknown request_id`, `choices is too long`, `question is too long` |

The refusal vocabulary of the surface is message text under HTTP `200`, not a
status code, which is why the tool error above is what the commands read. A
`200` body that is not JSON is caught by the same client-side
`except (URLError, OSError, ValueError)` as a socket failure — the `json.loads`
runs inside that block — so it is reported as `the service is unreachable`
rather than as an invalid response, and `wait` counts it against its transport
budget. The other `CliError` classifications are raised inside `_call` as well,
so `wait` charges every one of them to the same budget instead of failing fast.

## `ask` — create and print

`ask` resolves the bearer (exit `1` if absent), builds the arguments, makes one
`ask_question` call, requires the result to be a dict with a truthy `request_id`,
and prints **only** that id — one line on stdout — then exits `0`.

It sends no `wait_seconds`, so the create returns as soon as the card exists:
**`ask` deliberately does not sleep**. The window is the waiter's job, and the
server's park only applies to a call that asked for one. The other fields a
create returns (`duplicate`, `version`, `read_with: get_response`) are dropped in
the printed output; a runner that needs them reads the surface. A tool error, an
unreachable service or a reply without a `request_id` is one stderr line and exit
`1`, with no retry — the create either happened or it did not, and a repeated
`--external-id` is the idempotent way to ask again.

## `wait` — hold the card's lifetime

`wait_for_answer` runs one loop whose size is deliberately bounded:

| Constant | Value | Role |
|---|---|---|
| `WINDOW_SECONDS` | `60.0` | the per-call window asked for, and the most it will ever ask for |
| `REQUEST_TIMEOUT` | `WINDOW_SECONDS + 30` (90 s) | socket timeout for each HTTP call |
| `TRANSPORT_ATTEMPTS` | `20` | consecutive transport failures tolerated |
| `TRANSPORT_BACKOFF_SECONDS` | `3.0` | sleep between those attempts |

The window is clamped before the first call: `window = max(0.0, min(float(window),
WINDOW_SECONDS))`. The command can therefore never ask the service for a window
the surface would refuse (`wait_seconds` is validated `0`–`60` there), and the
socket timeout is derived from the **maximum** window, not recomputed from the
argument, so even a shortened window gets 90 seconds of slack.

```mermaid
flowchart TD
    enter["wait_for_answer with a request id"] --> bearer{"create bearer resolved"}
    bearer -- no --> e1["one stderr line, exit 1"]
    bearer -- yes --> clamp["window clamped to at most 60 seconds"]
    clamp --> call["_call get_response with wait_seconds"]
    call --> transport{"CliError"}
    transport -- yes --> count["failures plus one"]
    count --> cap{"failures below 20"}
    cap -- yes --> back["sleep 3 seconds"]
    back --> call
    cap -- no --> e2["print the error, exit 1"]
    transport -- no --> tool{"ToolError"}
    tool -- yes --> unknown{"message contains unknown request_id"}
    unknown -- yes --> e4["one stderr line, exit 4"]
    unknown -- no --> e1
    tool -- no --> status{"envelope status"}
    status -- "answered or acknowledged" --> ok["print the envelope, exit 0"]
    status -- "expired or cancelled" --> e3["one stderr line, exit 3"]
    status -- "anything else" --> clamp
```

Each iteration holds one bounded `get_response` call: the envelope decides
whether the command prints an answer and exits `0`, reports an unanswerable card
and exits `3`, or holds another window, while a transport failure counts against
the retry budget or ends the command with exit `1`.

The loop's decisions in words:

- **`answered` / `acknowledged`** — the envelope is printed to stdout as one line
  of compact JSON (`json.dumps(envelope)`), nothing goes to stderr, exit `0`. The
  runner reads the answer nested at `response.choice`.
- **`expired` / `cancelled`** — `paraphe: the card is <status>` on stderr, nothing
  on stdout, exit `3`. The card can no longer be answered, so holding longer
  would be a lie.
- **anything else** — including `pending` and any status the client does not know
  — means the card is still open, so the loop holds *another* bounded window.
  There is no terminal exit for a pending card: the command's lifetime is the
  card's lifetime, and the per-call window is what keeps every single wire
  request inside the server's own bound.

Retries are about transport, not about the card. A `CliError` increments
`failures`, sleeps 3 seconds and retries the same window; any successful call
resets `failures` to `0`, so the counter is *consecutive* failures rather than a
run total. After the twentieth consecutive failure the command prints the error
and exits `1`. A `ToolError` is never retried. Two consequences follow:

- an outage shorter than roughly a minute of backoff (19 sleeps of 3 s, longer
  if a connection hangs until its socket timeout) is ridden out, and a card held
  for hours never exhausts the budget as long as the service keeps answering;
- an outage longer than that ends the waiter with exit `1` **even though the card
  is still answerable** — which is exactly the case the durable read covers at
  the next boundary.

Exit `4` is recognised by content, not by status or code: if the tool error text
contains `unknown request_id`, the command exits `4`; every other refusal is
`1`. Either way the tool error text is printed first as one `paraphe: <text>`
line on stderr, so exit `4` is told from exit `1` by the code alone. That message
is produced when the request id is not in the store, and it
arrives as a JSON-RPC result with `isError: true` under HTTP `200`. `ask` never
produces `3` or `4`, since it does not hold a card's lifetime.

## Exit codes

| Code | Meaning | Triggered by |
|---|---|---|
| `0` | answered, or acknowledged | `wait`: envelope status `answered` / `acknowledged`, envelope printed to stdout. `ask`: the card was created and its request id printed. Also `--help` / `-h` / no arguments |
| `1` | error, or unreachable service | no create bearer; unknown option, missing option value, wrong positional count, unknown command; a non-2xx HTTP reply; a connection failure; an invalid or unparseable reply; a tool error other than an unknown request id |
| `3` | the card is expired or cancelled | `wait` only: envelope status `expired` / `cancelled`, one stderr line, nothing on stdout |
| `4` | unknown request id | `wait` only: the tool error mentions `unknown request_id`, one stderr line, nothing on stdout |

A wrong bearer is an HTTP `401`, which `_call` raises as a `CliError`. Because
`wait` retries `CliError`, a misconfigured credential in `wait` is retried on the
same 20-attempt budget before exiting `1`, while `ask` reports `the service
refused the call (HTTP 401)` immediately — the same classification, two different
observable costs.

## One loop, end to end

```bash
export PARAPHE_MCP_CREATE_BEARER=...   # or mcp_create_bearer in paraphe.toml

REQUEST_ID=$(paraphe ask "Ship the cut?" \
  --context "release train 2026-09-11" \
  --choice Ship --choice Hold \
  --agent-name release-bot \
  --external-id release-2026-09-11)

paraphe wait "$REQUEST_ID" > /tmp/answer.json &
```

The tap ends the wait; exit `0` means `/tmp/answer.json` holds the answered
envelope, and the runner reads `response.choice` from it. Anything else — a dead
waiter, a missed window, a runner that could not background the command at all —
falls back to the same durable read (`get_response`, `list_unprocessed`), which is
why the lifecycle is documented once in
[ask, answer, resume](/openwiki/workflows/ask-answer-resume.md) rather than
re-told here.

Two operational details matter when the command is backgrounded:

- the bearer must be in the environment **the background process inherits** (or
  in `PARAPHE_CONFIG_PATH` / `./paraphe.toml` as seen from its working
  directory); otherwise the waiter exits `1` immediately with the "no create
  bearer" line;
- the waiter's only output is one line of JSON on stdout when it succeeds, so a
  runner can treat the process exit and that line as the whole result.

The MCP clients that do not run commands can still discover the command: the
served `how_to_use` text names `paraphe wait <request_id>` and its exit codes
beside the waited call, which is how the per-runtime idioms in
`skills/paraphe-return-path/SKILL.md` are found rather than guessed.

## Tests and extension seams

`TestWaitCommand` in `tests/inbox/test_wait_engine.py` is the focused suite for
this page: it drives the commands in-process against a real server on a loopback
port and pins that `ask` prints the request id of the card it created (question,
context and choices included), that `wait` exits `0` printing the answered
envelope with empty stderr, exits `3` for an expired card with empty stdout,
exits `4` for an unknown request id, loops over more than one window until a
later tap (`window=0.1` against a tap 0.15 s in), uses `mcp_create_bearer` from
the config file when the environment is unset, and refuses with `1` and
`no create bearer` when neither source exists.

`tests/inbox/test_setup.py` drives the top-level entry point that the client path
is dispatched ahead of: `test_console_command_prints_usage_when_nothing_is_configured`
runs `_entry.main([])` with an empty environment and expects the usage text and
exit `0`, and
`test_console_command_fails_closed_on_incomplete_configuration` sets only
`PARAPHE_OWNER_TELEGRAM_ID` and expects one `paraphe:` line on stderr and
exit `2`.

The commands are importable as functions, which is the extension seam:
`cli.main(argv)` is the dispatch entry, `ask(...)` and `wait_for_answer(...)`
accept injected `out` / `err` streams, and `wait_for_answer` accepts a `window`.
That `window` argument is how the suite observes the multi-window loop in a test
rather than in 60-second real time; it is not reachable from the command line, so
a caller cannot shorten or lengthen the shipped window.
