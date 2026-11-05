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

The app needs a small always-on server with a persistent disk (for the SQLite file). Two easy options:

### Option A: Fly.io (about $2-5/month)

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

### Option B: any VPS (Hetzner, DigitalOcean, etc.)

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
