"""HackerGit: every GitHub repo shared on Hacker News, searchable by points, comments and poster karma.

Data sources (all free, no keys needed):
  - Algolia HN Search API (hn.algolia.com/api) for stories whose URL points at github.com
  - Official HN Firebase API (hacker-news.firebaseio.com) for each poster's karma
  - GitHub GraphQL API for stars/language/description (optional, needs GITHUB_TOKEN)

Usage:
  python hackergit.py backfill      # one-time: pull every GitHub story since 2007 (~10-20 min)
  python hackergit.py update        # pull recent stories, refresh karma, rebuild the index
  python hackergit.py stats
"""
import json
import os
import re
import sqlite3
import sys
import time
import urllib.parse
import urllib.request
from concurrent.futures import ThreadPoolExecutor

DB_PATH = os.environ.get("HACKERGIT_DB", os.path.join(os.path.dirname(os.path.abspath(__file__)), "data", "hackergit.db"))
GITHUB_TOKEN = os.environ.get("GITHUB_TOKEN", "")
UA = "HackerGit/1.0 (+https://hackergit.org)"

ALGOLIA = "https://hn.algolia.com/api/v1/search_by_date"
FIREBASE_USER = "https://hacker-news.firebaseio.com/v0/user/{}.json"

# First path segments on github.com that are site pages, not users or orgs.
RESERVED = {
    "about", "account", "apps", "blog", "collections", "contact", "customer-stories", "enterprise",
    "events", "explore", "features", "git-guides", "login", "join", "marketplace", "new", "notifications",
    "orgs", "organizations", "pricing", "readme", "search", "security", "settings", "site", "sponsors",
    "topics", "trending", "users", "codespaces", "education", "mobile", "team", "home", "premium-support",
    "resources", "solutions", "signup", "copilot", "pulls", "issues", "dashboard", "watching", "stars",
    "sessions", "password_reset", "community", "partners", "case-studies", "open-source", "why-github",
    "accelerator", "github-copilot", "mcp", "models", "status", "shop", "logos", "careers", "press",
}
REPO_RE = re.compile(r"^https?://(?:www\.)?github\.com/([A-Za-z0-9][A-Za-z0-9-]{0,38})/([A-Za-z0-9._-]{1,100})", re.I)


def parse_repo(url):
    """Return 'owner/name' for a github.com repo URL, or None."""
    if not url:
        return None
    m = REPO_RE.match(url.strip())
    if not m:
        return None
    owner, name = m.group(1), m.group(2)
    if name.lower().endswith(".git"):
        name = name[:-4]
    if owner.lower() in RESERVED or name in ("", ".", ".."):
        return None
    return f"{owner}/{name}"


# ---------------------------------------------------------------- database

