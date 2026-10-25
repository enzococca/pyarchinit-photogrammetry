# Errors and updates

What to do when something goes wrong, where the program writes what happened, and how it updates itself.

## Long steps

Long steps run in the background with a progress window and *Cancel*. The window shows:

* the bar, the current message, the elapsed time and an estimate of the time left (from the rate of the last minutes,
  so a step that slows down sees it grow rather than count down);
* under *Details*, a console with every message of the step and its time - the photos whose key points are extracted,
  the blocks of pairs matched, the photos placed in the block one by one with the points they see, the adjustments of
  the whole block, the depth maps fused - read from COLMAP's own log while it runs, as Metashape's console does. The
  console stays open or closed as you left it.

## When a step fails

![The error window](../img/gui/dialog-error.png)

An error window shows:

* a one-line summary;
* a hint for the known problems (missing SpatiaLite or PyTorch, a crashed reconstruction, a stale Anaconda
  install...);
* the traceback under *Details*;
* the buttons *Copy* (summary and traceback, ready for a bug report) and *Open log folder*.

The same text goes to the [Log pane](panes.md#log) and to the log file `ppg-gui.log` (rotated at 2 MB) in the log folder
of the system; crashes of the native code are written to `crash.log` in the same folder by Python's faulthandler.
*Help > Open the log folder* opens it.

## Environment diagnosis

*Help > Environment diagnosis...* (or `ppg-gui --doctor`, `ppg doctor`) lists Python and every library with its
version, the known pitfalls - another `ppg-gui` earlier on `PATH`, the Anaconda base environment, Open3D 0.20.0, an old
pyvistaqt, a missing SpatiaLite - and what to do about them. *Copy* puts the report on the clipboard, to attach to a bug
report together with the log.

## Updates

A few seconds after start-up the application asks GitHub for the latest release, at most once a day: one HTTPS request to
the GitHub API carrying the version in its User-Agent and nothing else. *Help > Check for updates at start-up* switches it
off, and so does `PPG_NO_UPDATE_CHECK=1`.

When a newer version exists a window shows its notes and offers:

| Button | What it does |
| --- | --- |
| **Download and install** (macOS application) | the dmg is downloaded to the Downloads folder, the new application is copied out of it and, when the program closes (at once with *Restart now*, after the usual request to save the project), it replaces the old one in its folder and opens. The old copy is kept until the new one is in place. If the folder cannot be written by the user, the disk image opens for a manual drag to *Applications* |
| **Download** (Windows application) | the zip goes to the Downloads folder, to extract over the program folder after closing it |
| **Copy the command** (Python installation) | `pip install --upgrade pyarchinit-photogrammetry` (or `git pull` in a source checkout) |
| *Page of the version*, *Skip this version*, *Later* | a skipped version is not proposed again at start-up, but *Help > Check for updates...* still finds it |

From the command line `ppg update` tells whether a newer version exists and `ppg update --download` fetches the
installer of the system.

## Robustness notes

* Photos kept "online only" by Dropbox, iCloud or OneDrive are empty placeholder files: the dense cloud step lists them
  before it starts and offers to go on without them (`build_depth_maps(skip_unreadable=True)`).
* Message boxes are Qt's own (non-native): the native `NSAlert` of macOS 26/27 aborts the process with Qt 6.
* Poisson reconstruction runs in a child process (`engine/isolate.py`): a crash of the native code becomes an error in
  the log, the step retries at a shallower depth and finally builds a 2.5D height field.
* Open3D is imported before rasterio and SciPy (`_env.load_open3d_first`), and the PROJ data exported by Anaconda or
  QGIS is replaced by rasterio's own, also for worker threads.
