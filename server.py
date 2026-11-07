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
import queue
import re
import subprocess
import sys
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
STATIC_FILES = {"/og.png": ("og.png", "image/png"), "/sw.js": ("sw.js", "application/javascript")}
SITEMAP_CHUNK = 40000  # sitemap files may hold at most 50,000 URLs
PAGE_SIZE = 30

_local = threading.local()
# Responses are cached until the next index rebuild (every UPDATE_MINUTES), not per database write.
_cache = OrderedDict()
_cache_lock = threading.Lock()
_version = {"v": None, "checked": 0.0}
CACHE_MAX = 5000


# Request threads are short-lived, so read connections are pooled and reused (warm caches) instead of
# being opened per request.
_pool = queue.LifoQueue()


def db():
    con = getattr(_local, "con", None)
    if con is None:
        try:
            con = _pool.get_nowait()
        except queue.Empty:
            con = hg.connect(readonly=True)
        _local.con = con
    return con


def release_db():
    con = getattr(_local, "con", None)
    if con is not None:
        _local.con = None
        if _pool.qsize() < 64:
            _pool.put(con)
        else:
            con.close()


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


KINDS = {"show": "Show HN", "launch": "Launch HN", "ask": "Ask HN", "tell": "Tell HN", "other": ""}


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
    kind = (p.get("type") or [""])[0]
    if kind not in KINDS:
        kind = ""
    show = view != "posts" and kind == "show"  # rank and display repos by their best Show HN post

    where, args = [], []
    repo_from, match_args = "repos r", []
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
            # Collect the matching repos first (both search indexes), then filter/sort just those.
            # CROSS JOIN makes SQLite start from the (usually small) match set instead of all repos.
            mk = "SELECT key FROM repos_fts WHERE repos_fts MATCH ?"
            match_args = [match]
            if (p.get("readme") or ["1"])[0] != "0":  # also match README text
                mk += " UNION SELECT m.key FROM readme_fts f JOIN readme_map m ON m.rid = f.rowid WHERE readme_fts MATCH ?"
                match_args.append(match)
            repo_from = f"({mk}) AS mk CROSS JOIN repos r ON r.key = mk.key"
        cols = {"points": "r.points", "comments": "r.comments", "karma": "r.karma",
                "stars": "r.stars", "time_from": "r.last_time", "time_to": "r.first_time"}
        if show:
            cols.update(points="r.show_points", comments="r.show_comments", karma="coalesce(r.show_karma, 0)")

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
    if kind:
        if view == "posts":
            if kind == "other":
                where.append(" AND ".join(f"s.title NOT LIKE '{x}%'" for x in KINDS.values() if x))
            else:
                where.append("s.title LIKE ?")
                args.append(KINDS[kind] + "%")
        else:
            where.append("r.kinds = ','" if kind == "other" else "r.kinds LIKE ?")
            if kind != "other":
                args.append(f"%,{kind},%")

    w = ("WHERE " + " AND ".join(where)) if where else ""
    con = db()
    if view == "posts":
        order = SORTS_POSTS.get(sort, SORTS_POSTS["points"])
        base = f"""FROM stories s LEFT JOIN users u ON u.name = s.author
                   LEFT JOIN github g ON g.key = lower(s.repo) {w}"""
        total = con.execute(f"SELECT count(*) {base}", args).fetchone()[0]
        rows = con.execute(f"""SELECT s.id, s.repo, s.url, s.title, s.author, s.points, s.comments, s.time,
                coalesce(u.karma, 0) AS karma, g.stars, g.lang, g.descr, g.avatar {base}
                ORDER BY {order}, s.id DESC LIMIT ? OFFSET ?""", args + [PAGE_SIZE, page * PAGE_SIZE]).fetchall()
    else:
        order = SORTS_REPOS.get(sort, SORTS_REPOS["points"])
        order_args = []
        if show:
            order = {"points": "r.show_points DESC", "comments": "r.show_comments DESC",
                     "karma": "coalesce(r.show_karma, 0) DESC"}.get(sort, order)
        elif kind in ("launch", "ask", "tell") and sort in ("points", "comments"):
            # small sets: rank by the matching post itself
            order = (f"(SELECT max({sort}) FROM stories s WHERE lower(s.repo) = r.key AND s.title LIKE ?) DESC")
            order_args = [KINDS[kind] + "%"]
        total = con.execute(f"SELECT count(*) FROM {repo_from} {w}", match_args + args).fetchone()[0]
        rows = con.execute(f"""SELECT r.* FROM {repo_from} {w} ORDER BY {order}, r.key
                LIMIT ? OFFSET ?""", match_args + args + order_args + [PAGE_SIZE, page * PAGE_SIZE]).fetchall()
        if kind in ("launch", "ask", "tell"):  # show the matching post, not the repo's overall top post
            out = []
            for x in rows:
                m = con.execute("""SELECT s.id, s.title, s.points, s.comments, s.author, coalesce(u.karma, 0) AS karma
                        FROM stories s LEFT JOIN users u ON u.name = s.author
                        WHERE lower(s.repo) = ? AND s.title LIKE ? ORDER BY s.points DESC LIMIT 1""",
                                (x["key"], KINDS[kind] + "%")).fetchone()
                out.append({**dict(x), "top_id": m["id"], "top_title": m["title"], "points": m["points"],
                            "comments": m["comments"], "top_author": m["author"], "karma": m["karma"]} if m else dict(x))
            rows = out
        if show:  # show the Show HN post instead of the repo's overall top post
            rows = [{**dict(x), "top_id": x["show_id"], "top_title": x["show_title"], "points": x["show_points"],
                     "comments": x["show_comments"], "top_author": x["show_author"], "karma": x["show_karma"]}
                    for x in rows]
    return {"view": view, "total": total, "page": page, "page_size": PAGE_SIZE,
            "results": [dict(r) for r in rows]}


