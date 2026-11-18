# Shared Dani Agent repository

The GitHub remote is the private repository `https://github.com/redline1357/dani-agent`.
Codex and Claude work on the same application source. Read `README.md` and `QUICK_START.md` for the supported product and startup limits.

## Git coordination

- Inspect `git status` and the current branch before editing. Preserve the other agent's uncommitted work.
- Coordinate one writer at a time in a shared working directory. Do not switch its branch, replace its index, reset files or stash another agent's changes while that agent is working.
- For parallel work, use separate worktrees/branches based on the clean `main` history. Use `codex/` or `claude/` branch prefixes as appropriate.
- The initial published `main` has a new, clean history. Older local branches and linked worktrees may retain private investigation history. Never merge or push those legacy histories to GitHub.
- Push only the intended clean branch with an explicit refspec. Never use `--all`, `--mirror`, `--tags`, `--follow-tags` or a force push as a shortcut. Default pushes from the original installation are limited to `main`.
- `.gitignore` excludes local data, generated products, run evidence, release copies, provider configuration and private owner notes. Do not force-add these paths. Review the staged file list and diff before committing.

## Development and validation

- Preserve MXC sandboxing, Windows Job containment, protected-path checks and the restricted Claude reviewer profile.
- Use existing provider subscription sign-ins. Do not expose credentials or introduce API billing/fallback.
- Do not start model sessions, install software, or deploy products without the applicable explicit owner approval.
- Run the checks relevant to your change. Distinguish existing source-matched evidence from tests actually rerun; do not claim customer readiness from offline tests alone.
- Keep user-facing documentation in English. The owner chose the Apache License 2.0 (2026-11-18); keep `LICENSE` and `NOTICE` unchanged unless the owner decides otherwise.
- Users start Dani Agent with `Start Dani Agent.cmd` (or `Start Dani Agent (no AI).cmd`); `QUICK_START.md` is their step-by-step guide. `Start-Dani-Agent.cmd` is the older desktop window: it stays in the repository because the source checkpoint names it, but the user download leaves it out.
- The public website lives in `website/` (static HTML/CSS, no build step, no tracking) and is published by the owner at `https://buildhub.dk/dani-agent/` (Simply.com hosting, uploaded by hand; `.htaccess` sets its security headers). Keep its claims in line with `README.md`, and keep its paths relative so it works inside that folder.
