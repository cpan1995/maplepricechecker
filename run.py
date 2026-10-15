"""Launcher that works from any working directory (elevated processes start in System32)."""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from mapleprice.__main__ import main

main()
