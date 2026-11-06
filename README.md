# HackerGit

Every GitHub repo ever shared on Hacker News, searchable and sortable by points, comments and the poster's karma.
Built for **hackergit.org**, in the spirit of [HackerTube](https://hackertube.org).

- Pure Python standard library: no `pip install`, no build step.
- One SQLite file (~200 MB) holds everything.
- The server keeps itself up to date: new HN posts show up within ~10 minutes.

## Where the data comes from

| What | Source | Key needed? |
|---|---|---|
| Stories linking to `github.com` (title, points, comments, author, date) | [Algolia HN Search API](https://hn.algolia.com/api) | No |
| Poster karma | [Official HN API](https://github.com/HackerNews/API) (`/v0/user/<name>.json`) | No |
| Stars, language, description (optional) | GitHub GraphQL API | `GITHUB_TOKEN` |

No scraping of news.ycombinator.com is needed. HN publishes an official API, and Algolia runs a free full-text
index of every HN item. HackerTube used a third route: the
[open-index/hacker-news](https://huggingface.co/datasets/open-index/hacker-news) Parquet dump on Hugging Face
(full archive, queryable with DuckDB). We checked that Algolia and that dump return the exact same GitHub stories
for a sample month, and Algolia is also live, so HackerGit uses Algolia alone.

## Run it locally

```bash
python3 hackergit.py backfill   # one-time: all GitHub stories since 2007 + every poster's karma (~15-25 min)
python3 server.py               # http://localhost:8000
```

If you skip `backfill`, the server runs it in the background on first start (the site shows results as they arrive).

Other commands: `python3 hackergit.py update | karma | github | rebuild | stats`.

## Settings (environment variables)

| Variable | Default | Meaning |
|---|---|---|
| `PORT` | `8000` | HTTP port |
| `HACKERGIT_DB` | `./data/hackergit.db` | SQLite file location (put it on a persistent disk) |
| `UPDATE_MINUTES` | `10` | How often to pull new posts; `0` turns the updater off |
| `GITHUB_TOKEN` | empty | A GitHub token (no scopes needed) to add stars, language and description |

## Deploy to hackergit.org

The app needs a small always-on server with a persistent disk (for the SQLite file).

### Option A (set up): AWS Lightsail via GitHub Actions (about $7/month)

Every push to `main` runs `.github/workflows/deploy.yml`, which creates a Lightsail server on the first run
(Ubuntu, 1 GB plan, static IP, ports 22/80/443) and then installs or updates the app with `deploy/setup.sh`
(Python + [Caddy](https://caddyserver.com) for automatic HTTPS, run by systemd). No AWS keys are stored in GitHub:
the workflow signs in with GitHub's OIDC and uses a short-lived SSH certificate from Lightsail.

One-time setup:

1. In the AWS console of the hosting account, open **CloudShell** (the `>_` icon), upload
   `deploy/aws-bootstrap.sh` (Actions → Upload file) and run:
   ```bash
   EMAIL=you@example.com bash aws-bootstrap.sh
   ```
   It creates the GitHub sign-in provider, a `hackergit-deploy` role limited to Lightsail and to this repo's
   `main` branch, and a $10/month budget alert. It prints the role ARN.
2. In GitHub: **Settings → Secrets and variables → Actions → New repository secret**:
   `AWS_ROLE_ARN` = the ARN from step 1.
   Optional: `STARS_GITHUB_TOKEN`, a GitHub token with no extra permissions, to show stars and languages.
   Optional variables: `DOMAIN` (default `hackergit.org`), `AWS_REGION` (default `us-east-1`).
3. **Actions → Deploy to AWS Lightsail → Run workflow** (or push to `main`). The run summary shows the server IP.
4. At your domain registrar, add `A` records for `hackergit.org` and `www.hackergit.org` pointing to that IP.

Logs on the server: `sudo journalctl -u hackergit -f` (connect with the browser SSH button in the Lightsail console).

### Option B: Fly.io (about $2-5/month)

```bash
fly launch --copy-config --no-deploy      # pick a unique app name if "hackergit" is taken
fly volumes create data --size 1
fly secrets set GITHUB_TOKEN=ghp_...      # optional
fly deploy
fly certs add hackergit.org
fly certs add www.hackergit.org
```

`fly certs add` prints the DNS records to add at your domain registrar (an `A` and `AAAA` record for
`hackergit.org`, and a `CNAME` for `www`).

### Option C: any VPS with Docker (Hetzner, DigitalOcean, etc.)

```bash
git clone <this repo> /opt/hackergit && cd /opt/hackergit
docker build -t hackergit .
docker run -d --name hackergit --restart=always -p 127.0.0.1:8080:8080 -v hackergit-data:/data \
  -e GITHUB_TOKEN=ghp_... hackergit
```

Then put [Caddy](https://caddyserver.com) in front for automatic HTTPS. `/etc/caddy/Caddyfile`:

```
hackergit.org, www.hackergit.org {
    encode gzip
    reverse_proxy 127.0.0.1:8080
}
```

Point an `A` record for `hackergit.org` (and `www`) at the server's IP.

## README badge

Show how your repo did on Hacker News. Replace `owner/repo` with yours:

```html
<a href="https://hackergit.org/r/owner/repo"><img src="https://hackergit.org/badge/owner/repo.svg?style=card" alt="Hacker News" width="250" height="55"></a>
```

<a href="https://hackergit.org/r/karpathy/nanoGPT"><img src="https://hackergit.org/badge/karpathy/nanoGPT.svg?style=card" alt="Hacker News" width="250" height="55"></a>

The card is 250×55, the same size as Trendshift-style README cards, and shows points, comments and number of HN posts.
Add `&theme=dark` for dark READMEs.

**Rank badge** (`?style=rank`): shows the repo's best achievement on Hacker News, such as
"#1 Repo of the Day", "#3 Show HN of the Week", "#2 Repo of 2023" or a live front page position.
Ranks compare GitHub posts on HN by points for the same day, week, month or year. Falls back to the points card.

<a href="https://hackergit.org/r/karpathy/nanoGPT"><img src="https://hackergit.org/badge/karpathy/nanoGPT.svg?style=rank" alt="Hacker News" width="250" height="55"></a>

Prefer a small shields.io-style badge? Drop `?style=card`:

[![Hacker News](https://hackergit.org/badge/karpathy/nanoGPT.svg)](https://hackergit.org/r/karpathy/nanoGPT)

Options for the small badge: `?show=comments` or `?show=posts` for other numbers, `?label=HN` for a shorter label.

Prefer to send readers straight to Hacker News? Point the link at your HN post instead
(`https://news.ycombinator.com/item?id=...`); the badge picker on each repo page has a checkbox for this.
It shows the best-scoring post and updates on its own. Every repo page on hackergit.org has a copy button.

## API

All endpoints return JSON.

- `GET /api/search` with
  `q` (text, plus `by:user`, `owner:org`, `lang:go`),
  `view=repos|posts`,
  `sort=points|comments|karma|stars|new|old|subs`,
  `min_points`, `min_comments`, `min_karma`, `max_karma`, `min_stars`, `from` / `to` (years), `page`
- `GET /api/repo?key=owner/name`: the repo and every HN post of it
- `GET /api/user?name=pg`: karma and totals for an HN user
- `GET /api/stats`
- `GET /badge/<owner>/<repo>.svg`: README badge (see above)
- `GET /sitemap.xml`: sitemap index of every repo page

## How it fits together

- `hackergit.py`: database schema, Algolia/HN/GitHub fetchers, and the `repos` summary table
  (one row per repo: best post, total comments, number of posts, poster karma, stars).
- `server.py`: HTTP server, search queries (SQLite FTS5), and the background updater thread.
- `static/index.html`: the whole front end.

Each update cycle pulls the last 3 days of GitHub stories (new ones plus fresh points and comment counts), fetches
karma for new posters, re-checks karma for the 500 stalest posters, refreshes GitHub data when a token is set,
then rebuilds the summary table in one transaction so readers never see a half-built index.

## License

Code: MIT. Hacker News content belongs to its authors.