def series(key):
    """Time series for a repo's charts: live points/comments and rank positions per post, and daily stars."""
    con = db()
    key = key.lower()
    ids = [r[0] for r in con.execute("SELECT id FROM stories WHERE lower(repo) = ?", (key,))]
    out = {"points": {}, "ranks": {}, "stars": []}
    if ids:
        qs = ",".join("?" * len(ids))
        for i, t, pts, com in con.execute(f"SELECT id, t, points, comments FROM points_log WHERE id IN ({qs}) ORDER BY t", ids):
            out["points"].setdefault(str(i), []).append([t, pts, com])
        for i, lst, t, pos in con.execute(f"SELECT id, list, t, pos FROM rank_log WHERE id IN ({qs}) ORDER BY t", ids):
            out["ranks"].setdefault(str(i), {}).setdefault(lst, []).append([t, pos])
    out["stars"] = [[d, n] for d, n in con.execute("SELECT day, stars FROM stars_log WHERE key = ? ORDER BY day", (key,))]
    return out


def suggest(q):
    """Type-ahead: repos whose owner/name or name starts with q, and HN users whose name starts with q."""
    q = q.strip().lower().lstrip("@")
    if len(q) < 2 or len(q) > 80:
        return {"repos": [], "users": []}
    hi = q + "\uffff"
    con = db()
    repos = con.execute("""SELECT repo, points, stars, avatar FROM (
            SELECT key, repo, points, stars, avatar FROM repos WHERE key >= ? AND key < ?
            UNION SELECT key, repo, points, stars, avatar FROM repos WHERE lower(name) >= ? AND lower(name) < ?)
        ORDER BY points DESC LIMIT 6""", (q, hi, q, hi)).fetchall()
    users = con.execute("""SELECT name, karma FROM users WHERE lower(name) >= ? AND lower(name) < ? AND karma > 0
        ORDER BY karma DESC LIMIT 3""", (q, hi)).fetchall() if "/" not in q else []
    return {"repos": [dict(r) for r in repos], "users": [dict(u) for u in users]}


TRENDING = {"day": 86400, "week": 7 * 86400, "month": 30 * 86400}


def trending(period="day", show=False, limit=30):
    """Top GitHub repos by their best HN post in the last day / week / month, with live rank info."""
    since = int(time.time()) - TRENDING.get(period, 86400)
    rows = db().execute(f"""SELECT s.id AS sid, s.title AS stitle, s.points AS spoints, s.comments AS scomments,
            s.author AS sauthor, s.time AS stime, coalesce(u.karma, 0) AS skarma,
            f.best_pos AS front_best, sh.best_pos AS show_best, r.*
            FROM stories s JOIN repos r ON r.key = lower(s.repo)
            LEFT JOIN users u ON u.name = s.author
            LEFT JOIN ranks f ON f.id = s.id AND f.list = 'front'
            LEFT JOIN ranks sh ON sh.id = s.id AND sh.list = 'show'
            WHERE s.time > ? {"AND s.title LIKE 'Show HN%'" if show else ""}
            ORDER BY s.points DESC LIMIT 400""", (since,)).fetchall()
    out, seen = [], set()
    for x in rows:
        if x["key"] in seen:
            continue
        seen.add(x["key"])
        d = dict(x)
        d.update(top_id=x["sid"], top_title=x["stitle"], points=x["spoints"], comments=x["scomments"],
                 top_author=x["sauthor"], karma=x["skarma"], post_time=x["stime"])
        for k in ("sid", "stitle", "spoints", "scomments", "sauthor", "stime", "skarma"):
            d.pop(k)
        out.append(d)
        if len(out) >= limit:
            break
    return {"period": period, "show": show, "results": out}


