"""Entry point of the CI runner. The code is in hub/cli.py and hub/commands/."""
import sys

from hub.cli import main

if __name__ == '__main__':
    sys.exit(main())
