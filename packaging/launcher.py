"""Entry point of the packaged desktop application (PyInstaller)."""
import multiprocessing
import sys

if __name__ == "__main__":
    multiprocessing.freeze_support()
    import ppg_launcher  # splash at once; worker processes (--ppg-worker) and --doctor go straight on

    sys.exit(ppg_launcher.main())