def repo_detail(key):
    con = db()
    r = con.execute("SELECT * FROM repos WHERE key = ?", (key.lower(),)).fetchone()
    if not r:
        return None
    posts = con.execute("""SELECT s.id, s.title, s.url, s.author, s.points, s.comments, s.time,
            coalesce(u.karma, 0) AS karma,
            f.best_pos AS front_best, f.best_at AS front_best_at, f.minutes_top30 AS front_minutes,
            sh.best_pos AS show_best, sh.best_at AS show_best_at, sh.minutes_top30 AS show_minutes
            FROM stories s LEFT JOIN users u ON u.name = s.author
            LEFT JOIN ranks f ON f.id = s.id AND f.list = 'front'
            LEFT JOIN ranks sh ON sh.id = s.id AND sh.list = 'show'
            WHERE lower(s.repo) = ? ORDER BY s.time DESC""", (key.lower(),)).fetchall()
    return {"repo": dict(r), "posts": [dict(x) for x in posts], "rankings": repo_rankings(con, r)}


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
        return sitemap_urls([(f"{SITE}/", day(time.time())), (f"{SITE}/trending", day(time.time())), (f"{SITE}/show-hn", day(time.time())), (f"{SITE}/badges", None), (f"{SITE}/about", None)])
    m = re.fullmatch(r"repos-(\d+)\.xml", name)
    if not m:
        return None
    rows = db().execute("SELECT repo, last_time FROM repos ORDER BY key LIMIT ? OFFSET ?",
                        (SITEMAP_CHUNK, int(m.group(1)) * SITEMAP_CHUNK)).fetchall()
    if not rows:
        return None
    return sitemap_urls([(f"{SITE}/r/{r['repo']}", day(r["last_time"])) for r in rows])


_index_html = [None]


def _repo_items(rows, title_key="top_title", id_key="top_id", pts_key="points", com_key="comments"):
    """Plain HTML list of repos, linked to their HackerGit pages (for crawlers and no-JS visitors)."""
    out = []
    for i, r in enumerate(rows, 1):
        out.append(f'<li class="item"><div class="rank">{i}.</div><div>'
                   f'<a class="repo mono" href="/r/{xml_esc(r["repo"])}">{xml_esc(r["repo"])}</a>'
                   f'<div class="title"><a href="https://news.ycombinator.com/item?id={r[id_key]}">{xml_esc(r[title_key])}</a></div>'
                   + (f'<div class="descr">{xml_esc(r["descr"])}</div>' if r["descr"] else "")
                   + f'<div class="meta"><span class="pts">{r[pts_key]:,} points</span><span>{r[com_key]:,} comments</span>'
                   + (f'<span>{r["stars"]:,} GitHub stars</span>' if r["stars"] is not None else "")
                   + '</div></div></li>')
    return '<ol class="list">' + "".join(out) + "</ol>"


BADGE_TEXT = """<p>Add a <b>Hacker News badge</b> to your GitHub README to show how your repo did on HN: points, comments
and how many times it was posted. It's free, updates by itself, and comes as a 250×55 card (the same size as
Trendshift and other README cards) or a small shields.io-style badge.</p>"""


