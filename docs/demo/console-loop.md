# Demo: the whole loop on a clean checkout

Recorded on 2026-09-10 against `docs/repo-grooming`. The steps are the ones in
the README and nothing else: a clone, a virtual environment, `pip install .`,
a configuration file carrying two values the operator made up, and the command.

No Telegram bot, no third-party account, no external service is in the path.
The destination is the console, which is what a first run uses.

An animated terminal recording of these five steps is committed as
[`console-loop.gif`](console-loop.gif) and embedded on the README.

## The configuration the operator wrote

```toml
mcp_create_bearer = "reader-supplied-create-bearer"
owner_answer_token = "reader-supplied-answer-credential"
```

## The run

```
$ .venv/bin/paraphe --config paraphe.toml
Paraphe ready: mcp http://127.0.0.1:8899/mcp answer-path on
```

## The loop

```
1. asked      : 43c1b52f-… version 1 pending True
2. agent credential answering is refused: 401
3. answered   : 200 answered
4. agent resumed (poll): Yes via answer-path
5. closed     : acknowledged processed_at set: True
```

1. The agent called `ask_question` on the MCP surface with the create
   credential. The card went to the store.
2. The same credential was presented to the answer path. It was refused, no
   claim was recorded, and the card was unchanged. This is the product's line,
   observed rather than assumed.
3. The owner answered through the owner-only answer path with their own
   credential.
4. The agent polled `get_response` and found the answer, with
   `responded_via: answer-path`.
5. `mark_processed` closed the card.

## What the owner saw

The example card, composed per the writing contract in
[`../tools.md`](../tools.md): purpose first, origin, then what each answer
authorises:

```
[paraphe] card 43c1b52f-… (version 1) is waiting for you
  kind: question   risk: low   priority: normal
  question: Move the card store before the release?
  context: Pourquoi : the release cannot ship with the store at the old path.
    D'où ça vient : agent Hermes · runtime CLI · repo paraphe · worktree store-move · ticket card-3119
    Action: run `paraphe store relocate` on the deployment host.
    Yes authorises that one move and nothing else. No leaves the store where
    it is and the release waits.
  choices: Yes, No
  recommendation: Yes
  if ignored: the release ships with the store at the old path.
  answer with: curl -s -X POST http://127.0.0.1:8899/answer \
    -H "Authorization: Bearer $PARAPHE_OWNER_ANSWER_TOKEN" \
    --data '{"request_id": "43c1b52f-…", "version": 1, "choice": "<your answer>"}'
```

## The phone variant

The same loop with a Telegram bot as the destination is what the deployed
service runs. There is no recorded asset of it yet: making one needs a phone,
which this repository cannot operate. Until there is one, the tap path is
documented in [`../adapters.md`](../adapters.md) and its tests are in
`tests/inbox/test_telegram_port.py`.
