"""Запуск службы как модуля: python -m processor --help."""

from .cli import main

raise SystemExit(main())
