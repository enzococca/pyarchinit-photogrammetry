"""Entry point of the packaged desktop application (PyInstaller)."""
import multiprocessing
import sys

if __name__ == "__main__":
    multiprocessing.freeze_support()
    from pyarchinit_photogrammetry.gui import main  # handles --ppg-worker (isolated Poisson) itself

    sys.exit(main())
