# Flip-day checklist

Status: staged, **nothing here has run**. D0 = flip day, owner-timed.
Prepared 2026-09-14 for the launch run (plan R19/R20). Every public step below
is gated on the owner's go-public approval card; nothing fires before it.

How to read it:

- **Owner** — `owner` = the repository owner (the approval, the PyPI linking,
  the posts); `flip` = the flip lane that executes the sequence.
- **Order** — strict sequence. A row starts only when the previous row's
  verification passed. Stop and report if any verification fails.
- **Verification** — the read-back that proves the row happened.

## D0 — the flip, in order

| Order | Step | Owner | Verification |
|---:|---|---|---|
| 1 | **Visibility.** Repository flipped to public. The go-public card fires this and nothing before it; the flip lane revalidates first. | flip | `gh repo view --json visibility -q .visibility` reads back `PUBLIC`. |
| 2 | **Protections armed** in the same session, immediately: rulesets on `main` and `dev` from the gates lane's definitions — pull request required, required checks, no direct push, no force-push, no deletion; squash-only feature merges; merge-commit promotions; head auto-deletion off. | flip | Ruleset read-back on both branches shows every rule; a direct push to `main` is refused (plan AE3). |
| 3 | **Secret and reporting.** `PRIVATE_TERMS` secret set; private vulnerability reporting enabled. | flip | `gh secret list` shows `PRIVATE_TERMS`; the repository's security settings read back with private vulnerability reporting on. |
| 4 | **Metadata applied** exactly as drafted in `docs/launch/metadata.md`: description and topics; social preview if the design pass has landed, otherwise recorded as a deferred follow-up; homepage stays empty. | flip | Repository read-back matches the drafts (description, topics); homepage empty; social preview visible once applied (plan AE6). |
| 5 | **PyPI pending publisher linked (owner, about 2 minutes).** Exact steps: `docs/launch/pypi-runbook.md`. Surfaced to the owner ahead of the flip; it can be done any time before row 6. | owner | PyPI shows the pending publisher for `paraphe` bound to the repository and workflow `publish.yml`, environment left empty (D3). |
| 6 | **Release cut.** Tag `v0.1.0` on `main` plus a GitHub Release whose body is the README quick start. | flip | `git ls-remote --tags origin` shows `v0.1.0`; the release body reads back (plan R8). |
| 7 | **PyPI published.** The release triggers `publish.yml` (trusted publishing, no GitHub environment declared). | flip | PyPI serves `paraphe` 0.1.0; a clean venv `pip install paraphe` completes the console loop (plan AE5). |
| 8 | **Funnel opened.** Labels created, seed issues opened, Discussions enabled with its categories and the welcome post — texts from `docs/launch/labels.md`, `docs/launch/seeds/`, `docs/launch/discussions.md`. | flip | Labels read back; seed issues open; Discussions on with the welcome post (plan AE6). |
| 9 | **Post — primary: Show HN**, the full argument from `docs/launch/post.md`. | owner | The post is reachable at its live URL (read-back); that URL is recorded with the launch run. |
| 10 | **Post — r/selfhosted**, links the primary. | owner | The post is reachable at its live URL (read-back); that URL is recorded with the launch run. |
| 11 | **Post — r/LocalLLaMA**, links the primary. | owner | The post is reachable at its live URL (read-back); that URL is recorded with the launch run. |

## After the posts — owner-timed

| Order | Step | Owner | Verification |
|---:|---|---|---|
| 12 | **Directory submission — `awesome-mcp-servers` entry.** Rules and the drafted line: `docs/launch/submissions.md`, target 1. | owner | The entry is live in the list (or the PR is open); recorded with the launch run. |
| 13 | **Directory submission — MCP Registry `server.json`.** Target 2; only after row 7 (the package must exist on PyPI). | owner | The server resolves in the registry search. |
| 14 | **Directory submission — mcpservers.org form.** Target 3. | owner | The listing is live at mcpservers.org. |
| 15 | **Directory submission — `awesome-selfhosted` `software/paraphe.yml`.** Target 4; no earlier than four months after the `v0.1.0` release (the list's release-age gate, quoted in `docs/launch/submissions.md`). | owner | The entry is live in the list. |

## Notes

- The posts go out when the owner chooses. Staging them is this kit's job;
  posting is never automated (run decision D5).
- **Tracking:** the campaign target is 1,000 stars in 90 days from D0. This
  kit — the posts and the submission shortlist — feeds it; the campaign stays
  owner-run (plan SC3).
