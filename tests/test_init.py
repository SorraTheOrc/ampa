"""Tests for AMPA package."""

import pytest

from ampa import VERSION, PACKAGE_NAME


def test_version():
    """Test that version is defined."""
    assert VERSION == "0.1.0"


def test_package_name():
    """Test that package name is correct."""
    assert PACKAGE_NAME == "ampa"
