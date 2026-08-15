"""Allow running as a module: python -m stalker_gamma_gui."""

from __future__ import annotations

import sys

from .main import main

if __name__ == "__main__":
    sys.exit(main())
