---
allowed-tools: Bash(git *)
---

Check if the upstream repository (jwadow/kiro-gateway) has new commits and advise whether to merge.

Follow these steps:

1. **Ensure upstream remote exists**
   Run `git remote -v` to check. If there's no `upstream` remote, add it:
   ```
   git remote add upstream https://github.com/jwadow/kiro-gateway.git
   ```

2. **Fetch upstream**
   ```
   git fetch upstream
   ```

3. **Compare with local HEAD**
   Run `git log HEAD..upstream/main --oneline` to list new upstream commits not yet in our branch.

4. **If there are new commits:**
   - Show the count of new commits
   - For each commit, show a brief diff stat: `git show --stat --format="%h %s (%an, %ar)" <sha>`
   - Identify which files/areas are affected (routes, converters, streaming, auth, config, etc.)
   - Check for potential conflicts with local changes: `git diff HEAD...upstream/main --stat`

5. **If there are no new commits:**
   - Report that we're up to date with upstream and stop here.

6. **Analysis and recommendation**
   Based on the upstream changes, provide:
   - A summary of what changed upstream (features, fixes, refactors)
   - Whether any changes conflict with our local customizations
   - A clear recommendation: one of:
     - **Merge** — safe to `git merge upstream/main` directly
     - **Cherry-pick** — only specific commits are worth taking (list which ones)
     - **Skip** — changes are irrelevant or would conflict with our customizations

Do NOT actually merge or modify any branches. This command is read-only analysis only.
