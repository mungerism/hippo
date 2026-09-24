## Description
<!-- Provide a brief description of the changes, bug fixes, or new features -->

## Related Issues
<!-- e.g. Fixes #123, Closes #456, or Ref #789 -->

## Type of Change
- [ ] 🚀 New feature (feat)
- [ ] 🐛 Bug fix (fix)
- [ ] 📝 Documentation (docs)
- [ ] ♻️ Code refactoring (refactor)
- [ ] 🧪 Tests (test)
- [ ] 🔧 Build / Tooling (chore)

## Checklist
- [ ] My code adheres to the project coding and architecture guidelines
- [ ] `uv run pytest` passes cleanly with all tests green
- [ ] `uvx ruff check --select E9,F63,F7,F82 hippo_memory tests` reports no syntax errors
- [ ] Relevant documentation under `docs/` is updated if behavior or contracts changed
- [ ] `pnpm --dir docs run build` passes with zero broken links
- [ ] No un-sanitized secrets, private IPs, or sensitive paths are introduced
