# Pull request

## What

<!-- What changes, and why. Link the issue if there is one: Closes #123 -->

## Why

<!-- The problem being solved. If this fixes a bug, what was the wrong behaviour? -->

## How

<!-- Anything a reviewer should know to follow the diff: trade-offs taken,
     things deliberately left out, behaviour that is unchanged on purpose. -->

## Checklist

- [ ] `make check` passes (ruff, mypy --strict, pytest)
- [ ] New behaviour has a test; the awkward paths (throttling, shutdown,
      resume, failures) are covered, not just the happy one
- [ ] No test touches the network or sleeps on a real rate limit
- [ ] No new `noqa` without a comment saying why
- [ ] README updated if user-visible behaviour changed
- [ ] No secrets, webhook tokens or bot tokens anywhere in the diff
