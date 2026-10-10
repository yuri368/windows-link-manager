"""Windowed executable entry point; no console or development environment needed."""
import sys

from linkmanager.main import main


if __name__ == "__main__":
    sys.exit(main())
