# Hermes approval transport

Paraphe ships an optional Hermes plugin, `paraphe-approval-transport`, in
`paraphe.integrations.hermes`. The plugin component is version 0.4.3; this is
independent of the Paraphe distribution version. Installing Paraphe does not
enable the plugin, select a transport, add dependencies, or contact a service.
The ordinary Paraphe CLI and server do not import Hermes or activate the plugin.

## Install through Hermes' native surfaces

See the [official plugin guide](https://hermes-agent.nousresearch.com/docs/developer-guide/plugins/).
Hermes must provide `register_approval_transport`, `call_mcp`,
`ctx.state.data_dir`, and `utils.atomic_json_write` with `mode` and `fsync_dir`.
The `register_hook` seam is optional; without it, approval still works but the
smart-admission receipts described below are not registered.

For an environment supplied by its installation owner, install Paraphe into
that environment using its package manager. The wheel declares:

```toml
[project.entry-points."hermes_agent.plugins"]
paraphe-approval-transport = "paraphe.integrations.hermes"
```

Hermes discovers this metadata without importing the plugin. Loading the entry
point imports only the standard library; the atomic helper is imported lazily
when diagnostics are persisted. Enable the discovered plugin explicitly:

```console
hermes plugins list
hermes plugins enable paraphe-approval-transport
```

Do not pip-inject Paraphe into a PM-selected Hermes generation. Managed installs
use the directory plugin shipped at `src/paraphe/integrations/hermes`, containing
`__init__.py` and `plugin.yaml`. The current Hermes CLI accepts a repository
subdirectory in the identifier (not a `--subdir` flag):

```console
hermes plugins install leonardsellem/paraphe/src/paraphe/integrations/hermes --no-enable
hermes plugins enable paraphe-approval-transport
```

Use a revision containing the integration. To pin one, append
`--ref <full-40-character-commit-sha>` to the install command. A local directory
payload can be checked without installation:

```console
hermes plugins doctor ./src/paraphe/integrations/hermes --ci
```

The CLI's install argument is a Git/catalog identifier, not an arbitrary local
directory. This repository's root is the Paraphe distribution, not a directory
plugin; use the subdirectory above. Check your installed CLI's help before
installing on an older Hermes release. Restart Hermes after a managed environment
change. No live plugin replacement or migration is performed by importing Paraphe.

### Existing standalone copies

In the verified Hermes loader, directory plugins load before entry points. A
same-name standalone directory copy can shadow the pip entry point, even when
Paraphe has been upgraded. `hermes plugins list` and `hermes plugins doctor`
should identify the source actually loaded; the registration diagnostic also
fingerprints its module bytes. Directory manifests report component version
0.4.3; entry-point discovery reports the distribution version from package
metadata, not that component version.

Choose one source deliberately. Review and back up an existing standalone copy
before using Hermes' native removal/replacement workflow; preserve its settings
and durable data, and verify the new source after restart. Installing a wheel
alone is not proof that a legacy directory plugin was replaced. This package
never removes or edits that copy for you.

## Configure the return path

Configure a Paraphe MCP server named `paraphe` in Hermes, with the agent's create
credential only. Keep the owner's answer credential out of the agent runtime.
Then merge these non-secret settings into the active profile using Hermes'
configuration interface; do not replace unrelated plugin settings:

```yaml
plugins:
  enabled:
    - paraphe-approval-transport
  entries:
    paraphe-approval-transport:
      mcp_allowlist: [paraphe]
security:
  approval:
    transport: paraphe
```

The host gives this plugin a redacted approval request. It creates one card via
`request_approval`, polls `get_response` until the host deadline, and responds
`once` only to explicit `Approve`, or `deny` only to explicit `Deny`. It does not
grant session-wide or permanent approval, and cannot answer a Paraphe card.
Technical faults raise through the host's fail-closed contract without inventing
an owner denial. A timeout attempts `cancel_request` with `resolved_elsewhere`,
then raises even if cancellation fails. Card expiry stays within Paraphe's API
bounds; it does not extend the host's decision deadline. Calls are not retried
on technical failure; pending cards are polled every three seconds.

Cards retain the existing French copy, command, origin and best-effort read-only
Git context. Commands longer than 2000 characters are refused before creation.
Diagnostic redaction is not a claim that the card or the separate optional
smart-admission ledger contains no command text.

## Bounded diagnostic snapshots

Only these two committed files are owned by diagnostic retention, under the
public, profile-scoped `ctx.state.data_dir`:

- `transport-registration-latest.json`
- `transport-attempt-latest.json`

Each is limited to 2048 UTF-8 bytes, including pretty-printed JSON overhead:
4096 committed bytes total when both exist. Writes use the host's native
`utils.atomic_json_write`, mode `0600`, file fsync, atomic replace and directory
fsync. There is no diagnostic history or per-request file. "Latest" means the
last successful atomic replace per kind, not start order or all concurrent
attempts. The process-global `LAST` dictionary is only a legacy debug aid, never
a source for durable correlation.

Each attempt carries its own registration ID, source SHA-256 (when readable),
PID, and validated host request/digest identifiers. Only fixed phase, result,
exception categories and UUID-shaped Paraphe reply IDs are retained. No command,
cwd, raw MCP body or exception message is copied into these snapshots.

Legacy `state.json` is not read, rewritten, pruned or migrated. A quota-full or
malformed legacy store cannot block fresh snapshots. Storage failures warn once
per OS process across threads and import names; forked children have their own
warning budget and reset the inherited warning lock. Every later write is still
attempted, and a diagnostic failure never changes the approval result.

The cap excludes untouched legacy state, unrelated files, logs, filesystem
allocation overhead, and crash-orphan temporary files created by the native
atomic helper. It is not a cap on the entire profile or data directory.

### Optional smart-admission receipts are unchanged

If the host emits `post_approval_response(choice="smart_approve", ...)`, the
plugin appends a best-effort JSONL receipt to `ESCALATION_BAND_RECEIPTS`, or
`~/.hermes/state/escalation-band.receipts.jsonl` by default. It records the first
command line (at most 200 characters), pattern and session keys. The parent
directory must already exist; failure only logs a sanitized exception category.
This separate ledger is not rotated or capped by the diagnostic fix, and its
default does not follow a custom profile home. Set the environment override to
an appropriate private, externally retained destination if using this hook.
Ordinary Approve/Deny do not append these receipts.

## Offline verification

The root suite discovers the portable integration tests without Hermes:

```console
python3 -m unittest discover -s tests -p 'test_*.py'
```

These tests label their fake host helpers explicitly. Distribution CI checks
the installed entry point in a clean environment without Hermes. Plugin Doctor
checks native directory loading without creating cards; a separate scratch
exercise against a real host state object and atomic helper can verify the
installed wheel without contacting Paraphe or altering a live profile.
