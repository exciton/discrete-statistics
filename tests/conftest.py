"""Pytest configuration for discrete_stats tests."""

import pytest


# Configure pytest-asyncio mode to avoid async fixture issues with sync tests
def pytest_configure(config):
    """Configure pytest."""
    # Use auto mode for pytest-asyncio
    config.option.asyncio_mode = "auto"
