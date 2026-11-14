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
4. **Log in**, once, in the same terminal:

        wrangler login

   The browser opens on Cloudflare: allow wrangler. The credentials stay with wrangler; the program never sees them.
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
   address**; the address is kept with the project.

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

- The site: *Workers & Pages* > the project > *Settings* > **Delete project**, or in a terminal
  `wrangler pages project delete <name>`.
- The data: *R2* > the bucket > delete its objects, then the bucket; or turn off its public address with
  `wrangler r2 bucket dev-url disable <bucket>`.

## From the command line

    ppg web publish cloudflare SITE_FOLDER --project NAME --dry-run      # the plan, nothing run or sent
    ppg web publish cloudflare SITE_FOLDER --project NAME --yes-public
    ppg web publish cloudflare SITE_FOLDER --project NAME --bucket NAME-data --yes-public
    ppg web publish cloudflare SITE_FOLDER --project NAME --yes-public --yes-true-coordinates   # site built with --frame crs

`--yes-public` is required; a site built with real coordinates also needs `--yes-true-coordinates`. Other options:
`--data auto|pages|r2`, `--data-url https://...` (a custom domain of the bucket), `--origin https://...` (another
address of the page allowed to read the bucket), `--record PROJECT.ppg` (keeps the address in the project).

The wrangler commands the program runs, in order: `whoami --json`; `pages project list --json` and, for a new project,
`pages project create NAME --production-branch main`; with R2: `r2 bucket info BUCKET` (and `r2 bucket create
BUCKET` when missing), `r2 object put BUCKET/KEY --file FILE --content-type TYPE --cache-control "public,
max-age=3600" --remote` for each new or changed file, `r2 object delete BUCKET/KEY --remote` for each file gone,
`r2 bucket dev-url enable BUCKET -y` and `r2 bucket dev-url get BUCKET` (without a custom domain), `r2 bucket cors set
BUCKET --file RULE.json -y`; last `pages deploy FOLDER --project-name NAME --branch main --commit-dirty=true`.

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
