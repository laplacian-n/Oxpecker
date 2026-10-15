"""Prompt assembly. This file exists so `unittest discover` can reach the package at all.

Without an `__init__.py`, discovery raises "Start directory is not importable" and skips the
whole directory, so every test file under it is invisible to CI however it is written —
a blind spot distinct from the script-style one, and one that `importlib.import_module`
cannot detect, because namespace packages import fine. See agent/test_every_test_file_runs.py.
"""
