"""Make `import fh` work when a script is run from anywhere."""
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
