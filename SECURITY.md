# Security policy

## Supported versions

Only the latest commit on `main` is supported.

## Reporting a vulnerability

Please don't open a public issue. Report it privately on GitHub instead:
[Security → Report a vulnerability](https://github.com/tmoreau-sbp/timesheet/security/advisories/new).

Include what you found, how to reproduce it, and the macOS and Python versions. I aim to reply within a week.

## Scope

timesheet runs only on your Mac and makes no network calls. Relevant issues include:
- reading data it shouldn't (URLs, page titles, prompt text, file contents)
- writing or deleting files outside `~/Library/Application Support/timesheet/`
- changing crontab lines other than its own
- anything that could send data off the Mac
