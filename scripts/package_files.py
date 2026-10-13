"""Compatibility import for the shared program-file replacement policy."""
import sys
from voice_workbench import program_files
sys.modules[__name__] = program_files