SCHEMA = """
PRAGMA journal_mode=WAL;
CREATE TABLE IF NOT EXISTS stories (
  id INTEGER PRIMARY KEY, repo TEXT NOT NULL, url TEXT, title TEXT, author TEXT,
  points INTEGER DEFAULT 0, comments INTEGER DEFAULT 0, time INTEGER
);
DROP INDEX IF EXISTS stories_repo;
CREATE INDEX IF NOT EXISTS stories_rkey ON stories(lower(repo));
CREATE INDEX IF NOT EXISTS stories_author ON stories(author);
CREATE INDEX IF NOT EXISTS stories_time ON stories(time);
CREATE TABLE IF NOT EXISTS users (name TEXT PRIMARY KEY, karma INTEGER, created INTEGER, fetched INTEGER);
CREATE TABLE IF NOT EXISTS github (
  key TEXT PRIMARY KEY, stars INTEGER, lang TEXT, descr TEXT, archived INTEGER, missing INTEGER DEFAULT 0, fetched INTEGER
);
CREATE TABLE IF NOT EXISTS meta (k TEXT PRIMARY KEY, v TEXT);
-- Derived tables, rebuilt by rebuild(). One row per repo; one row per post with karma joined in.
CREATE TABLE IF NOT EXISTS repos (
  key TEXT PRIMARY KEY, repo TEXT, owner TEXT, name TEXT,
  subs INTEGER, points INTEGER, total_points INTEGER, comments INTEGER,
  first_time INTEGER, last_time INTEGER,
  top_id INTEGER, top_title TEXT, top_author TEXT, karma INTEGER, max_karma INTEGER,
  stars INTEGER, lang TEXT, descr TEXT,
  kinds TEXT,                      -- e.g. ",show,ask," : HN post types this repo has
  show_id INTEGER, show_title TEXT, show_points INTEGER, show_comments INTEGER,  -- best Show HN post
  show_author TEXT, show_karma INTEGER
);
CREATE INDEX IF NOT EXISTS repos_points ON repos(points);
CREATE INDEX IF NOT EXISTS repos_comments ON repos(comments);
CREATE INDEX IF NOT EXISTS repos_karma ON repos(karma);
CREATE INDEX IF NOT EXISTS repos_last ON repos(last_time);
CREATE INDEX IF NOT EXISTS repos_stars ON repos(stars);
CREATE INDEX IF NOT EXISTS repos_subs ON repos(subs);
CREATE INDEX IF NOT EXISTS repos_show_points ON repos(show_points);
CREATE VIRTUAL TABLE IF NOT EXISTS repos_fts USING fts5(key UNINDEXED, repo, titles, descr, tokenize='unicode61 tokenchars ''-_''');
CREATE VIRTUAL TABLE IF NOT EXISTS stories_fts USING fts5(title, repo, content='stories', content_rowid='id', tokenize='unicode61 tokenchars ''-_''');
CREATE TRIGGER IF NOT EXISTS stories_ai AFTER INSERT ON stories BEGIN
  INSERT INTO stories_fts(rowid, title, repo) VALUES (new.id, new.title, replace(new.repo, '/', ' '));
END;
CREATE TRIGGER IF NOT EXISTS stories_ad AFTER DELETE ON stories BEGIN
  INSERT INTO stories_fts(stories_fts, rowid, title, repo) VALUES ('delete', old.id, old.title, replace(old.repo, '/', ' '));
END;
CREATE TRIGGER IF NOT EXISTS stories_au AFTER UPDATE OF title, repo ON stories BEGIN
  INSERT INTO stories_fts(stories_fts, rowid, title, repo) VALUES ('delete', old.id, old.title, replace(old.repo, '/', ' '));
  INSERT INTO stories_fts(rowid, title, repo) VALUES (new.id, new.title, replace(new.repo, '/', ' '));
END;
"""


def connect(readonly=False):
    os.makedirs(os.path.dirname(DB_PATH), exist_ok=True)
    if readonly:
        con = sqlite3.connect(f"file:{DB_PATH}?mode=ro", uri=True, check_same_thread=False, timeout=30)
    else:
        con = sqlite3.connect(DB_PATH, timeout=60)
        cols = {r[1] for r in con.execute("PRAGMA table_info(repos)")}
        migrate = bool(cols) and "kinds" not in cols
        if migrate:
            con.execute("DROP TABLE repos")
        con.executescript(SCHEMA)
        if migrate:
            con.row_factory = sqlite3.Row
            rebuild(con)
    con.row_factory = sqlite3.Row
    return con


def meta_get(con, k, default=None):
    r = con.execute("SELECT v FROM meta WHERE k=?", (k,)).fetchone()
    return r[0] if r else default


def meta_set(con, k, v):
    con.execute("INSERT INTO meta(k,v) VALUES(?,?) ON CONFLICT(k) DO UPDATE SET v=excluded.v", (k, str(v)))


# ---------------------------------------------------------------- HTTP helpers

def http_json(url, data=None, headers=None, retries=4, timeout=60):
    h = {"User-Agent": UA, "Accept": "application/json"}
    h.update(headers or {})
    body = json.dumps(data).encode() if data is not None else None
    if body:
        h["Content-Type"] = "application/json"
    for attempt in range(retries):
        try:
            req = urllib.request.Request(url, data=body, headers=h)
            with urllib.request.urlopen(req, timeout=timeout) as r:
                return json.loads(r.read())
        except urllib.error.HTTPError as e:
            if e.code in (401, 403, 404) and attempt >= 1:
                raise
            wait = 2 ** attempt * (5 if e.code == 429 else 1)
        except Exception:
            wait = 2 ** attempt
        if attempt == retries - 1:
            raise
        time.sleep(wait)


