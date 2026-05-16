#!/usr/bin/env python3
"""Compatibility wrapper for the GUKO CLI."""
from pathlib import Path
import runpy

runpy.run_path(str(Path(__file__).with_name('guko.py')), run_name='__main__')
