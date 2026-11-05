"""HackerGit web server: JSON API + single-page UI + background updater. Python stdlib only.

  python server.py                 # serves on $PORT (default 8000)

Env vars:
  HOST             address to listen on (default 0.0.0.0)
  PORT             port to listen on (default 8000)
  HACKERGIT_DB     path to the SQLite file (default ./data/hackergit.db)
  UPDATE_MINUTES   how often to pull new stories from HN (default 10, 0 disables)
  GITHUB_TOKEN     optional; enables stars / language / description
"""
import gzip
import json
import os
import re
import threading
import time
import traceback
from collections import OrderedDict
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlencode, urlparse

import hackergit as hg

ROOT = os.path.dirname(os.path.abspath(__file__))
INDEX = os.path.join(ROOT, "static", "index.html")
SITE = os.environ.get("SITE_URL", "https://hackergit.org").rstrip("/")
STATIC_FILES = {"/og.png": ("og.png", "image/png")}
SITEMAP_CHUNK = 40000  # sitemap files may hold at most 50,000 URLs
PAGE_SIZE = 30

_local = threading.local()
# Responses are cached until the next index rebuild (every UPDATE_MINUTES), not per database write.
_cache = OrderedDict()
_cache_lock = threading.Lock()
_version = {"v": None, "checked": 0.0}
CACHE_MAX = 5000


def db():
    con = getattr(_local, "con", None)
    if con is None:
        con = _local.con = hg.connect(readonly=True)
    return con


# ---------------------------------------------------------------- query building

SORTS_REPOS = {
    "points": "r.points DESC", "comments": "r.comments DESC", "karma": "r.karma DESC",
    "stars": "coalesce(r.stars, -1) DESC", "new": "r.last_time DESC", "old": "r.first_time ASC",
    "subs": "r.subs DESC, r.points DESC", "total": "r.total_points DESC",
}
SORTS_POSTS = {
    "points": "s.points DESC", "comments": "s.comments DESC", "karma": "karma DESC",
    "stars": "coalesce(g.stars, -1) DESC", "new": "s.time DESC", "old": "s.time ASC",
    "subs": "s.points DESC", "total": "s.points DESC",
}


def fts_query(text):
    words = re.findall(r"[\w.\-/+#]+", text, re.UNICODE)
    words = [w.replace('"', "") for w in words if w.strip(".-/")]
    if not words:
        return None
    parts = [f'"{w}"' for w in words]
    parts[-1] += "*"
    return " ".join(parts)


def parse_q(q):
    """Pull out by:user, owner:name and lang:x filters; the rest is full-text."""
    filters = {}
    rest = []
    for tok in (q or "").split():
        m = re.match(r"^(by|owner|lang|user):(.+)$", tok, re.I)
        if m:
            key = m.group(1).lower()
            filters["by" if key == "user" else key] = m.group(2)
        else:
            rest.append(tok)
    return " ".join(rest), filters


def num(p, k, default=None):
    try:
        return int(p[k][0])
    except (KeyError, ValueError, IndexError):
        return default


def year_ts(y, end=False):
    return int(time.mktime(time.strptime(f"{y + (1 if end else 0)}-01-01", "%Y-%m-%d")))