def page_content(path):
    """(status, title, description, canonical, body_html, json_ld) for a page path."""
    con = db()
    st = hg.stats(con)
    repos_n, posts_n = f"{st['repos']:,}", f"{st['stories']:,}"
    site_ld = {"@context": "https://schema.org", "@type": "WebSite", "name": "HackerGit", "url": SITE + "/",
               "description": "Every GitHub repo shared on Hacker News, searchable by points, comments, stars and poster karma.",
               "potentialAction": {"@type": "SearchAction", "target": SITE + "/?q={search_term_string}",
                                   "query-input": "required name=search_term_string"}}
    if path in ("/", ""):
        rows = con.execute("SELECT * FROM repos ORDER BY points DESC LIMIT 30").fetchall()
        body = (f'<section class="hero"><h1>Every GitHub repo Hacker News has shared.</h1>'
                f'<p>{repos_n} GitHub repositories from {posts_n} Hacker News posts since 2008. Search them and sort by '
                f'points, comments, GitHub stars, or the karma of whoever posted them. Browse the '
                f'<a href="/show-hn">best Show HN projects</a> or get a <a href="/badges">Hacker News badge</a> for your README.</p></section>'
                f'<h2 class="sect">Top GitHub repos on Hacker News</h2>' + _repo_items(rows))
        return (200, "HackerGit: every GitHub repo shared on Hacker News",
                f"Search {repos_n} GitHub repos shared on Hacker News. Sort by points, comments, stars and poster karma, "
                "browse the best Show HN projects, and add a Hacker News badge to your README.",
                SITE + "/", body, site_ld)
    if path == "/show-hn":
        rows = con.execute("SELECT * FROM repos WHERE show_points IS NOT NULL ORDER BY show_points DESC LIMIT 50").fetchall()
        n = con.execute("SELECT count(*) FROM repos WHERE show_points IS NOT NULL").fetchone()[0]
        body = (f'<section class="hero"><h1>Best Show HN projects on GitHub</h1>'
                f'<p>{n:,} open source projects launched with a Show HN post, ranked by points. Filter by comments, '
                f'stars, language or the poster\'s karma to find hidden gems from first-time posters.</p></section>'
                + _repo_items(rows, "show_title", "show_id", "show_points", "show_comments"))
        return (200, "Best Show HN projects on GitHub - HackerGit",
                f"The best Show HN projects: {n:,} open source GitHub repos launched on Hacker News, ranked by points "
                "and comments. Find hidden gems from first-time posters.", SITE + "/show-hn", body, site_ld)
    if path == "/trending":
        rows = [dict(x) for x in trending("day")["results"]]
        body = ('<section class="hero"><h1>Trending on Hacker News: GitHub repos today</h1>'
                '<p>The GitHub repos getting the most attention on Hacker News in the last 24 hours, with live front page '
                'and Show HN positions. Switch to this week or this month.</p></section>' + _repo_items(rows))
        return (200, "Trending GitHub repos on Hacker News today - HackerGit",
                "GitHub repos trending on Hacker News today, this week and this month: points, comments, front page and "
                "Show HN rankings, updated every few minutes.", SITE + "/trending", body, site_ld)
    if path == "/badges":
        body = (f'<div class="page about"><h1>Hacker News badge for your GitHub README</h1>{BADGE_TEXT}'
                '<p><img src="/badge/karpathy/nanoGPT.svg?style=card" alt="Hacker News badge example" width="250" height="55"></p>'
                '<p><code>&lt;a href="https://hackergit.org/r/owner/repo"&gt;&lt;img src="https://hackergit.org/badge/owner/repo.svg?style=card" '
                'alt="Hacker News" width="250" height="55"&gt;&lt;/a&gt;</code></p>'
                '<p>Also works as Markdown, in dark mode, or linking straight to your HN post. Search for your repo above to get yours.</p></div>')
        return (200, "Hacker News badge for your GitHub README (Show HN badge) - HackerGit",
                "Free Hacker News badge for your GitHub README: show your HN points, comments and posts. Card or "
                "shields.io-style, HTML or Markdown, light or dark, updates automatically.", SITE + "/badges", body, None)
    if path == "/about":
        body = (f'<div class="page about"><h1>About HackerGit</h1><p>HackerGit collects every GitHub repository shared on '
                f'Hacker News ({repos_n} repos from {posts_n} posts) so you can search them and sort by points, comments, '
                'stars and poster karma. Data comes from the Algolia HN Search API, the official Hacker News API and the '
                'GitHub API, updated every 10 minutes. The code is open source on '
                '<a href="https://github.com/murtazayusuf/hackergit">GitHub</a>.</p></div>')
        return (200, "About HackerGit: GitHub repos on Hacker News",
                "How HackerGit collects every GitHub repo shared on Hacker News, where the data comes from, and how to "
                "contribute. Open source.", SITE + "/about", body, None)
    m = re.fullmatch(r"/r/([^/]+/[^/]+)/?", path)
    if m:
        r = con.execute("SELECT * FROM repos WHERE key = ?", (m.group(1).lower(),)).fetchone()
        if not r:
            return (404, f"{m.group(1)} - HackerGit", "This repo hasn't been shared on Hacker News yet.",
                    f"{SITE}/r/{m.group(1)}", f'<div class="page"><h1 class="mono">{xml_esc(m.group(1))}</h1>'
                    "<p>This repo hasn't been shared on Hacker News yet.</p></div>", None)
        posts = con.execute("SELECT id, title, points, comments, author, time FROM stories WHERE lower(repo) = ? "
                            "ORDER BY points DESC LIMIT 50", (r["key"],)).fetchall()
        url = f"{SITE}/r/{r['repo']}"
        desc = (f"{r['repo']} on Hacker News: {r['points']:,} points and {r['comments']:,} comments across {r['subs']} "
                f"post{'s' if r['subs'] != 1 else ''}. Top post: “{r['top_title']}”."
                + (f" {r['descr']}" if r["descr"] else ""))[:300]
        items = "".join(f'<li><a href="https://news.ycombinator.com/item?id={p["id"]}">{xml_esc(p["title"])}</a> '
                        f'({p["points"]:,} points, {p["comments"]:,} comments, by {xml_esc(p["author"])}, {day(p["time"])})</li>'
                        for p in posts)
        body = (f'<div class="page"><h1 class="mono">{xml_esc(r["repo"])}</h1>'
                + (f'<p class="descr">{xml_esc(r["descr"])}</p>' if r["descr"] else "")
                + f'<p>{r["points"]:,} best points · {r["comments"]:,} comments · {r["subs"]} Hacker News post{"s" if r["subs"] != 1 else ""}'
                + (f' · {r["stars"]:,} GitHub stars' if r["stars"] is not None else "")
                + f' · <a href="https://github.com/{xml_esc(r["repo"])}">View on GitHub</a></p>'
                f'<h2 class="sect">Hacker News discussions</h2><ul>{items}</ul></div>')
        ld = {"@context": "https://schema.org", "@type": "SoftwareSourceCode", "name": r["name"],
              "codeRepository": f"https://github.com/{r['repo']}", "url": url, "description": r["descr"] or r["top_title"],
              "author": {"@type": "Person", "name": r["owner"]}, "programmingLanguage": r["lang"] or None,
              "discussionUrl": f"https://news.ycombinator.com/item?id={r['top_id']}"}
        return (200, f"{r['repo']} on Hacker News ({r['points']:,} points) - HackerGit", desc, url, body,
                {k: v for k, v in ld.items() if v is not None})
    m = re.fullmatch(r"/u/([^/]+)/?", path)
    if m:
        name = m.group(1)
        return (200, f"GitHub repos posted to Hacker News by {name} - HackerGit",
                f"Every GitHub repo {name} has shared on Hacker News, with points, comments and stars.",
                f"{SITE}/u/{name}", f'<div class="page"><h1>{xml_esc(name)}</h1></div>', None)
    return (404, "Page not found - HackerGit", "This page doesn't exist.", SITE + "/",
            '<div class="page"><h1>Page not found</h1><p><a href="/">Back to HackerGit</a></p></div>', None)


