# Downloads and visits

How many people download pyarchinit-photogrammetry and visit its pages, and from where. A workflow of the
repository collects the numbers every week. Robots, mirrors, continuous integration and the author's own visits
are left out wherever the source allows it; the notes at the end say where it does not.

<!-- ppg-stats en -->

## How the numbers are made

**Downloads of the application.** GitHub counts every download of the files of a release: the dmg for macOS and
the zips for Windows x64 and Windows on ARM, with and without the version in the name. It gives no country, and
only the running total of each file, so the workflow reads it every week. The downloads of a month are those
counted at the readings made in that month; those made before the first reading go to the month of their
release. The count includes the updater of the application, which downloads each new version for the people
who already use it, and a few tests of the author.

**Download clicks.** The [installation page](install.md) counts a click on its download button, or on an entry
of its menu, as an event of GoatCounter: for these clicks the country and the source are known. A download from
the releases page of GitHub, or by the updater, is not a click.

**pip installs.** PyPI publishes every download in a public dataset on Google BigQuery. The raw count holds
everything: continuous integration, mirrors, caches and scanners. The real count keeps the installers people use
(pip, uv, poetry, pdm, pipenv and the browser), and leaves out the downloads that declare a CI environment and
those from a Python older than 3.10, which the program does not run on (they are scanners). The native core,
`pyarchinit-photogrammetry-core`, has a line of its own. Until BigQuery is configured and has read every day since
1 October 2026, the page shows the raw numbers of
[pypistats.org](https://pypistats.org/packages/pyarchinit-photogrammetry), which leave out only the known mirrors.

**Visitors of this site.** [GoatCounter](https://www.goatcounter.com) counts a visitor once per page in a session
of up to 8 hours, and leaves out robots. The author's own browsers are left out too.

**Visitors of the repository.** GitHub keeps the views of the repository for 14 days; the workflow adds them up
day by day. A person who comes back on three days counts three times. The author's own views cannot be told
apart and are included. The clones are not shown: almost all of them are continuous integration.

**Privacy.** Only totals and counts by country, system, browser, version and page are collected and published.
GoatCounter uses no cookies and stores no IP address; nothing here identifies a person.