def search(p):
    view = (p.get("view") or ["repos"])[0]
    sort = (p.get("sort") or ["points"])[0]
    page = max(0, min(num(p, "page", 0), 200))
    text, f = parse_q((p.get("q") or [""])[0])
    for k in ("by", "owner", "lang"):
        if p.get(k) and p[k][0]:
            f[k] = p[k][0]
    match = fts_query(text)

    where, args = [], []
    if view == "posts":
        t, kc = "s", "karma"
        if match:
            where.append("s.id IN (SELECT rowid FROM stories_fts WHERE stories_fts MATCH ?)")
            args.append(match)
        cols = {"points": "s.points", "comments": "s.comments", "karma": "coalesce(u.karma,0)",
                "stars": "g.stars", "time_from": "s.time", "time_to": "s.time"}
    else:
        t = "r"
        if match:
            where.append("r.key IN (SELECT key FROM repos_fts WHERE repos_fts MATCH ?)")
            args.append(match)
        cols = {"points": "r.points", "comments": "r.comments", "karma": "r.karma",
                "stars": "r.stars", "time_from": "r.last_time", "time_to": "r.first_time"}

    for key, col, op in (("min_points", cols["points"], ">="), ("min_comments", cols["comments"], ">="),
                         ("min_karma", cols["karma"], ">="), ("max_karma", cols["karma"], "<="),
                         ("min_stars", cols["stars"], ">=")):
        v = num(p, key)
        if v is not None:
            where.append(f"{col} {op} ?")
            args.append(v)
    yf, yt = num(p, "from"), num(p, "to")
    if yf:
        where.append(f"{cols['time_from']} >= ?")
        args.append(year_ts(yf))
    if yt:
        where.append(f"{cols['time_to']} < ?")
        args.append(year_ts(yt, end=True))
    if f.get("by"):
        if view == "posts":
            where.append("s.author = ?")
        else:
            where.append("r.key IN (SELECT lower(repo) FROM stories WHERE author = ?)")
        args.append(f["by"])
    if f.get("owner"):
        where.append(f"{'lower(s.repo)' if view == 'posts' else 'r.key'} LIKE ?")
        args.append(f["owner"].lower().replace("%", "").replace("_", r"\_") + "/%")
        where[-1] += " ESCAPE '\\'"
    if f.get("lang"):
        where.append(f"lower({'g.lang' if view == 'posts' else 'r.lang'}) = ?")
        args.append(f["lang"].lower())

    w = ("WHERE " + " AND ".join(where)) if where else ""
    con = db()
    if view == "posts":
        order = SORTS_POSTS.get(sort, SORTS_POSTS["points"])
        base = f"""FROM stories s LEFT JOIN users u ON u.name = s.author
                   LEFT JOIN github g ON g.key = lower(s.repo) {w}"""
        total = con.execute(f"SELECT count(*) {base}", args).fetchone()[0]
        rows = con.execute(f"""SELECT s.id, s.repo, s.url, s.title, s.author, s.points, s.comments, s.time,
                coalesce(u.karma, 0) AS karma, g.stars, g.lang, g.descr {base}
                ORDER BY {order}, s.id DESC LIMIT ? OFFSET ?""", args + [PAGE_SIZE, page * PAGE_SIZE]).fetchall()
    else:
        order = SORTS_REPOS.get(sort, SORTS_REPOS["points"])
        total = con.execute(f"SELECT count(*) FROM repos r {w}", args).fetchone()[0]
        rows = con.execute(f"""SELECT r.* FROM repos r {w} ORDER BY {order}, r.key
                LIMIT ? OFFSET ?""", args + [PAGE_SIZE, page * PAGE_SIZE]).fetchall()
    return {"view": view, "total": total, "page": page, "page_size": PAGE_SIZE,
            "results": [dict(r) for r in rows]}


def repo_detail(key):
    con = db()
    r = con.execute("SELECT * FROM repos WHERE key = ?", (key.lower(),)).fetchone()
    if not r:
        return None
    posts = con.execute("""SELECT s.id, s.title, s.url, s.author, s.points, s.comments, s.time,
            coalesce(u.karma, 0) AS karma FROM stories s LEFT JOIN users u ON u.name = s.author
            WHERE lower(s.repo) = ? ORDER BY s.time DESC""", (key.lower(),)).fetchall()
    return {"repo": dict(r), "posts": [dict(x) for x in posts]}


def user_detail(name):
    con = db()
    u = con.execute("SELECT name, karma, created FROM users WHERE name = ?", (name,)).fetchone()
    if not u:
        return None
    s = con.execute("SELECT count(*) n, sum(points) pts FROM stories WHERE author = ?", (name,)).fetchone()
    return {**dict(u), "posts": s["n"], "points": s["pts"] or 0}


def data_version(force=False):
    now = time.time()
    if force or now - _version["checked"] > 5:
        _version["v"] = hg.meta_get(db(), "last_rebuild", "0")
        _version["checked"] = now
    return _version["v"]


def cached_api(path, query):
    """JSON body for an API call, from cache when the index hasn't changed since it was computed."""
    p = parse_qs(query)
    key = path + "?" + urlencode(sorted((k, v[0]) for k, v in p.items()))
    v = data_version()
    with _cache_lock:
        hit = _cache.get(key)
        if hit and hit[0] == v:
            _cache.move_to_end(key)
            return hit[1]
    data = api(path, p)
    if data is None:
        return None
    body = json.dumps(data, separators=(",", ":"))
    with _cache_lock:
        _cache[key] = (v, body)
        while len(_cache) > CACHE_MAX:
            _cache.popitem(last=False)
    return body


