"""Entry point of the packaged app (PyInstaller): the hub with a tray / menu-bar icon."""

import multiprocessing
import sys

from source.desktop.app import main

if __name__ == "__main__":
    multiprocessing.freeze_support()
    sys.exit(main())
