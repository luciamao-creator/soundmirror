"""Test config: 添加 soundmirror 包到 sys.path"""
import sys
import pathlib
import pytest

# Add soundmirror package root
AGENT_DIR = pathlib.Path(__file__).parent.resolve()
SOUNDMIRROR_ROOT = AGENT_DIR.parent.resolve()

# Tests are inside soundmirror/tests/,so soundmirror is importable as a package
# Make sure parent is in path so 'soundmirror' package can be found
sys.path.insert(0, str(SOUNDMIRROR_ROOT.parent))
