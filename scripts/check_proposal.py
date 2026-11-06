"""Non-interactive risk check of a proposal (used by the Risk Manager agent).

  python3 scripts/check_proposal.py research/2026-10-09
Prints violations, automatic changes, warnings, final weights and order preview.
"""
import _boot  # noqa: F401
import sys
from pathlib import Path

from approve import review

if __name__ == "__main__":
    review(Path(sys.argv[1]).resolve(), interactive=False)
