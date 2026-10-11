# Contributing

Thanks for helping. timesheet is a single Python file with no dependencies, and it should stay that way.

## Ground rules

- **Python:** standard library only, Python 3.9+ (the `/usr/bin/python3` that ships with macOS). No pip packages.
- **Lean:** a new option needs a strong reason. Tunable numbers belong in `--settings`.
- **Privacy:** nothing may leave the Mac. Read times and paths only: never URLs, page titles, prompt text or file contents.
- **Security issues:** don't open a public issue; see [SECURITY.md](SECURITY.md).

## Setup

```sh
git clone https://github.com/tmoreau-sbp/timesheet.git
cd timesheet
./timesheet.py --help
```

## Before opening a pull request

```sh
python3 -m unittest discover -s tests            # all tests must pass
uvx ruff@0.17.0 check . && uvx ruff@0.17.0 format .   # or: pipx run ruff==0.17.0 ...
```

- **Tests:** add or update tests for any change in behaviour. Tests use a throwaway home folder and fake logs, never real data.
- **Docs:** update [README.md](README.md) for users and [docs/SPEC.md](docs/SPEC.md) for design when behaviour changes.
- **GitHub Actions:** pin each action to a full commit SHA with a version comment, using a release at least 10 days old. The `pinact` CI job checks this; `.pinact.yml` holds the rule.

## Commit messages

Use [Conventional Commits](https://www.conventionalcommits.org). The release notes and `CHANGELOG.md` are written from them, and the `commits` CI job rejects anything else.

```text
feat: add Zoom calls as meetings
fix(cron): keep other crontab lines
docs: explain --settings
feat!: rename the config file      # ! marks a breaking change
```

Types: `feat`, `fix`, `perf`, `refactor`, `docs`, `test`, `build`, `ci`, `chore`, `style`, `revert`.

Optional local check, run before each commit:

```sh
cat > .git/hooks/commit-msg <<'HOOK'
#!/bin/sh
grep -qE '^(feat|fix|perf|refactor|docs|test|build|ci|chore|style|revert)(\([a-z0-9-]+\))?!?: .+' "$1" ||
  { echo "Use a Conventional Commit message, e.g. 'fix: handle an empty log'"; exit 1; }
HOOK
chmod +x .git/hooks/commit-msg
```

## Pull requests

- One topic per pull request; its title is a Conventional Commit too, since pull requests are squash-merged.
- CI must pass: lint, tests on Python 3.9 and 3.14, commit messages, pinact and CodeQL.

## Releasing

Maintainers tag a version on `main`; the release workflow does the rest:

```sh
git tag v1.2.0 && git push origin v1.2.0
```

The workflow then:
- runs the tests
- stamps the version into `timesheet.py` (shown in `--help`)
- signs it with a GitHub build attestation
- publishes a release holding only `timesheet.py`, its signature (`.sigstore.json`) and a SHA-256 checksum, with notes written from the commits
- commits the regenerated `CHANGELOG.md` to `main` (pull afterwards)

To see which version the commits call for (`feat` → minor, `fix` → patch, `!` → major): `git cliff --bumped-version`.

Use [semantic versioning](https://semver.org). Once a version is released, `main` is never rewritten.

## License

By contributing, you agree that your contribution is licensed under [GPL-3.0](LICENSE).