def cached(key, build):
    """Cache any computed text until the next index rebuild."""
    v = data_version()
    with _cache_lock:
        hit = _cache.get(key)
        if hit and hit[0] == v:
            return hit[1]
    body = build()
    with _cache_lock:
        _cache[key] = (v, body)
    return body


def xml_esc(t):
    return str(t).replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;").replace('"', "&quot;")


def day(ts):
    return time.strftime("%Y-%m-%d", time.gmtime(ts or 0))


def sitemap_index():
    n = db().execute("SELECT count(*) FROM repos").fetchone()[0]
    files = ["sitemaps/pages.xml"] + [f"sitemaps/repos-{i}.xml" for i in range((n + SITEMAP_CHUNK - 1) // SITEMAP_CHUNK)]
    today = day(time.time())
    items = "".join(f"<sitemap><loc>{SITE}/{f}</loc><lastmod>{today}</lastmod></sitemap>" for f in files)
    return ('<?xml version="1.0" encoding="UTF-8"?>\n'
            f'<sitemapindex xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">{items}</sitemapindex>\n')


def sitemap_urls(urls):
    items = "".join(f"<url><loc>{xml_esc(loc)}</loc>{f'<lastmod>{lm}</lastmod>' if lm else ''}</url>" for loc, lm in urls)
    return ('<?xml version="1.0" encoding="UTF-8"?>\n'
            f'<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">{items}</urlset>\n')


def sitemap_file(name):
    if name == "pages.xml":
        return sitemap_urls([(f"{SITE}/", day(time.time())), (f"{SITE}/about", None)])
    m = re.fullmatch(r"repos-(\d+)\.xml", name)
    if not m:
        return None
    rows = db().execute("SELECT repo, last_time FROM repos ORDER BY key LIMIT ? OFFSET ?",
                        (SITEMAP_CHUNK, int(m.group(1)) * SITEMAP_CHUNK)).fetchall()
    if not rows:
        return None
    return sitemap_urls([(f"{SITE}/r/{r['repo']}", day(r["last_time"])) for r in rows])


_index_html = [None]


def page_html(path):
    """The single-page app, with a real title and description for repo pages (for search engines and link previews)."""
    if _index_html[0] is None:
        with open(INDEX, encoding="utf-8") as fh:
            _index_html[0] = fh.read()
    html = _index_html[0]
    m = re.fullmatch(r"/r/([^/]+/[^/]+)/?", path)
    r = db().execute("SELECT * FROM repos WHERE key = ?", (m.group(1).lower(),)).fetchone() if m else None
    if not r:
        if path == "/about":
            html = html.replace("<title>HackerGit</title>", "<title>About - HackerGit</title>", 1)
        return html
    title = f"{r['repo']} on Hacker News - HackerGit"
    desc = (f"{r['repo']}: {r['points']:,} points and {r['comments']:,} comments across {r['subs']} Hacker News "
            f"post{'s' if r['subs'] != 1 else ''}. Top post: “{r['top_title']}”."
            + (f" {r['descr']}" if r["descr"] else ""))[:300]
    url = f"{SITE}/r/{r['repo']}"
    t, d, u = xml_esc(title), xml_esc(desc), xml_esc(url)
    html = html.replace("<title>HackerGit</title>", f"<title>{t}</title>", 1)
    html = re.sub(r'<meta name="description" content="[^"]*">', f'<meta name="description" content="{d}">', html, count=1)
    html = re.sub(r'<meta property="og:title" content="[^"]*">', f'<meta property="og:title" content="{t}">', html, count=1)
    html = re.sub(r'<meta property="og:description" content="[^"]*">', f'<meta property="og:description" content="{d}">', html, count=1)
    html = html.replace('<link rel="canonical" href="https://hackergit.org/">', f'<link rel="canonical" href="{u}">', 1)
    html = html.replace('<meta property="og:url" content="https://hackergit.org/">', f'<meta property="og:url" content="{u}">', 1)
    return html


def warm_cache():
    """Precompute the pages most people open, right after the index changes."""
    data_version(force=True)
    t = time.time()
    queries = ["", "page=1"] + [f"sort={s}" for s in SORTS_REPOS] + [f"sort={s}&page=1" for s in SORTS_REPOS]
    for q in queries:
        cached_api("/api/search", q)
    cached_api("/api/stats", "")
    hg.log(f"cache: warmed {len(queries) + 1} pages in {time.time() - t:.1f}s")


def api(path, p):
    if path == "/api/search":
        return search(p)
    if path == "/api/repo":
        return repo_detail((p.get("key") or [""])[0])
    if path == "/api/user":
        return user_detail((p.get("name") or [""])[0])
    if path == "/api/stats":
        con = db()
        s = hg.stats(con)
        s["langs"] = [r[0] for r in con.execute(
            "SELECT lang FROM repos WHERE lang IS NOT NULL GROUP BY lang ORDER BY count(*) DESC LIMIT 40")]
        return s
    raise KeyError(path)


# ---------------------------------------------------------------- HTTP

class Handler(BaseHTTPRequestHandler):
    server_version = "HackerGit"

    def log_message(self, fmt, *a):
        if os.environ.get("ACCESS_LOG"):
            super().log_message(fmt, *a)

    def send(self, code, body, ctype, cache="no-cache"):
        if isinstance(body, str):
            body = body.encode()
        gz = "gzip" in (self.headers.get("Accept-Encoding") or "") and len(body) > 1024
        if gz:
            body = gzip.compress(body, 6)
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", cache)
        self.send_header("X-Content-Type-Options", "nosniff")
        if gz:
            self.send_header("Content-Encoding", "gzip")
            self.send_header("Vary", "Accept-Encoding")
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(body)

    def do_HEAD(self):
        self.do_GET()

    def do_GET(self):
        u = urlparse(self.path)
        try:
            if u.path.startswith("/api/"):
                body = cached_api(u.path, u.query)
                if body is None:
                    return self.send(404, '{"error":"not found"}', "application/json")
                return self.send(200, body, "application/json",
                                 "no-cache" if u.path == "/api/stats" else "public, max-age=120")
            if u.path == "/robots.txt":
                return self.send(200, f"User-agent: *\nAllow: /\n\nSitemap: {SITE}/sitemap.xml\n", "text/plain")
            if u.path == "/sitemap.xml":
                return self.send(200, cached("sitemap", sitemap_index), "application/xml", "public, max-age=3600")
            if u.path.startswith("/sitemaps/"):
                name = u.path[len("/sitemaps/"):]
                body = cached("sitemap/" + name, lambda: sitemap_file(name) or "")
                if not body:
                    return self.send(404, "not found", "text/plain")
                return self.send(200, body, "application/xml", "public, max-age=3600")
            if u.path in STATIC_FILES:
                fname, ctype = STATIC_FILES[u.path]
                with open(os.path.join(ROOT, "static", fname), "rb") as fh:
                    return self.send(200, fh.read(), ctype, "public, max-age=86400")
            if u.path == "/healthz":
                return self.send(200, "ok", "text/plain")
            return self.send(200, page_html(u.path), "text/html; charset=utf-8", "public, max-age=300")
        except KeyError:
            return self.send(404, '{"error":"not found"}', "application/json")
        except Exception:
            traceback.print_exc()
            return self.send(500, '{"error":"server error"}', "application/json")


def updater(minutes):
    """Background loop: backfill if the database is empty, then pull fresh stories every few minutes."""
    while True:
        try:
            con = hg.connect()
            if not hg.meta_get(con, "backfill_done"):
                hg.log("updater: no data yet, running backfill (takes ~10-20 min)")
                hg.backfill(con)
                hg.rebuild(con)  # show results now; karma fills in next
                warm_cache()
                hg.refresh_karma(con)
                hg.rebuild(con)
            hg.update(con)
            con.close()
            warm_cache()
        except Exception:
            traceback.print_exc()
        time.sleep(minutes * 60)


def main():
    port = int(os.environ.get("PORT", "8000"))
    minutes = float(os.environ.get("UPDATE_MINUTES", "10"))
    hg.connect().close()  # make sure the schema exists before readers open it
    if minutes > 0:
        threading.Thread(target=updater, args=(minutes,), daemon=True).start()
    host = os.environ.get("HOST", "0.0.0.0")
    srv = ThreadingHTTPServer((host, port), Handler)
    srv.daemon_threads = True
    hg.log(f"HackerGit on http://{host}:{port}  db={hg.DB_PATH}")
    srv.serve_forever()


if __name__ == "__main__":
    main()
