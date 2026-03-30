"""Tests for AMPA package."""

import os
import sys
import pytest

# Ensure src/ is on sys.path so tests can import the package without an
# editable/installed package. This is a small compatibility shim for CI/local
# runs in this repo where we use a src/ layout.
ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), os.pardir))
SRC = os.path.join(ROOT, "src")
if SRC not in sys.path:
    sys.path.insert(0, SRC)

from ampa import VERSION, PACKAGE_NAME


def test_version():
    """Test that version is defined."""
    assert VERSION == "0.1.0"


def test_package_name():
    """Test that package name is correct."""
    assert PACKAGE_NAME == "ampa"