_index_html = [None]


def page_html(path):
    """(status, html): the single-page app with real content, title, description and canonical for this page."""
    if _index_html[0] is None:
        with open(INDEX, encoding="utf-8") as fh:
            _index_html[0] = fh.read()
    status, title, desc, canonical, body, ld = cached("page:" + path, lambda: page_content(path))
    t, d, u = xml_esc(title), xml_esc(desc), xml_esc(canonical)
    html = _index_html[0]
    html = html.replace("<title>HackerGit</title>", f"<title>{t}</title>", 1)
    for pat, val in ((r'<meta name="description" content="[^"]*">', f'<meta name="description" content="{d}">'),
                     (r'<meta property="og:title" content="[^"]*">', f'<meta property="og:title" content="{t}">'),
                     (r'<meta property="og:description" content="[^"]*">', f'<meta property="og:description" content="{d}">'),
                     (r'<meta name="twitter:title" content="[^"]*">', f'<meta name="twitter:title" content="{t}">'),
                     (r'<meta name="twitter:description" content="[^"]*">', f'<meta name="twitter:description" content="{d}">'),
                     (r'<link rel="canonical" href="[^"]*">', f'<link rel="canonical" href="{u}">'),
                     (r'<meta property="og:url" content="[^"]*">', f'<meta property="og:url" content="{u}">')):
        html = re.sub(pat, lambda _m, v=val: v, html, count=1)
    head_extra = ('<meta name="robots" content="noindex">' if status == 404 else "")
    if ld:
        head_extra += '<script type="application/ld+json">' + json.dumps(ld).replace("</", "<\\/") + "</script>"
    html = html.replace("</head>", head_extra + "\n</head>", 1)
    html = html.replace('<main id="view"></main>', f'<main id="view">{body}</main>', 1)
    return status, html


# ---------------------------------------------------------------- README badges

# Approximate Verdana 11px glyph widths; textLength in the SVG makes the final fit exact.
_NARROW = {"i": 3.1, "l": 3.1, "j": 3.4, "f": 3.9, "t": 4.3, "r": 4.7, " ": 3.9, ",": 3.6, ".": 3.6,
           ":": 4.5, "'": 3.0, "(": 4.5, ")": 4.5, "I": 4.2, "-": 4.6}
_WIDE = {"m": 10.7, "w": 9.0, "M": 9.8, "W": 11.0}


def text_width(t):
    w = 0.0
    for c in t:
        if c in _NARROW:
            w += _NARROW[c]
        elif c in _WIDE:
            w += _WIDE[c]
        elif c.isdigit():
            w += 7.0
        elif c.isupper():
            w += 7.6
        else:
            w += 6.4
    return round(w, 1)


