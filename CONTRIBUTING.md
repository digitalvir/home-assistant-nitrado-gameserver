# Contributing

Contributions must preserve administrator authorization, fresh-state checks,
fail-closed mutation gates, credential redaction, and the generic-Nitrado versus
game-profile boundary described in the ADRs.

## Development setup

Use the pinned Python, Home Assistant, Node, npm, and browser versions documented
in [COMPATIBILITY.md](COMPATIBILITY.md). Then run:

```bash
uv venv --python 3.14 .venv-ha
uv pip install --python .venv-ha/bin/python -r requirements_test.txt
npm ci
PYTHONDONTWRITEBYTECODE=1 .venv-ha/bin/python -m unittest discover -s tests -q
PYTHONDONTWRITEBYTECODE=1 .venv-ha/bin/pytest -p no:cacheprovider -q ha_tests
npm run test:frontend
npx playwright install chromium
npm run test:browser
```

Also run Ruff, formatting, Bandit, compileall, both JavaScript syntax checks,
HACS, hassfest, public-repository validation, and exact release-artifact gates.

Do not include credentials, live saves, provider files, player names, account or
service IDs, private addresses, local paths, screenshots of personal data, or
private development-workspace records in commits, tests, issues, or fixtures.

Public pull requests require explicit maintainer authorization. A local branch
or patch does not imply permission to publish one.
