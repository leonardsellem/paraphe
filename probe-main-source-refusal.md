# Negative test: the main-source guard

This branch exists only to exercise the pipeline guard: a pull request into
`main` whose head is not `dev`. The `main only accepts pull requests from
dev` check must fail; the pull request is closed unmerged.
