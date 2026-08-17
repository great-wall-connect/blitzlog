name: Pull request

about: Open a PR against Blitzlog
title: ""
labels: []
assignees: []

---

## Summary

One or two sentences. Reference the issue this closes with `Closes #N`.

## Type of change

- [ ] Bug fix (non-breaking change that fixes an issue)
- [ ] New feature (non-breaking change that adds functionality)
- [ ] Breaking change (fix or feature that changes existing behaviour)
- [ ] Documentation / infra only

## Checklist

- [ ] I have read [AGENTS.md](../AGENTS.md) and follow its conventions.
- [ ] Branch name is `feat/issue-{N}-{slug}` or `fix/issue-{N}-{slug}`.
- [ ] Commit messages follow [Conventional Commits](https://www.conventionalcommits.org/).
- [ ] `mise run lint` passes locally.
- [ ] `mise run test` passes locally. New tests added where applicable.
- [ ] I have not committed secrets, internal identifiers, or personal information.

## Notes for reviewer

Anything the reviewer should know — context, design trade-offs, follow-ups.