def log(*a):
    print(time.strftime("%H:%M:%S"), *a, flush=True)


# ---------------------------------------------------------------- stories (Algolia)

def algolia_page(before, after=0):
    """Up to 1000 stories with a github.com URL, newest first, with after <= time < before."""
    params = {
        "tags": "story",
        "query": "github.com",
        "restrictSearchableAttributes": "url",
        "typoTolerance": "false",
        "hitsPerPage": "1000",
        "attributesToRetrieve": "title,url,author,points,num_comments,created_at_i",
        "attributesToHighlight": "",
        "numericFilters": f"created_at_i>={after},created_at_i<{before}",
    }
    d = http_json(ALGOLIA + "?" + urllib.parse.urlencode(params))
    return d.get("hits", [])


def upsert_stories(con, hits):
    rows = []
    for h in hits:
        repo = parse_repo(h.get("url"))
        if not repo or not h.get("author"):
            continue
        rows.append((int(h["objectID"]), repo, h["url"], h.get("title") or "", h["author"],
                     h.get("points") or 0, h.get("num_comments") or 0, h.get("created_at_i")))
    con.executemany("""INSERT INTO stories(id,repo,url,title,author,points,comments,time) VALUES(?,?,?,?,?,?,?,?)
        ON CONFLICT(id) DO UPDATE SET points=excluded.points, comments=excluded.comments,
          title=excluded.title, url=excluded.url, repo=excluded.repo""", rows)
    return len(rows)


def fetch_window(con, after, before, label=""):
    """Walk backwards from `before` to `after`, 1000 stories at a time."""
    cur, total, seen = before, 0, 0
    while cur > after:
        hits = algolia_page(cur, after)
        if not hits:
            break
        total += upsert_stories(con, hits)
        seen += len(hits)
        con.commit()
        oldest = min(h["created_at_i"] for h in hits)
        if len(hits) < 1000:
            break
        # Re-query the oldest second so stories sharing that timestamp aren't lost (upsert dedupes).
        cur = oldest + 1 if oldest + 1 < cur else oldest
        if label:
            log(f"{label} {time.strftime('%Y-%m-%d', time.gmtime(oldest))}  repo-stories so far: {total}")
        time.sleep(0.25)
    return total


