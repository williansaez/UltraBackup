"""Ponto de entrada de módulo: ``python3 -m ultrabackup``."""

import sys

from .cli import main

if __name__ == "__main__":
    sys.exit(main())
