# Claude collaboration instructions

Read and follow [AGENTS.md](AGENTS.md) for the shared Dani Agent repository and security requirements.

The shared remote is `https://github.com/redline1357/dani-agent` (private). Start new work from its clean `main` history. Preserve Codex's uncommitted changes and coordinate before changing branches in a shared directory.

An older local Claude worktree may still be on a legacy branch. It is preserved for reference, but its history is not approved for publication. Do not merge or push that history; use a new branch/worktree based on clean `main` for publishable changes.

GitHub access is through the user's existing Git authentication. Do not request, print, copy or store provider/GitHub tokens in this repository. Do not start a Claude or Codex model session merely to check the Git connection.

## Current continuation handoff

This Claude worktree now uses `claude/from-main`, based on published `main` commit `3e361bb`.
The previous branch `claude/charming-maxwell-96e98a` is retained locally for reference.
Its one additional dashboard port-exclusivity fix and socket regression have been transferred
onto the clean history. Do not merge the legacy branches suggested by older chat messages.
Historical tracked notes removed from this checkout are preserved in a private local archive;
ignored settings, data and evidence were retained in place. No provider session was started.

The focused port regression passed with Windows Job containment. The full regression and
source-review evidence for the older `main` snapshot do not certify this new change. Run the
relevant validation before starting application/model work, following the existing approval gates.
Keep parallel edits in this branch separate from the primary checkout on `main`.