def badge_svg(label, value, color="#ff6600"):
    """A shields.io-style flat badge with a small HN logo."""
    lw_text, vw_text = text_width(label), text_width(value)
    logo = 14
    lw = round(6 + logo + 4 + lw_text + 6)
    vw = round(6 + vw_text + 6)
    w = lw + vw
    lx = (6 + logo + 4 + lw_text / 2) * 10
    vx = (lw + 6 + vw_text / 2) * 10
    l, v = xml_esc(label), xml_esc(value)
    return f"""<svg xmlns="http://www.w3.org/2000/svg" width="{w}" height="20" role="img" aria-label="{l}: {v}">
<title>{l}: {v}</title>
<linearGradient id="s" x2="0" y2="100%"><stop offset="0" stop-color="#bbb" stop-opacity=".1"/><stop offset="1" stop-opacity=".1"/></linearGradient>
<clipPath id="r"><rect width="{w}" height="20" rx="3" fill="#fff"/></clipPath>
<g clip-path="url(#r)"><rect width="{lw}" height="20" fill="#555"/><rect x="{lw}" width="{vw}" height="20" fill="{color}"/><rect width="{w}" height="20" fill="url(#s)"/></g>
<rect x="6" y="3" width="{logo}" height="{logo}" rx="2" fill="#ff6600" stroke="#fff" stroke-opacity=".35"/>
<text x="13" y="14" fill="#fff" font-family="Verdana,Geneva,DejaVu Sans,sans-serif" font-size="10" font-weight="bold" text-anchor="middle">Y</text>
<g fill="#fff" text-anchor="middle" font-family="Verdana,Geneva,DejaVu Sans,sans-serif" text-rendering="geometricPrecision" font-size="110">
<text aria-hidden="true" x="{lx:.0f}" y="150" fill="#010101" fill-opacity=".3" transform="scale(.1)" textLength="{lw_text * 10:.0f}">{l}</text>
<text x="{lx:.0f}" y="140" transform="scale(.1)" textLength="{lw_text * 10:.0f}">{l}</text>
<text aria-hidden="true" x="{vx:.0f}" y="150" fill="#010101" fill-opacity=".3" transform="scale(.1)" textLength="{vw_text * 10:.0f}">{v}</text>
<text x="{vx:.0f}" y="140" transform="scale(.1)" textLength="{vw_text * 10:.0f}">{v}</text>
</g></svg>
"""


def _w(t, size, bold=False):
    """Rough text width for sans-serif at a given size (kept generous so text never clips)."""
    base = text_width(t) / 11.0 * size
    return base * (1.12 if bold else 1.0)


_MONTHS = ["January", "February", "March", "April", "May", "June", "July", "August", "September", "October",
           "November", "December"]


def plural(n, w):
    return f"{n:,} {w}{'s' if n != 1 else ''}"


def _nice_date(d):
    y, m, dd = (d.split("-") + ["", ""])[:3]
    return f"{_MONTHS[int(m) - 1][:3]} {int(dd)}, {y}" if dd else f"{_MONTHS[int(m) - 1]} {y}" if m else y


def ach_label(r, comments=None):
    """('#1 Repo of the Day', 'Jan 11, 2023 · 1,532 points') for a repo's best achievement, or None."""
    if not r or r["ach_rank"] is None:
        return None
    rank, per, sc, dt, pts = r["ach_rank"], r["ach_period"], r["ach_scope"], r["ach_date"], r["ach_points"]
    noun = "Show HN" if sc == "show" else "Repo"
    when = {"day": _nice_date(dt), "week": f"Week of {_nice_date(dt)}", "month": _nice_date(dt),
            "year": "", "live": _nice_date(dt)}.get(per, "")
    small = f"{when} \u00b7 {pts:,} points" if when else f"{pts:,} points" + (f" \u00b7 {plural(comments, 'comment')}" if comments is not None else "")
    if per == "live":
        return (f"#{rank} on the Front Page" if sc == "front" else f"#{rank} on Show HN"), small
    if per == "year":
        return f"#{rank} {noun} of {dt}", small
    return f"#{rank} {noun} of the {per.title()}", small


RANK_PERIODS = ("day", "week", "month", "year", "alltime")


def post_rankings(con, post, scope="all"):
    """Where one HN post ranks by points among GitHub posts on HN (or among Show HN posts, scope='show')
    in its day / week / month / year, plus the repo's all-time rank. Returns {period: {...}}."""
    import datetime as dt
    t, pts = post["time"], post["points"]
    d = dt.datetime.fromtimestamp(t, dt.timezone.utc).date()
    to_ts = lambda x: int(dt.datetime(x.year, x.month, x.day, tzinfo=dt.timezone.utc).timestamp())
    monday = d - dt.timedelta(days=d.weekday())
    m0 = d.replace(day=1)
    m1 = (m0 + dt.timedelta(days=32)).replace(day=1)
    windows = {"day": (d, d + dt.timedelta(days=1)), "week": (monday, monday + dt.timedelta(days=7)),
               "month": (m0, m1), "year": (dt.date(d.year, 1, 1), dt.date(d.year + 1, 1, 1))}
    show_sql = " AND title LIKE 'Show HN%'" if scope == "show" else ""
    out = {}
    for per, (a, b) in windows.items():
        rank, total = con.execute(f"""SELECT sum(points > ?) + 1, count(*) FROM stories
            WHERE time >= ? AND time < ?{show_sql}""", (pts, to_ts(a), to_ts(b))).fetchone()
        out[per] = {"rank": rank, "total": total, "start": a.isoformat()}
    col = "show_points" if scope == "show" else "points"
    rank, total = con.execute(f"SELECT sum({col} > ?) + 1, count({col}) FROM repos", (pts,)).fetchone()
    out["alltime"] = {"rank": rank, "total": total, "start": ""}
    return out


