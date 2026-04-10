import sys
from pathlib import Path

# Add agent/ to path so tests can import modules
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
