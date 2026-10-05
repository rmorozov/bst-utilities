"""Entry point for the bst-source-grep command.

Implementation lives in the tool-local _source_grep package.
"""

import sys

from ._source_grep.application import main

__all__ = ["main"]

if __name__ == "__main__":
    sys.exit(main())