def ranking_label(per, scope, info, pts, comments):
    """('#3 Repo of the Week', 'Week of Jan 9, 2023 · 474 points') for one ranking."""
    noun = "Show HN" if scope == "show" else "Repo"
    rank, start = info["rank"], info["start"]
    if per == "alltime":
        return f"#{rank:,} {noun} of All Time", f"{pts:,} points \u00b7 {plural(comments, 'comment')}"
    if per == "year":
        return f"#{rank:,} {noun} of {start[:4]}", f"{pts:,} points \u00b7 {plural(comments, 'comment')}"
    when = {"day": _nice_date(start), "week": f"Week of {_nice_date(start)}", "month": _nice_date(start[:7])}[per]
    return f"#{rank:,} {noun} of the {per.title()}", f"{when} · {pts:,} points"


def repo_rankings(con, r):
    """All rankings for a repo's top post, and for its best Show HN post if it has one."""
    out = []
    top = con.execute("SELECT id, time, points FROM stories WHERE id = ?", (r["top_id"],)).fetchone()
    if top:
        out.append({"scope": "all", "post_id": top["id"], "points": top["points"], "ranks": post_rankings(con, top)})
    if r["show_id"]:
        sp = con.execute("SELECT id, time, points FROM stories WHERE id = ?", (r["show_id"],)).fetchone()
        if sp:
            out.append({"scope": "show", "post_id": sp["id"], "points": sp["points"], "ranks": post_rankings(con, sp, "show")})
    return out


def badge_card(label, big, small, theme="light", muted=False):
    """A 250x55 card badge (the size Trendshift and similar README cards use); wider only if the text needs it."""
    dark = theme == "dark"
    bg, fg, sub = ("#0d1117", "#e6edf3", "#9198a1") if dark else ("#ffffff", "#1f2328", "#59636e")
    accent = "#9198a1" if muted else "#ff6600"
    tx, h, w = 57, 55, 250
    room = w - tx - 12
    # calibrated against browser rendering; shrink long text so the card stays 250 wide (like Trendshift's)
    big_fs = max(12.5, min(17.0, 17.0 * room / max(1, _w(big, 17, True) * 0.87)))
    small_fs = max(9.0, min(10.5, 10.5 * room / max(1, _w(small, 10.5) * 0.87)))
    L, B, S = xml_esc(label), xml_esc(big), xml_esc(small)
    font = "-apple-system,BlinkMacSystemFont,'Segoe UI','Noto Sans',Helvetica,Arial,sans-serif"
    icon_svg = ('<text x="28.5" y="35" fill="#fff" font-family="Verdana,Geneva,DejaVu Sans,sans-serif" '
                'font-size="21" font-weight="bold" text-anchor="middle">Y</text>')
    return f"""<svg xmlns="http://www.w3.org/2000/svg" width="{w}" height="{h}" viewBox="0 0 {w} {h}" role="img" aria-label="{L}: {B}, {S}">
<title>{L}: {B}, {S}</title>
<rect x="1" y="1" width="{w - 2}" height="{h - 2}" rx="9" fill="{bg}" stroke="{accent}" stroke-width="1.5"/>
<rect x="12" y="11" width="33" height="33" rx="7" fill="{accent}"/>
{icon_svg}
<g font-family="{font}">
<text x="{tx}" y="18.5" fill="{accent}" font-size="9.5" font-weight="600" letter-spacing=".6">{L.upper()}</text>
<text x="{tx}" y="35" fill="{fg}" font-size="{big_fs:.1f}" font-weight="700">{B}</text>
<text x="{tx}" y="47.5" fill="{sub}" font-size="{small_fs:.1f}">{S}</text>
</g></svg>
"""


