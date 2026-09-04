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

CI runs the same suite, plus the HACS and hassfest validators. hassfest can
be run locally too; see `CLAUDE.md`.

## What a pull request needs

- **A test for every change in behaviour, and the test must fail without
  the change.** Revert the fix, watch the test fail, restore it. A test that
  passes either way proves nothing, and that is what a reviewer will check.
- **README changes for anything a user can see:** a new option, a changed
  default, a different result on a chart.
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
