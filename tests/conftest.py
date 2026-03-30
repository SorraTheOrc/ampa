# Ensure tests can import the package from src/ when running without an editable install
# This keeps tests working in CI and local runs without requiring a pip install.
import os
import sys

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), os.pardir))
SRC = os.path.join(ROOT, "src")

if SRC not in sys.path:
    # Insert at front so local package is preferred over any installed packages
    sys.path.insert(0, SRC)
