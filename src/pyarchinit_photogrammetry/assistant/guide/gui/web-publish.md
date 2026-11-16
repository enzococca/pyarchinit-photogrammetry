# Publishing the web site of a survey

The pane **Publish on the web** builds the site of a survey as a folder (see
[Exports and reports](exports.md#web-site-publish-on-the-web)). Its step 4, **Publish**, puts that folder on
**Cloudflare** for free with your own Cloudflare account; the folder can also go to any other static hosting by hand
(at the end of this page).

The program never asks for, reads or keeps a password, a token or a key: it runs **wrangler**, the command line of
Cloudflare, which you install and log in to yourself. It reads only what wrangler answers, and what it shows or keeps of
it is cleaned of anything that looks like a token, an account id or an e-mail address. The project keeps only the
address of the site, the name of the Cloudflare project and of the bucket, and the date.

## What is free (read on Cloudflare's documentation on 14 November 2026)

| Service | Free plan | Source |
|---|---|---|
| **Pages**: files per site | 20,000 (100,000 on paid plans) | [Pages limits](https://developers.cloudflare.com/pages/platform/limits/) |
| **Pages**: size of one file | 25 MiB | same page |
| **Pages**: requests and traffic of a static site | free and unlimited | [Pages pricing](https://developers.cloudflare.com/pages/functions/pricing/) |
| **Pages**: projects per account | 100 | Pages limits |
| **Pages**: range requests | answered with the whole file (`200`), not by parts | [Serving Pages](https://developers.cloudflare.com/pages/configuration/serving-pages/) |
| **R2**: storage | 10 GB-month a month (Standard storage) | [R2 pricing](https://developers.cloudflare.com/r2/pricing/) |
| **R2**: writes (Class A: upload, list, CORS rule) | 1 million a month | same page |
| **R2**: reads (Class B: download, head) | 10 million a month | same page |
| **R2**: traffic out (egress) | free | same page |
| **R2**: one upload with wrangler | up to 315 MB | [Upload objects](https://developers.cloudflare.com/r2/objects/upload-objects/) |
| **R2**: public `r2.dev` address | rate-limited, "for development purposes" only; no cache, no Access | [Public buckets](https://developers.cloudflare.com/r2/buckets/public-buckets/) |

R2 must be turned on once for the account ("R2 subscription", a checkout in the dashboard); the R2 pages do not say
whether a payment method is asked when the use stays within the free tier: expect the dashboard to ask for one.
Workers static assets have the same file limits as Pages (20,000 files, 25 MiB a file); Pages is not deprecated and
`wrangler pages deploy` publishes a folder without any configuration file, so the program uses Pages.

**Where the files go.** With **Automatic** (the default) the whole site goes to Pages when every file is within
25 MiB and there are at most 20,000 files - a normal trench with the default size of 500 MB usually is. Otherwise the
page (`index.html`, `site.json`, `app/`) goes to Pages and the folder `data/` (orthophoto, DEM, model, cloud) to an
**R2 bucket**, which serves files by parts: the viewer reads large rasters a piece at a time. On Pages alone the
viewer takes each raster whole (up to 50 MB a file, the viewer's own limit): it works, it is only slower to open.

## The first time, step by step

1. **A Cloudflare account**: sign up for free at <https://dash.cloudflare.com/sign-up>.
2. **Node.js**: install it from <https://nodejs.org> (the LTS version).
3. **wrangler**: in a terminal (*Terminal* on macOS, *PowerShell* on Windows):

        npm install -g wrangler

   The program never installs it by itself. It looks for `wrangler` on the PATH and in the usual folders of Node
   (Homebrew, `~/.npm-global/bin`, nvm, Volta), then for `npx --no wrangler` (which never downloads anything).
4. **Log in**, once. Since 1.10.1 the simplest way is the program itself: press **Check** in step 4 and, when it says
   wrangler is not logged in, press **Log in to Cloudflare** (below). Or in the same terminal:

        wrangler login

   When wrangler is not installed but `npx` finds it (it sits only in the cache of npx), `wrangler` alone answers
   *command not found*: type `npx wrangler login` instead. The pane and `ppg web publish cloudflare` name the command
   as you can type it, from the one they found. The browser opens on Cloudflare: allow wrangler. The credentials stay
   with wrangler; the program never sees them.
5. In the program: build the site (steps 1-2 of the pane), then in step 4 write the **Pages project** name
   (lowercase letters, digits, dashes; the site will be at `https://<name>.pages.dev`; Cloudflare adds a suffix when
   the name is taken, and the pane shows the address it gives).
6. Press **Check**: the pane finds wrangler, sees whether you are logged in (`wrangler whoami`) and shows the **plan**:
   how many files and MB go to Pages and to R2, each free limit with *within* or *over*, what becomes public, the
   warnings (in amber) and what stops the publication (in red). Nothing is sent.
7. Press **Publish...**: a message says that the site becomes **public** - anyone with the address can open it and
   download its files - and asks for confirmation. A site built with **Real coordinates** asks a second time, in red:
   it tells anyone where the site is. Without both answers nothing is sent.
8. The publication runs in the background with *Cancel*; the commands of wrangler and their answers go to the Log pane.
   At the end the pane and the Workspace (**Web site**) show the address, with **Open in the browser** and **Copy the
   address**; the address is kept with the project. The address is read from wrangler's answers, never guessed: when
   they do not say it, the pane says so, the answers go whole (cleaned) to the Log pane - send them to us - and you
   write the address (from the dashboard, *Workers & Pages*, or `wrangler pages project list`) in the field below.

**Log in to Cloudflare** (since 1.10.1). When the check says wrangler is not logged in, step 4 shows this button: the
program runs your own `wrangler login` (the same command a terminal would run), wrangler opens the browser, you sign
in there, and when wrangler ends the program checks again by itself (the login and the plan). Meanwhile the step says
*complete it in the browser*; **Cancel** stops wrangler, and after 10 minutes without an answer it stops by itself.
Nothing of what wrangler prints - the sign-in address, the account - is kept, written to the Log pane or shown: only
whether it worked. The program never reads wrangler's files.

**A terminal from the program** (since 1.10.1): *Tools > Open a terminal* opens the terminal of your system in the
folder of the project with `ppg`, `npx` and `wrangler` reachable ([Tools](menus.md#tools)).

**Started from the application** (Finder, Start menu) wrangler has no folder of its own to work in: since 1.10.1 the
program gives it a cache folder of yours in the temporary directory (`WRANGLER_CACHE_DIR`, unless you set one), so
commands such as `pages project list` no longer end with *Missing file or directory: /.wrangler/cache*. When a command
fails, the message starts from wrangler's `[ERROR]` line.

**"Not logged in" right after a login that worked**: a folder `~/.wrangler` holding only `cache` or `logs` was
made by running wrangler with the home folder as working folder, and wrangler now takes it for its configuration
and finds no login there. Delete that folder and press **Check** again: the login is kept elsewhere. Since 1.10.1
the pane and the error messages name the folder when they find it (they only check that it exists).

Publishing again sends only what changed: Pages skips the files it already has, and the program remembers the files
sent to R2 (`.ppg-publish.json` in the site folder, never published) so that it sends only new or changed ones and
deletes those no longer in the site. When the site is built again after a publication, step 4 says *to redo*.

## When the data go to R2

The pane asks for a **bucket** name (3-63 lowercase letters, digits, dashes). Use a bucket for this site alone: the
program writes its CORS rule (GET and HEAD with the `Range` header from the page's address), replacing any other. It
creates the bucket when it is not there, uploads each data file with its content type and a cache of one hour, and
turns on the bucket's public **r2.dev** address, which the published copy of `site.json` uses (the folder on your
disk stays as it was built). Everything in that bucket becomes public.

The r2.dev address is meant for trying: for a site that stays online, connect a **custom domain** to the bucket in the
dashboard (*R2 > the bucket > Settings > Custom Domains*; the domain must be on Cloudflare) and write its `https://`
address in **Address of the data**: the program then leaves r2.dev alone.

## Names of people

Since 2.0.0 the site does not publish the attributes in which PyArchInit keeps the name of a person:

- `schedatore`, `direttore_us`, `responsabile_us`, `disegnatore`: who recorded a unit, who directed, who was in
  charge, who drew;
- `responsabile`, `responsabile_scheda`, `compilatore`, `nome_compilatore`: who is in charge of a survey unit or of
  a record, who filled it in;
- `team_members`, the team of a survey unit, and `draa`, the author of a drawing in the records of the materials.

Nor those in which it keeps the account of whoever wrote a record: `last_modified_by`, `created_by`, `updated_by`,
`editing_by`. They are left out of everything the site publishes - units, spot heights, drawings (a layer imported
among them brings its columns), sections and elevations, what the plugins add (their layers, tables and legends).
Nothing changes in the project or in PyArchInit: the names stay there.

An attribute is recognised by its name, whatever the case of its letters and whatever stands between them
(`Disegnatore`, `Direttore US`, `direttore-us`, `nomeCompilatore`), also as a shapefile cuts a long name: to its
first ten characters (`responsabi`, `nomeCompil`) or to eight and a number (`responsa_1`, `lastModi_1`). What it
holds is left out whole; so is a column whose cut name is the one of these fields, even when it held something else
(`last_modified`, a date, cut to `last_modif`). What the program does not do:

- a name that says more is another attribute and is published (`disegnatore2`, `us_disegnatore`), and so is a
  column that is not in the list (`autore`, `rilevatore`), and so is a name a shapefile cut to a word of its own
  (`Direttore US` cut to `Direttore`): take such a column out of the layer before importing it, or leave the
  drawings out of the products of the site (step 1);
- it does not look for names in the texts: a name written elsewhere - in the title or the description of the site,
  in a label, in a note, in the name of a layer or of a drawing - is published as it is written.

The sites built with versions 1.9.0 to 1.10.2 hold those attributes with the others: the panel of a click and the
files under `data/` show, for instance, who drew a unit (1.9.0 built the site, to be put online by hand; since
1.10.0 the program publishes it). To take the names off a site that is already online:

1. Build the site again (step 2) and publish it again (step 4) with 2.0.0 or later: the main address and the data then
   give the new files (the data within the hour of their cache).
2. Delete the earlier deployments. Cloudflare Pages keeps them, each at an address of its own
   (`<hash>.<name>.pages.dev`, the one wrangler prints): publishing again changes what `<name>.pages.dev` shows and
   leaves them where they are, so the copy published before can still be opened at its address.
   `npx wrangler pages deployment list --project-name <name>` lists them and
   `npx wrangler pages deployment delete <id> --project-name <name>` deletes one (commands of Cloudflare, read on its
   documentation on 15 November 2026).

Or use **Remove from the web...** ([Deleting the site](#deleting-the-site)), which deletes the project with all its
deployments, and publish again.

A site put online in another way - its folder copied to a server, a repository behind GitHub Pages - is replaced
there with the folder built again. Where the hosting keeps what was published before, the names stay in it: a Git
repository, for instance, keeps the earlier files of `data/` in its history until the history is rewritten or the
repository is made again.

## Restricting who can open the site: Cloudflare Access

The program publishes a public site. To let only some people open it, set **Cloudflare Access** (Zero Trust) in the
dashboard, after publishing:

1. *Workers & Pages* > your project > *Settings* > **Enable access policy**: this protects the preview addresses.
2. To protect the main `<name>.pages.dev` address as well: *Manage* on the policy just made, then in *Access >
   Applications* open the application, *Configure*, and in *Public hostname* delete the `*` from the *Subdomain*
   field (Cloudflare's own steps: [Known issues of Pages](https://developers.cloudflare.com/pages/platform/known-issues/)).
3. Write who may enter (for instance a list of e-mail addresses, who receive a code by e-mail).

Data in R2 behind the r2.dev address cannot be protected by Access: with a restricted site use a custom domain for the
bucket and protect it with Access too, or keep the whole site on Pages.

## Deleting the site

Since 1.10.2, step 4 has **Remove from the web...**, shown when the site folder records a publication (the file
`.ppg-publish.json` the program writes in it). The program removes only what it published itself from that folder:

- the Cloudflare Pages project, with all its deployments: the address of the site stops working;
- the public r2.dev address of the bucket, only when the program turned it on;
- the files it sent to the R2 bucket, one by one; other files of the bucket are never touched;
- the bucket itself only when you tick **Delete the bucket too**, which is enabled only when the program created
  the bucket, and only after every file it sent is gone (a bucket holding other files is not deleted).

A custom domain you connected to the bucket is never touched, and the site folder on your computer stays. The window
lists what will be removed, says it cannot be undone, and enables its button only once you type the name of the
project. The removal runs like the publication: its steps in the log, **Cancel** stops it. It goes in the order of
the list: the Pages project first, so that the address of the site stops answering before the slower removal of the
data. If it is cancelled or a step fails, press **Remove from the web...** again: it goes on with what is left. Once
everything went, the record in the folder is removed and the project forgets the address.

wrangler looks only in the Cloudflare account it is logged in to. A project or a bucket it does not find there counts
as removed, and the program says so: a window in the application, a `note:` line on the command line, with its name
and the address the publication recorded. If the site or its data still answer at that address, they are in another
account: delete them in the dashboard of that account (*Workers & Pages*, *R2*).

Publishing the folder again after a removal left halfway makes again what was removed: the next removal starts from
the beginning.

Sites published before 1.10.2 did not record whether the program created the bucket or turned on its r2.dev address:
for them both are left as they are (the window says so); turn them off in the dashboard if you wish.

By hand, in the dashboard: *Workers & Pages* > the project > *Settings* > **Delete project**; *R2* > the bucket >
delete its objects, then the bucket, or turn off its public address in *Settings*.

## From the command line

    ppg web login cloudflare                                             # your wrangler login (since 1.10.1)
    ppg web publish cloudflare SITE_FOLDER --project NAME --dry-run      # the plan, nothing run or sent
    ppg web publish cloudflare SITE_FOLDER --project NAME --yes-public
    ppg web publish cloudflare SITE_FOLDER --project NAME --bucket NAME-data --yes-public
    ppg web publish cloudflare SITE_FOLDER --project NAME --yes-public --yes-true-coordinates   # site built with --frame crs
    ppg web unpublish cloudflare SITE_FOLDER [--delete-bucket] [--yes]   # remove it from Cloudflare (since 1.10.2)

`ppg web login cloudflare` does nothing when wrangler is logged in already; otherwise it runs `wrangler login`
(`--timeout` seconds, 600 by default; Ctrl+C stops it) and prints only whether it worked.
`--yes-public` is required; a site built with real coordinates also needs `--yes-true-coordinates`. Other options:
`--data auto|pages|r2`, `--data-url https://...` (a custom domain of the bucket), `--origin https://...` (another
address of the page allowed to read the bucket), `--record PROJECT.ppg` (keeps the address in the project).

The wrangler commands the program runs, in order: `whoami --json`; `pages project list --json` and, for a new project,
`pages project create NAME --production-branch main`; with R2: `r2 bucket info BUCKET` (and `r2 bucket create
BUCKET` when missing), `r2 object put BUCKET/KEY --file FILE --content-type TYPE --cache-control "public,
max-age=3600" --remote` for each new or changed file, `r2 object delete BUCKET/KEY --remote` for each file gone,
`r2 bucket dev-url enable BUCKET -y` and `r2 bucket dev-url get BUCKET` (without a custom domain), `r2 bucket cors set
BUCKET --file RULE.json -y`; last `pages deploy FOLDER --project-name NAME --branch main --commit-dirty=true`.

`ppg web unpublish cloudflare` prints what it will remove and, without `--yes`, asks you to type the name of the
project; `--delete-bucket` deletes the bucket too (only when the program created it); `--record PROJECT.ppg` forgets
the address in the project once everything went. It ends with code 1 when something was not removed: run it again
to go on. The wrangler commands, in order: `pages project delete NAME --yes`, `r2 bucket dev-url disable BUCKET -y`
(when the program turned the address on), `r2 object delete BUCKET/KEY --remote` for each file the program sent,
last `r2 bucket delete BUCKET` (when asked and allowed). A project or a bucket that wrangler does not find in its
account is written as `not found in this Cloudflare account (it counts as removed)`, with a `note:` line naming the
address to check.

## Other hostings

- **Any static hosting** (a university server, Netlify, an S3 bucket...): copy the site folder as it is. The host
  should answer range requests (`Accept-Ranges: bytes`) for the large rasters; without them the viewer takes each file
  whole up to 50 MB.
- **GitHub Pages**: the published site may be at most 1 GB, with a soft limit of 100 GB of traffic a month and of 10
  builds an hour ([GitHub Pages limits](https://docs.github.com/en/pages/getting-started-with-github-pages/github-pages-limits));
  Git refuses files above 100 MB. Good for small surveys: push the folder to a repository and turn on Pages.
- **Tailscale** for a private preview to colleagues: start `ppg web serve SITE_FOLDER` and, in another terminal,
  `tailscale serve 8765` (only the computers of your tailnet see it) - or `tailscale funnel 8765` to open it to the
  internet for a while.
