# Branch rulesets — drafts

`main.json` and `dev.json` define the two branch rulesets this repository
arms at the flip. They are drafts: branch rulesets are refused while the
repository is private on a free plan, so nothing here is active until the
runbook applies it.

## Arm them

```bash
gh api --method POST repos/leonardsellem/paraphe/rulesets --input docs/launch/rulesets/main.json
gh api --method POST repos/leonardsellem/paraphe/rulesets --input docs/launch/rulesets/dev.json
```

Read them back:

```bash
gh api repos/leonardsellem/paraphe/rulesets --jq '.[] | {id, name, enforcement}'
gh api repos/leonardsellem/paraphe/rulesets/<id> --jq '.rules[].type'
```

## Set the term-list secret first

The `private-terms` and `messages` jobs cannot run without the secret, and
they fail closed without it — so set it before the first run on the new
repository:

```bash
gh secret set PRIVATE_TERMS --repo leonardsellem/paraphe < "$HOME/.config/paraphe/private-terms.txt"
```

## Merge settings at the flip

Rulesets cannot express the merge method, so the repository settings carry it:
squash-only, and the head branch is deleted after a merge.

```bash
gh api --method PATCH repos/leonardsellem/paraphe \
  -f allow_merge_commit=false -f allow_rebase_merge=false \
  -f allow_squash_merge=true -f delete_branch_on_merge=true
```

Read them back:

```bash
gh api repos/leonardsellem/paraphe \
  --jq '{allow_merge_commit, allow_rebase_merge, allow_squash_merge, delete_branch_on_merge}'
```

The automatic deletion covers ordinary change branches. A `dev -> main`
promotion has `dev` itself as its head, and the `dev` ruleset refuses its
deletion — which is what keeps `dev` alive through a promotion.

## What the rulesets require

| Rule | `main` | `dev` |
|---|---|---|
| Pull request before merging (no direct push) | yes | yes |
| Block force-push (`non_fast_forward`) | yes | yes |
| Block deletion | yes | yes |
| Bypass actors | none | none |
| Required approving reviews | 0 | 0 |
| Required checks | 11 | 10 |

Required checks, by their exact check names:

| Check name | Job in `.github/workflows/ci.yml` |
|---|---|
| `suite (python 3.11 on ubuntu-latest)` … `suite (python 3.13 on macos-latest)` (six) | `suite` (matrix) |
| `the distribution installs and the command runs` | `distribution` |
| `the container builds and reports ready` | `container` |
| `the tracked tree carries no private term` | `private-terms` |
| `the pushed commit messages carry no private term` | `messages` |
| `main only accepts pull requests from dev` | `main-source` (`main` only) |

The check names are the job `name:` strings verbatim. A rename on either
side silently unhooks a required check: change both in the same commit.

## Choices, and why

- **No required approvals.** Self-approval is impossible on GitHub, so a
  required review would make the owner's own promotions unmergeable. The
  gate is the required checks plus the source check.
- **No bypass actors.** Nobody bypasses the ruleset, including the owner.
  "No direct push" is the `pull_request` rule: it refuses pushes to the
  branch and accepts changes only through a pull request.
- **Not strict on up-to-date branches.** Checks must be green on the pull
  request's head — which is the `dev` tip in a promotion. Strict mode would
  make every promotion leave `dev` behind `main` with no direct push to fix
  it; `strict_required_status_checks_policy` is one field away if that
  changes.
- **`main` only from `dev`.** GitHub cannot restrict a pull request's
  source branch, so the `main-source` check enforces it and the `main`
  ruleset requires that check.
- **Fork pull requests.** GitHub does not hand repository secrets to fork
  pull requests, so `private-terms` and `messages` fail closed there and a
  fork contribution needs a maintainer to re-run it from the repository:
  push the pull request's head into the repository so the push workflow can
  read the secret, delete the fork's own run (its failed checks keep gating
  the pull request even once the repository-side run is green, and approving
  the fork run cannot help), then merge and delete the helper branch. If
  `dev` has moved past the branch's base, sync the pull request branch onto
  `dev` first and use the synced head.

  ```bash
  git fetch origin refs/pull/<number>/head:refs/heads/pr-<number>-head
  git push origin pr-<number>-head
  # wait for the push run: all ten checks green
  gh api -X DELETE repos/leonardsellem/paraphe/actions/runs/<fork-run-id>
  # merge the pull request, then: git push origin --delete pr-<number>-head
  ```