def badge(path, query):
    """/badge/<owner>/<repo>.svg?show=points|comments|posts&label=..."""
    m = re.fullmatch(r"/badge/([A-Za-z0-9-]+)/([A-Za-z0-9._-]+?)(?:\.svg)?", path)
    if not m:
        return None
    p = parse_qs(query)
    show = (p.get("show") or ["points"])[0]
    label = ((p.get("label") or ["Hacker News"])[0] or "Hacker News")[:40]
    key = f"{m.group(1)}/{m.group(2)}".lower()
    r = db().execute("SELECT * FROM repos WHERE key = ?", (key,)).fetchone()
    style = (p.get("style") or [""])[0]
    if style == "rank":
        theme = (p.get("theme") or ["light"])[0]
        per = (p.get("rank") or ["best"])[0]
        scope = "show" if (p.get("scope") or [""])[0] == "show" else "all"
        if r and per in RANK_PERIODS:  # a specific ranking, e.g. ?rank=week or ?rank=alltime&scope=show
            pid = r["show_id"] if scope == "show" else r["top_id"]
            post = db().execute("SELECT id, time, points, comments FROM stories WHERE id = ?", (pid,)).fetchone() if pid else None
            if post:
                info = post_rankings(db(), post, scope)[per]
                big, small = ranking_label(per, scope, info, post["points"], post["comments"])
                return badge_card(label, big, small, theme)
        ach = db().execute("SELECT comments FROM stories WHERE id = ?", (r["ach_id"],)).fetchone() if r and r["ach_id"] else None
        lab = ach_label(r, ach["comments"] if ach else None)
        if lab:
            return badge_card(label, lab[0], lab[1], theme)
        style = "card"  # no ranking: fall back to the points card
    if style == "card":
        theme = (p.get("theme") or ["light"])[0]
        if not r:
            return badge_card(label, "Not posted yet", "No posts found", theme, muted=True)
        return badge_card(label, plural(r["points"], "point"),
                          f"{plural(r['comments'], 'comment')} \u00b7 {plural(r['subs'], 'post')}", theme)
    if not r:
        return badge_svg(label, "not posted yet", "#9f9f9f")
    if show == "comments":
        value = f"{r['comments']:,} comment{'s' if r['comments'] != 1 else ''}"
    elif show == "posts":
        value = f"{r['subs']:,} post{'s' if r['subs'] != 1 else ''}"
    else:
        value = f"{r['points']:,} point{'s' if r['points'] != 1 else ''}"
    return badge_svg(label, value)


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
    if path == "/api/suggest":
        return suggest((p.get("q") or [""])[0])
    if path == "/api/series":
        return series((p.get("key") or [""])[0])
    if path == "/api/trending":
        period = (p.get("period") or ["day"])[0]
        return trending(period if period in TRENDING else "day", (p.get("show") or ["0"])[0] == "1")
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
        try:
            self._get()
        finally:
            release_db()

    def _get(self):
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
            if u.path.startswith("/badge/"):
                body = cached("badge:" + u.path + "?" + u.query, lambda: badge(u.path, u.query) or "")
                if not body:
                    return self.send(404, "not found", "text/plain")
                return self.send(200, body, "image/svg+xml; charset=utf-8", "public, max-age=3600")
            if u.path in STATIC_FILES:
                fname, ctype = STATIC_FILES[u.path]
                with open(os.path.join(ROOT, "static", fname), "rb") as fh:
                    return self.send(200, fh.read(), ctype, "no-cache" if fname == "sw.js" else "public, max-age=86400")
            if u.path == "/healthz":
                return self.send(200, "ok", "text/plain")
            status, html = page_html(u.path)
            return self.send(status, html, "text/html; charset=utf-8", "public, max-age=300")
        except KeyError:
            return self.send(404, '{"error":"not found"}', "application/json")
        except Exception:
            traceback.print_exc()
            return self.send(500, '{"error":"server error"}', "application/json")


def background_process(minutes, rank_minutes):
    """Run all background work (HN updates, karma, stars, READMEs, rankings, rank tracking) in a separate,
    low-priority process, so it never competes with page requests for Python's interpreter lock.
    Restarted automatically if it exits."""
    cmd = [sys.executable, os.path.join(ROOT, "hackergit.py"), "daemon", str(minutes), str(rank_minutes)]
    while True:
        try:
            proc = subprocess.Popen(cmd, preexec_fn=lambda: os.nice(10))
            code = proc.wait()
            hg.log(f"background process exited ({code}); restarting in 30s")
        except Exception:
            traceback.print_exc()
        time.sleep(30)


def cache_warmer():
    """When the background process publishes a new index, pre-compute the pages most people open."""
    seen = None
    while True:
        try:
            v = data_version(force=True)
            if v != seen:
                if seen is not None:
                    warm_cache()
                seen = v
        except Exception:
            traceback.print_exc()
        finally:
            release_db()
        time.sleep(20)


def main():
    port = int(os.environ.get("PORT", "8000"))
    minutes = float(os.environ.get("UPDATE_MINUTES", "10"))
    hg.connect().close()  # make sure the schema exists before readers open it
    rank_minutes = float(os.environ.get("RANK_MINUTES", "2"))
    if minutes > 0 or rank_minutes > 0:
        threading.Thread(target=background_process, args=(minutes, rank_minutes), daemon=True).start()
    threading.Thread(target=cache_warmer, daemon=True).start()
    host = os.environ.get("HOST", "0.0.0.0")
    ThreadingHTTPServer.request_queue_size = 1024  # default is 5: bursts got delayed ~1 s by connection retries
    srv = ThreadingHTTPServer((host, port), Handler)
    srv.daemon_threads = True
    hg.log(f"HackerGit on http://{host}:{port}  db={hg.DB_PATH}")
    srv.serve_forever()


if __name__ == "__main__":
    main()
