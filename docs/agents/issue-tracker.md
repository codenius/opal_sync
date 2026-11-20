# Issue tracker: GitHub

Issues and specs for this repo live as GitHub issues. Use the `gh` CLI for all operations.

## Conventions

- **Create an issue**: `gh issue create --title "..." --body "..."`.
- **Read an issue**: `gh issue view <number> --json number,title,body,labels,comments`.
- **List issues**: `gh issue list --state open --json number,title,body,labels,comments`.
- **Comment**: `gh issue comment <number> --body "..."`.
- **Close**: `gh issue close <number> --comment "..."` or comment first, then close.
- **Apply labels**: `gh issue edit <number> --add-label "..."` / `--remove-label "..."`.

Infer the repository from `git remote -v`. File issues only in `codenius/opal_sync`.

**Pull requests are not a triage surface.**
