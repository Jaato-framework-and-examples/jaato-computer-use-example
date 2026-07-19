"""Put the workspace root on sys.path so `from a11y import ...` resolves when
pytest is invoked from anywhere."""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
