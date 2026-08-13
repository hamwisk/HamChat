# Update preservation bundles

`hamchat.update_preservation` creates a recovery bundle only when a later
updater coordinator explicitly invokes it.  It writes beneath the trusted,
selected-data-root-derived preservation root, never beneath the release tree.
Bundles live in `bundles/<transaction-id>`, journals in
`journals/<transaction-id>.json`, and unpublished staging data in `.staging/`.

The typed preservation plan is the inventory authority. Release-owned,
derived, ambiguous, extension, and arbitrary untracked paths are excluded.
Ordinary user-owned and legacy tracked-customization files are streamed as
exact bytes, with a SHA-256 digest, private permissions, staged verification,
atomic publication, and independent post-publication verification.

The selected data root is **not** copied as ordinary files: HamChat uses WAL
SQLite and may use SQLCipher, while CAS may be encrypted. A future application
quiescence/online-snapshot provider must make and verify that coherent DB/CAS
snapshot; without one this transaction reports a controlled blocker.

The canonical version-1 manifest has a digest over all fields except
`manifest_digest` and `verified`. The journal has `PREPARING`,
`BACKUP_PUBLISHED`, `BACKUP_VERIFIED`, and `ABORTED` states. It is atomically
replaced and directory-synced. A verified journal is accepted only after the
published bundle independently verifies; matching verified retries reopen it
without rewriting artifacts.

Legacy `READY` plans remain non-executable. Later execution must add their
declared originals to the plan before capture, and record plan/output digests;
this module neither writes a user registry layer nor restores a shipped file.
`BACKUP_VERIFIED` is evidence for a later coordinator, never permission to
modify sources, Git state, the database, or an installed release.

## Data snapshots

The system-only updater never touches the configured data root. Coherent
database/CAS backup and updater-led data migration are deferred work.
