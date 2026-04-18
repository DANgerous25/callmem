# WO-24 — Update .gitignore

## Summary

The `.gitignore` has specific entries for `vault.key` and `vault.salt` but lacks broader wildcard patterns and is missing entries for backup files created by the settings route.

## Files to Modify

- `.gitignore`

## Changes Required

1. Add `*.key` and `*.pem` wildcard patterns (broader protection for any key/cert files)
2. Add `*.salt` wildcard pattern
3. Add `config.toml.bak` (settings route creates backup files)
4. Add `*.bak` wildcard pattern
5. Verify `uv.lock` exclusion is intentional (currently ignored — unusual but may be by design)

## Acceptance Criteria

- [ ] `*.key`, `*.pem`, `*.salt` patterns present
- [ ] `*.bak` pattern present (covers `config.toml.bak`)
- [ ] Existing specific entries (`vault.key`, `vault.salt`) retained for clarity
- [ ] No tracked files become newly ignored (verify with `git status`)
