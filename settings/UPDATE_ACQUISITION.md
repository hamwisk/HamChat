# Verified release acquisition

Automatic system-file installation consumes only a schema-version-2 trusted
`latest.json` manifest.  A candidate release includes a `release_payload`
object with an HTTPS URL bound to the immutable `git_ref`, `zip` format, exact
archive byte size and SHA-256, one required archive root prefix, and an
exhaustive list of managed regular files (`path`, `size`, and SHA-256).

The descriptor is an allowlist, not a request to copy an archive wholesale.
The acquirer streams the archive into a private transaction root, validates its
outer digest, rejects unsafe ZIP metadata, and stages only the declared files.
Safe undeclared regular files and directories may be present in a tagged source
archive; they are structurally inspected but are never extracted or installed.
It independently verifies staged bytes, then creates the existing system
installation journal binding the transaction, staging root, and inventory.

`data/`, `settings/`, registries, `.git`, and transaction metadata are never
managed payload paths, even when safe undeclared copies exist in an archive.
Empty inventory entries do not delete prior files;
the current system-file executor deliberately rejects manifest removals until
it has an explicitly verified removal operation.

The acquirer returns a `VerifiedStagedCandidate` only after journal
preparation.  That result still does not change an installation: only the
journal-authorized executor can do so.  Download, archive, staging, and
journal failures are typed, non-authorizing results and never access the
configured data root.