def backfill(con):
    start = int(meta_get(con, "backfill_cursor", int(time.time()) + 60))
    log("backfill: walking back from", time.strftime("%Y-%m-%d", time.gmtime(start)))
    year = 365 * 86400
    before = start
    while before > 1160000000:  # HN launched Oct 2006
        after = max(before - year // 4, 1160000000)
        fetch_window(con, after, before, label="backfill")
        before = after
        meta_set(con, "backfill_cursor", before)
        con.commit()
    meta_set(con, "backfill_done", int(time.time()))
    con.commit()


def update_recent(con, days=3):
    """Fetch new stories and refresh points/comments for anything posted in the last few days."""
    latest = con.execute("SELECT max(time) FROM stories").fetchone()[0] or int(time.time()) - days * 86400
    after = min(latest, int(time.time())) - days * 86400
    return fetch_window(con, after, int(time.time()) + 60)


# ---------------------------------------------------------------- karma (HN Firebase API)

def fetch_user(name):
    try:
        d = http_json(FIREBASE_USER.format(urllib.parse.quote(name)), retries=3, timeout=30)
    except Exception:
        return None
    if not d:
        return (name, 0, 0)  # account deleted
    return (name, d.get("karma", 0), d.get("created", 0))


def refresh_karma(con, limit=None, stale_days=7, workers=24):
    now = int(time.time())
    missing = [r[0] for r in con.execute(
        "SELECT DISTINCT s.author FROM stories s LEFT JOIN users u ON u.name=s.author WHERE u.name IS NULL")]
    stale = [r[0] for r in con.execute(
        "SELECT name FROM users WHERE fetched < ? ORDER BY fetched LIMIT ?", (now - stale_days * 86400, limit or 500))]
    names = missing + stale
    if limit:
        names = names[:limit]
    if not names:
        return 0
    log(f"karma: fetching {len(names)} users ({len(missing)} new)")
    done = 0
    with ThreadPoolExecutor(workers) as ex:
        batch = []
        for res in ex.map(fetch_user, names):
            if res:
                batch.append((*res, now))
            if len(batch) >= 500:
                con.executemany("INSERT OR REPLACE INTO users(name,karma,created,fetched) VALUES(?,?,?,?)", batch)
                con.commit()
                done += len(batch)
                batch = []
                log(f"karma: {done}/{len(names)}")
        con.executemany("INSERT OR REPLACE INTO users(name,karma,created,fetched) VALUES(?,?,?,?)", batch)
        con.commit()
        done += len(batch)
    return done


# ---------------------------------------------------------------- GitHub stars (optional)

def refresh_github(con, limit=25000, stale_days=14):
    if not GITHUB_TOKEN:
        meta_set(con, "github_status", "no token set")
        con.commit()
        return 0
    now = int(time.time())
    keys = [r[0] for r in con.execute("""
        SELECT r.key FROM repos r LEFT JOIN github g ON g.key=r.key
        WHERE g.key IS NULL OR g.fetched < ? ORDER BY g.fetched IS NOT NULL, r.points DESC LIMIT ?""",
        (now - stale_days * 86400, limit))]
    done = 0
    for i in range(0, len(keys), 80):
        chunk = keys[i:i + 80]
        parts = []
        for j, k in enumerate(chunk):
            owner, name = k.split("/", 1)
            parts.append(f'r{j}: repository(owner: {json.dumps(owner)}, name: {json.dumps(name)}) '
                         '{ stargazerCount isArchived description primaryLanguage { name } }')
        q = "query {" + "\n".join(parts) + "}"
        try:
            d = http_json("https://api.github.com/graphql", data={"query": q},
                          headers={"Authorization": f"bearer {GITHUB_TOKEN}"}, retries=3)
        except Exception as e:
            log("github: request failed:", e)
            meta_set(con, "github_status", f"error: {e}"[:200])
            con.commit()
            break
        data = d.get("data")
        if not data:  # bad token, rate limit, etc.: keep the repos queued instead of marking them missing
            msg = "; ".join(x.get("message", "") for x in d.get("errors") or []) or "no data returned"
            log("github: error:", msg)
            meta_set(con, "github_status", f"error: {msg}"[:200])
            con.commit()
            break
        rows = []
        for j, k in enumerate(chunk):
            r = data.get(f"r{j}")
            if r:
                rows.append((k, r["stargazerCount"], (r.get("primaryLanguage") or {}).get("name"),
                             (r.get("description") or "")[:400], int(r["isArchived"]), 0, now))
            else:
                rows.append((k, None, None, None, None, 1, now))
        con.executemany("INSERT OR REPLACE INTO github(key,stars,lang,descr,archived,missing,fetched) VALUES(?,?,?,?,?,?,?)", rows)
        con.commit()
        done += len(rows)
        time.sleep(0.5)
    if done:
        log(f"github: refreshed {done} repos")
        meta_set(con, "github_status", f"ok: {done} repos refreshed at {time.strftime('%Y-%m-%d %H:%M UTC', time.gmtime())}")
        con.commit()
    return done


# ---------------------------------------------------------------- derived index

def rebuild(con):
    """Recompute per-repo aggregates and the repo search index in one transaction."""
    t = time.time()
    con.execute("BEGIN IMMEDIATE")
    con.execute("DELETE FROM repos")
    con.execute("""
        INSERT INTO repos(key, repo, owner, name, subs, points, total_points, comments, first_time, last_time,
                          top_id, top_title, top_author, karma, max_karma, stars, lang, descr,
                          kinds, show_id, show_title, show_points, show_comments, show_author, show_karma)
        WITH s AS (
          SELECT st.*, lower(st.repo) AS k, coalesce(u.karma, 0) AS karma,
                 row_number() OVER (PARTITION BY lower(st.repo) ORDER BY st.points DESC, st.time) AS rn
          FROM stories st LEFT JOIN users u ON u.name = st.author
        ), agg AS (
          SELECT k, count(*) subs, max(points) points, sum(points) total_points, sum(comments) comments,
                 min(time) first_time, max(time) last_time, max(karma) max_karma,
                 ',' || (CASE WHEN max(title LIKE 'Show HN%') THEN 'show,' ELSE '' END)
                     || (CASE WHEN max(title LIKE 'Launch HN%') THEN 'launch,' ELSE '' END)
                     || (CASE WHEN max(title LIKE 'Ask HN%') THEN 'ask,' ELSE '' END)
                     || (CASE WHEN max(title LIKE 'Tell HN%') THEN 'tell,' ELSE '' END) AS kinds
          FROM s GROUP BY k
        ), sh AS (
          SELECT k, id, title, points, comments, author, karma,
                 row_number() OVER (PARTITION BY k ORDER BY points DESC, time) AS rn
          FROM s WHERE title LIKE 'Show HN%'
        )
        SELECT a.k, t.repo, substr(t.repo, 1, instr(t.repo, '/') - 1), substr(t.repo, instr(t.repo, '/') + 1),
               a.subs, a.points, a.total_points, a.comments, a.first_time, a.last_time,
               t.id, t.title, t.author, t.karma, a.max_karma, g.stars, g.lang, g.descr,
               a.kinds, sh.id, sh.title, sh.points, sh.comments, sh.author, sh.karma
        FROM agg a JOIN s t ON t.k = a.k AND t.rn = 1
        LEFT JOIN sh ON sh.k = a.k AND sh.rn = 1
        LEFT JOIN github g ON g.key = a.k
    """)
    con.execute("DELETE FROM repos_fts")
    con.execute("""
        INSERT INTO repos_fts(key, repo, titles, descr)
        SELECT r.key, replace(r.repo, '/', ' ') || ' ' || r.repo, t.titles, coalesce(r.descr, '')
        FROM repos r JOIN (SELECT lower(repo) AS k, group_concat(title, ' | ') AS titles
                           FROM stories GROUP BY lower(repo)) t ON t.k = r.key
    """)
    meta_set(con, "last_rebuild", int(time.time()))
    con.commit()
    log(f"rebuild: {con.execute('SELECT count(*) FROM repos').fetchone()[0]} repos in {time.time() - t:.1f}s")


def update(con, karma_limit=None):
    n = update_recent(con)
    log(f"update: {n} recent repo-stories fetched")
    refresh_karma(con, limit=karma_limit)
    rebuild(con)
    if refresh_github(con):
        rebuild(con)
    meta_set(con, "last_update", int(time.time()))
    con.commit()


def stats(con):
    q = lambda s: con.execute(s).fetchone()[0]
    return {
        "stories": q("SELECT count(*) FROM stories"),
        "repos": q("SELECT count(*) FROM repos"),
        "users": q("SELECT count(*) FROM users"),
        "with_stars": q("SELECT count(*) FROM github WHERE stars IS NOT NULL"),
        "first": q("SELECT min(time) FROM stories"),
        "last": q("SELECT max(time) FROM stories"),
        "last_update": int(meta_get(con, "last_update", 0) or 0),
        "github_status": meta_get(con, "github_status", "not run yet"),
        "version": meta_get(con, "last_rebuild", "0"),
    }


def main():
    cmd = sys.argv[1] if len(sys.argv) > 1 else "help"
    con = connect()
    if cmd == "backfill":
        backfill(con)
        refresh_karma(con)
        rebuild(con)
        refresh_github(con, limit=10 ** 9) and rebuild(con)
        meta_set(con, "last_update", int(time.time()))
        con.commit()
    elif cmd == "update":
        update(con)
    elif cmd == "karma":
        refresh_karma(con)
        rebuild(con)
    elif cmd == "github":
        refresh_github(con, limit=10 ** 9)
        rebuild(con)
    elif cmd == "rebuild":
        rebuild(con)
    elif cmd == "stats":
        print(json.dumps(stats(con), indent=2))
    else:
        print(__doc__)


if __name__ == "__main__":
    main()
