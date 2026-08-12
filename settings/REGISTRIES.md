# Model knowledge registries

`context_overrides.json` and `modality_triggers.json` are tracked, shipped
version-1 schemas.  Their `version` field identifies format only, not which
knowledge is newer.

Optional ignored user layers are `context_overrides.user.json` and
`modality_triggers.user.json`.  They are never created, repaired, or rewritten
by HamChat.  Context user layers add/replace the three shipped mappings and use
`remove_by_name`, `remove_by_regex`, and `remove_default_by_family` tombstones.
Modality user layers use `add`/`remove` trigger-list mappings,
`model_overrides`, and `remove_model_overrides`; `defaults_to` stays
release-owned.  A malformed user layer is ignored in full while the valid
shipped layer remains active.

Legacy edits to a tracked shipped file require a read-only three-way plan:
baseline (the installed commit), local working-tree file, target shipped file,
and any existing user layer.  Additions, replacements, and tombstones can be
projected; conflicting target/user intent, regex/list reordering, and modality
`defaults_to` changes cannot.  A `READY` plan is still non-executable: a later
transaction must preserve original tracked and user bytes before applying it.
