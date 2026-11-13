# Contributing

Thanks for looking. This is a small project with a small surface, and the
rules that matter are the ones below.

## Run the suite

```bash
python3 -m unittest discover -s tests -p 'test_*.py'
```

Two things about that command:

- **Pass `-s tests -p 'test_*.py'`.** A bare `python3 -m unittest` discovers
  nothing, runs zero tests and exits 0. If the run reports no count, you have
  run nothing.
- **Read the count.** The suite is the only witness that a change to the
  storage, the credentials or the namespace did not alter behaviour. A green
  run that ran fewer tests than before is a red flag.

Tests live in `tests/inbox/` (the product) and `tests/tools/` (the repository's
own tools); the command above discovers both. Add a test next to the behaviour
you changed; the suite has no fixtures to learn.

## What a mergeable change looks like

- **The suite is green**, and the behaviour you added or fixed is covered by a
  test that fails without your change.
- **A change to the create/answer credential boundary needs a test that proves
  the boundary holds.** Specifically: the create credential is refused on the
  answer path, no claim is recorded and the card is unchanged. This is the
  product, and it is the one place where "it works" is not enough.
- **Nothing outside the standard library at runtime.** Adding a dependency
  needs a reason that survives the question "what does this buy a self-hoster
  who installs once and reads the source?"
- **Small and focused.** One failure mode per change. A rename that also
  refactors is two reviews in one diff.
- **No secrets**, ever: not in code, tests, commits, logs or issues. Credential
  *names* are fine.

## The pipeline

`dev` is the integration branch. Every change lands through a pull request
whose base is `dev`, and `main` moves only by a pull request from `dev` — a
source check refuses anything else. Direct pushes, force-pushes and branch
deletion are refused on both branches, and the required checks are listed in
`docs/launch/rulesets/README.md`.

**Releases** are promotions: `dev` is merged into `main` with a merge commit,
`gh pr merge <N> --merge` (never `--delete-branch`), so `main` keeps `dev`'s
history and the next promotion merges without replaying old conflicts. Feature
pull requests into `dev` are squash-merged — the `dev` ruleset allows only
squash, the `main` ruleset only merge commits. Head auto-deletion is off
(`delete_branch_on_merge=false`): delete a feature branch with
`gh pr merge --squash --delete-branch`, and never pass `--delete-branch` on a
promotion. The earlier claim that `dev` survived a promotion because deletion
was refused was false: the live ruleset carried an owner bypass, and the target
branch's auto-deletion deleted `dev` two seconds after #67 merged. The `dev`
ruleset now refuses deletion and has no bypass actors. The release is tagged
`vX.Y.Z` from `main` with notes from the README's quick start. The version is
authored only in `src/paraphe/integrations/hermes/plugin.yaml`; `pyproject.toml`
reads it at build time, so a release bumps that one line.

**Commit messages are scanned.** Pushed commit messages go through the same
private-term list as the tracked files — public history is as permanent as the
tree. Run the same scan before you push:

```bash
python3 tools/scan_private_terms.py --commits origin/dev..HEAD
```

The `messages` job fails closed, names the commit and the category, and never
prints the matched value.

## Where things are

| Path | What |
|---|---|
| `src/paraphe/inbox/` | the card lifecycle, the store, the MCP surface, the composition root |
| `src/paraphe/adapters/` | destinations (console, Telegram) |
| `docs/tools.md` | the tool surface contract |
| `docs/adapters.md` | the contract a contributor implements |
| `docs/adr/` | why the shape is what it is — read the ones you touch |
| `docs/launch/rulesets/` | the branch rulesets this repository arms, and the required checks |
| `tools/scan_private_terms.py` | the private-term gate, run on every push |

## Issues and pull requests

Issues are the request surface; see `docs/agents/issue-tracker.md` for the
labels and the workflow. Pull requests should say which issue they close and
what they verified.

## Reporting a vulnerability

Not in the tracker. See [`SECURITY.md`](SECURITY.md).
