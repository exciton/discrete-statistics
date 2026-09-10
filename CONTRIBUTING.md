# Contributing

Bug reports and pull requests are welcome. For anything larger than a fix,
open an issue first so the shape can be agreed before the work is done.

## Running the tests

Tests run in a container, because Home Assistant needs a newer Python than
most hosts carry. `script/test` builds the image on first use and passes its
arguments to pytest:

```bash
script/test tests/                              # whole suite
script/test tests/test_compiler.py -v           # one file
script/test tests/test_compiler.py::test_name   # one test
```

Do not run `pytest` directly: it will fail, or run against a different
Home Assistant version and pass for the wrong reasons.

CI runs the same suite, plus the HACS and hassfest validators and the
card's checks below. hassfest can be run locally too; see `CLAUDE.md`.

## Linting

Python is linted and formatted with ruff. CI refuses anything `ruff check`
or `ruff format --check` would change, so install the pre-commit hook once
and each commit is fixed up before it is made:

```bash
pipx install pre-commit   # a plain pip install is refused by most system Pythons
pre-commit install
```

Without the hook, the container carries the same ruff:

```bash
docker run --rm -v "$PWD:/workspace" -w /workspace ha-discrete-stats-test \
  ruff check --fix custom_components tests
docker run --rm -v "$PWD:/workspace" -w /workspace ha-discrete-stats-test \
  ruff format custom_components tests
```

## Working on the card

The card is TypeScript under `frontend/`, and the built bundle is
committed at
`custom_components/discrete_statistics/frontend/discrete-statistics-card.js`,
so a change to the card is two things: the source and the bundle built
from it. `script/release` refuses to release when the committed bundle
is older than the source.

```bash
cd frontend
npm install          # first time
npm run check        # type-check
npm test             # the pure modules: IDs, series maths, the state list
npm run build        # rebuild the committed bundle
```

The card renders through `<ha-chart-base>`, `ha-sortable` and the
`ui_color` selector, which are internal to the Home Assistant frontend
and have no stability promise; a frontend release can move them, so try
a card change in a real dashboard, not only in the tests. `script/deploy
user@host` copies the integration and the built card onto a running
instance over ssh and bumps the version there so browsers fetch the new
bundle instead of the cached one.

## What a pull request needs

- **A test for every change in behaviour, and the test must fail without
  the change.** Revert the fix, watch the test fail, restore it. A test that
  passes either way proves nothing, and that is what a reviewer will check.
- **README changes for anything a user can see:** a new option, a changed
  default, a different result on a chart, a card option or editor control.
- **The rebuilt bundle, when the card's source changes.** CI type-checks,
  tests and builds the card and fails if the build differs from the
  committed file.
- **CLAUDE.md changes for anything a maintainer must know:** it is the
  architecture document, and its *Invariants* section lists the properties
  that produced wrong data when they were broken. A change that adds one,
  or relies on one, says so there.
- **A commit message that describes the code**, not the process of writing
  it. Pull requests are squash-merged, so the message you write is the one
  that lands.

## Where things live

`CLAUDE.md` describes the pipeline and the reasons behind it; read it before
changing `compiler.py` or `canonicalise.py`. Everything below `compiler` is
pure and testable without a `hass` instance, and should stay that way.

## Releases

Maintainers cut releases from `main` with `script/release X.Y.Z`, which
bumps `manifest.json`, tags `vX.Y.Z` and publishes a GitHub release. HACS
reads the version from the release tag, so releases are what users install.
