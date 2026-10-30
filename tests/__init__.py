"""Test suite. Run with:  python -m pytest   (or  python -m unittest discover -s tests -t .)"""
import os
import tempfile

# Keep runtime state written during tests out of the real per-user data directory.
os.environ["AFI_DATA_DIR"] = tempfile.mkdtemp(prefix="afi-test-")
