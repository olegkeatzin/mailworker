#!/usr/bin/env python3
"""Точка входа службы автоматической обработки почтового ящика.

    python main.py --config config.ini --check
    python main.py --config config.ini
"""

from __future__ import annotations

import sys

from processor.cli import main

if __name__ == "__main__":
    sys.exit(main())
