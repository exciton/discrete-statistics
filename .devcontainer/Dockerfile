# Home Assistant 2026.8+ requires Python >= 3.14.2, which is newer than the
# system Python on most hosts. This image exists so the test suite runs on a
# supported interpreter without touching the host.
FROM mcr.microsoft.com/devcontainers/python:3.14-bookworm

COPY requirements-test.txt /tmp/requirements-test.txt
RUN pip install --no-cache-dir -r /tmp/requirements-test.txt
