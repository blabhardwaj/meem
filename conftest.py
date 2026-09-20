import sys
from pathlib import Path

# Ensure meem_salvage root is on sys.path for all pytest test suites
root_dir = Path(__file__).resolve().parent
if str(root_dir) not in sys.path:
    sys.path.insert(0, str(root_dir))
