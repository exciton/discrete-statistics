# Home Assistant 2026.8+ requires Python >= 3.14.2, which is newer than the
# system Python on most hosts. This image exists so the test suite runs on a
# supported interpreter without touching the host. CI installs the same
# requirements onto a runner instead; keep the Python minor here and in
# .github/workflows/validate.yml the same.
FROM mcr.microsoft.com/devcontainers/python:3.14-bookworm

COPY requirements-test.txt /tmp/requirements-test.txt
# ruff is not a test requirement; it is here for linting in the container
# and for hassfest, which needs it. Pinned to the version
# .pre-commit-config.yaml and validate.yml use, so all three agree.
RUN pip install --no-cache-dir -r /tmp/requirements-test.txt ruff==0.16.6

# The repository is bind-mounted, so anything pytest or ruff writes into
# it - caches, bytecode - is owned by whoever runs here. The image's
# unprivileged user has uid 1000, the usual first account on the host.
USER vscode
