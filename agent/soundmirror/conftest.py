"""Test config: 让 `python -m pytest` 在仓库根目录跑,可以通过 `soundmirror.orchestrator` 等 import"""
import sys
import os
import pathlib

ROOT = pathlib.Path(__file__).parent.resolve()
sys.path.insert(0, str(ROOT))
