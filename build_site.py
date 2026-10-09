#!/usr/bin/env python3
"""Veille entrepreneuriat : récupère les sources, met à jour l'archive, génère site/index.html.

Bibliothèque standard uniquement. Lancé chaque matin par GitHub Actions
(.github/workflows/update.yml) ; peut aussi être lancé à la main :

    python build_site.py              # récupère les sources puis régénère le site
    python build_site.py --no-fetch   # régénère le site depuis l'archive, sans réseau
"""
from __future__ import annotations

import argparse
import email.utils
import html
import json
import logging
import os
import re
import socket
import sys
import time
import unicodedata
import urllib.error
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from datetime import date, datetime, time as dtime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent
CONFIG_FILE = ROOT / "sources.json"
TEMPLATE_FILE = ROOT / "template.html"
ARCHIVE_FILE = ROOT / "data" / "archive.json"
STATUS_FILE = ROOT / "data" / "status.json"
SITE_FILE = ROOT / "site" / "index.html"
LOG_FILE = ROOT / "logs" / "update.log"
ALERT_BODY = ROOT / "data" / "alert.md"             # lus par le workflow pour ouvrir une issue GitHub
ALERT_TITLE = ROOT / "data" / "alert_title.txt"

USER_AGENT = "VeilleEntrepreneuriat/1.0 (lecteur personnel de flux)"
MAX_BYTES = 8_000_000
SUMMARY_LEN = 240

JOURS = ["lundi", "mardi", "mercredi", "jeudi", "vendredi", "samedi", "dimanche"]
MOIS = ["janvier", "février", "mars", "avril", "mai", "juin", "juillet", "août",
        "septembre", "octobre", "novembre", "décembre"]
MOIS_ABR = ["janv.", "févr.", "mars", "avr.", "mai", "juin", "juil.", "août",
            "sept.", "oct.", "nov.", "déc."]

# Pour les sources généralistes (« filter »: true) : on ne garde que ce qui parle d'entrepreneuriat.
KEYWORDS = re.compile(
    r"\b(entrepreneu\w*|start-?ups?|scale-?ups?|pme|tpe|eti|fondateur\w*|fondatrice\w*|"
    r"levees? de fonds|incubat\w+|accelerat\w+|licorne\w*|french tech|business model|"
    r"capital-risque|capital risque|venture|auto-entrepreneur\w*|freelance\w*|"
    r"travailleu\w+ autonomes?|releve|repreneur\w*|reprise d.entreprise|creation d.entreprise|"
    r"creer son entreprise|creation de start|innovation|business angels?|crowdfunding|"
    r"financement participatif|pitch|cofondat\w+|co-fondat\w+|dirigeants? de pme|"
    r"patron\w* de pme|entreprises? familiales?|transfert d.entreprise)\b"
)

log = logging.getLogger("veille")


# --------------------------------------------------------------------------- utilitaires

def norm(s: str) -> str:
    """Minuscules sans accents, pour comparer et chercher."""
    s = unicodedata.normalize("NFD", s or "")
    return "".join(c for c in s if not unicodedata.combining(c)).lower()


def title_key(title: str) -> str:
    return re.sub(r"[^a-z0-9]+", " ", norm(title)).strip()


def url_key(url: str) -> str:
    p = urllib.parse.urlsplit(url.strip())
    query = [(k, v) for k, v in urllib.parse.parse_qsl(p.query, keep_blank_values=True)
             if not k.lower().startswith("utm_") and k.lower() not in ("fbclid", "gclid", "at_medium", "at_campaign")]
    path = p.path.rstrip("/") or "/"
    return urllib.parse.urlunsplit((p.scheme.lower(), p.netloc.lower(), path, urllib.parse.urlencode(query), ""))


def safe_url(url: str) -> str:
    """N'autorise que http(s) : un flux ne doit jamais produire un lien javascript:."""
    url = (url or "").strip()
    return url if urllib.parse.urlsplit(url).scheme in ("http", "https") else ""


_SCRIPT_RE = re.compile(r"<(script|style)\b.*?</\1>", re.S | re.I)
_TAG_RE = re.compile(r"<[^>]+>")
_BOILERPLATE_RE = re.compile(
    r"\s*(?:The post .{0,300}? appeared first on .{0,100}|"
    r"(?:Cet article|L.article) .{0,300}? (?:est apparu en premier sur|est paru en premier sur) .{0,100}|"
    r"Lire la suite.*|Continue reading.*|Read more.*)$", re.I)


def clean_text(raw: str, limit: int | None = None) -> str:
    s = _SCRIPT_RE.sub(" ", raw or "")
    s = html.unescape(_TAG_RE.sub(" ", s))
    s = _TAG_RE.sub(" ", html.unescape(s))          # contenu double-échappé
    s = re.sub(r"\s+", " ", s).strip()
    s = _BOILERPLATE_RE.sub("", s)
    s = re.sub(r"\s*(\[…\]|\[\.\.\.\]|…|\.\.\.)$", "", s).strip()
    if limit and len(s) > limit:
        s = s[:limit].rsplit(" ", 1)[0].rstrip(" ,;:.-–—") + "…"
    return s


def parse_dt(value: str | None) -> datetime | None:
    if not value:
        return None
    value = value.strip()
    dt = None
    try:
        dt = email.utils.parsedate_to_datetime(value)
    except (TypeError, ValueError, IndexError):
        pass
    if dt is None:
        try:
            dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError:
            return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def http_get(url: str, accept: str = "*/*", timeout: int = 25, retries: int = 2) -> tuple[bytes, str]:
    last: Exception | None = None
    for attempt in range(retries + 1):
        req = urllib.request.Request(url, headers={
            "User-Agent": USER_AGENT, "Accept": accept, "Accept-Language": "fr,en;q=0.7"})
        try:
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                raw = resp.read(MAX_BYTES + 1)
                if len(raw) > MAX_BYTES:
                    raise ValueError("réponse trop volumineuse")
                return raw, resp.headers.get_content_charset() or "utf-8"
        except urllib.error.HTTPError as e:
            last = e
            if e.code in (401, 403, 404, 410):      # un blocage explicite ne se contourne pas
                break
        except Exception as e:                       # réseau, délai dépassé…
            last = e
        time.sleep(2 * (attempt + 1))
    assert last is not None
    raise last


def decode(raw: bytes, charset: str) -> str:
    try:
        return raw.decode(charset, errors="replace")
    except LookupError:
        return raw.decode("utf-8", errors="replace")


# --------------------------------------------------------------------------- récupération

def _local(tag) -> str:
    return tag.rsplit("}", 1)[-1] if isinstance(tag, str) else ""


def _kids(el, *names):
    return [c for c in el if _local(c.tag) in names]


def _text(el, *names) -> str:
    for c in _kids(el, *names):
        t = "".join(c.itertext()).strip()
        if t:
            return t
    return ""


def fetch_rss(src: dict, _archive: dict) -> list[dict]:
    raw, _ = http_get(src["url"], accept="application/rss+xml, application/atom+xml, text/xml, */*")
    root = ET.fromstring(raw)
    entries = [e for e in root.iter() if _local(e.tag) in ("item", "entry")]
    out = []
    for e in entries:
        title = clean_text(_text(e, "title"))
        link = ""
        for l in _kids(e, "link"):
            link = l.get("href") or (l.text or "")
            if link and l.get("rel") in (None, "alternate"):
                break
        link = safe_url(link or _text(e, "guid"))
        if not title or not link:
            continue
        published = parse_dt(_text(e, "pubDate", "published", "date", "updated"))
        publisher = _text(e, "source")
        if publisher and title.endswith(" - " + publisher):        # Google Actualités
            title = title[: -len(publisher) - 3].rstrip()
        summary = ""
        if not src.get("aggregator"):
            summary = clean_text(_text(e, "description", "summary") or _text(e, "encoded", "content"), SUMMARY_LEN)
        authors = _text(e, "creator") or " ".join(_text(a, "name") for a in _kids(e, "author")).strip()
        if "@" in authors:                                          # <author> RSS = e-mail
            authors = ""
        tags = []
        if src["kind"] == "revue":                    # catégories utiles pour les revues seulement (bruit côté presse)
            for c in _kids(e, "category"):
                t = clean_text(c.get("term") or "".join(c.itertext()))
                if t and not re.match(r"(?i)(uncategorized|non classifi|sans cat|blog$)", norm(t)) and t not in tags:
                    tags.append(t)
        if src.get("show_authors") is False:         # certains sites de revue n'indiquent que la personne qui poste l'article
            authors = ""
        out.append({
            "title": title, "url": link, "summary": summary,
            "authors": clean_text(authors), "tags": tags[:3],
            "name": publisher or src["name"],
            "published": published.isoformat() if published else None, "date_only": False,
        })
    return out


def _issue_label(volume: str | None, issue: str | None) -> str:
    parts = []
    if volume:
        parts.append(volume if volume.lower().startswith("vol") else f"Vol. {volume}")
    if issue:
        m = re.match(r"(?i)hs\s*(\d*)", issue)
        parts.append(f"Hors-série {m.group(1)}".strip() if m else f"n° {issue}")
    return " · ".join(parts)


def fetch_crossref(src: dict, _archive: dict) -> list[dict]:
    """Revue de l'Entrepreneuriat (Cairn) : Cairn bloque les robots, on passe par l'API publique Crossref."""
    params = urllib.parse.urlencode({
        "filter": f"issn:{src['issn']}", "rows": src.get("max_items", 25),
        "sort": "published", "order": "desc",
        "select": "title,DOI,URL,author,published,volume,issue,abstract,type"})
    raw, charset = http_get("https://api.crossref.org/works?" + params, accept="application/json")
    items = json.loads(decode(raw, charset))["message"]["items"]
    out = []
    for w in items:
        title = clean_text((w.get("title") or [""])[0])
        doi = w.get("DOI")
        if not doi:
            continue
        issue = _issue_label(w.get("volume"), w.get("issue"))
        if norm(title) in ("editorial", ""):
            title = f"Éditorial — {issue}" if issue else "Éditorial"
        parts = (w.get("published", {}).get("date-parts") or [[None]])[0]
        published = None
        if parts and parts[0]:
            published = date(parts[0], parts[1] if len(parts) > 1 else 1, parts[2] if len(parts) > 2 else 1).isoformat()
        authors = [" ".join(filter(None, (a.get("given"), a.get("family")))) for a in w.get("author", [])]
        authors_txt = ", ".join(authors[:3]) + (" et al." if len(authors) > 3 else "")
        abstract = re.sub(r"^(abstract|résumé|resume)\s*[:.]?\s*", "", clean_text(w.get("abstract", "")), flags=re.I)
        out.append({
            "title": title, "url": safe_url(w.get("URL") or f"https://doi.org/{doi}"),
            "summary": clean_text(abstract, SUMMARY_LEN), "authors": authors_txt,
            "tags": [issue] if issue else [], "name": src["name"],
            "published": published, "date_only": True,
        })
    return out


def fetch_revuegestion(src: dict, archive: dict) -> list[dict]:
    """Revue Gestion (Magento, sans flux RSS) : on lit la page de catégorie, puis la date de chaque fiche (mise en cache)."""
    known = archive["items"]
    budget = src.get("max_page_fetches", 20)
    out, seen = [], set()
    for page in src["pages"]:
        raw, charset = http_get(page, accept="text/html")
        cards = re.split(r'<li class="item product product-item">', decode(raw, charset))[1:]
        for c in cards:
            m = re.search(r'<a class="product-item-link([^"]*)"\s+href="([^"]+)"[^>]*>\s*(.*?)\s*</a>', c, re.S)
            if not m:
                continue
            url, title = safe_url(html.unescape(m.group(2))), clean_text(m.group(3))
            if not url or not title or url in seen:
                continue
            seen.add(url)
            desc =re.search(r'product-item-description">\s*(.*?)\s*</div>', c, re.S)
            authors = re.search(r'<div class="authors">\s*Par\s*(.*?)\s*</div>', c, re.S)
            tags = [clean_text(t) for t in re.findall(r'class="product-category">([^<]*)', c)]
            tags = [t for t in tags if t and norm(t) != "entrepreneuriat"][:2]
            if "lock-item" in m.group(1):
                tags.append("Abonnés")
            existing = known.get(url_key(url))
            published = existing.get("published") if existing else None
            if not published and budget > 0:
                budget -= 1
                time.sleep(src.get("delay", 2.5))
                try:
                    art, art_cs = http_get(url, accept="text/html", retries=1)
                    d = re.search(r'article:published_time"\s+content="(\d{4}-\d{2}-\d{2})', decode(art, art_cs))
                    published = d.group(1) if d else None
                except Exception as e:                      # une fiche en échec n'invalide pas la page
                    log.warning("  date introuvable pour %s (%s)", url, e)
            out.append({
                "title": title, "url": url, "summary": clean_text(desc.group(1), SUMMARY_LEN) if desc else "",
                "authors": clean_text(authors.group(1)) if authors else "", "tags": tags,
                "name": src["name"], "published": published, "date_only": True,
            })
    return out


FETCHERS = {"rss": fetch_rss, "crossref": fetch_crossref, "revuegestion": fetch_revuegestion}


# --------------------------------------------------------------------------- archive

def load_archive() -> dict:
    try:
        data = json.loads(ARCHIVE_FILE.read_text(encoding="utf-8"))
        if isinstance(data.get("items"), dict):
            return data
    except (OSError, ValueError):
        pass
    return {"items": {}}


def write_atomic(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(text, encoding="utf-8")
    os.replace(tmp, path)


def local_day(published: str | None, date_only: bool) -> date | None:
    if not published:
        return None
    if date_only:
        try:
            return date.fromisoformat(published[:10])
        except ValueError:
            return None
    dt = parse_dt(published)
    return dt.astimezone().date() if dt else None


def merge(archive: dict, src: dict, fetched: list[dict], today: date, seed: bool, titles: dict, oldest: date,
          alerts_on: bool = False) -> int:
    items, added = archive["items"], 0
    now_iso = datetime.now(timezone.utc).isoformat()
    # on filtre d'abord (mots-clés, ancienneté), puis on garde les plus récents
    if src.get("filter"):
        fetched = [f for f in fetched if KEYWORDS.search(norm(f["title"] + " " + f.get("summary", "")))]
    fetched = [f for f in fetched if (local_day(f.get("published"), f["date_only"]) or today) >= oldest]
    fetched.sort(key=lambda f: f.get("published") or "", reverse=True)
    for f in fetched[: src.get("max_items", 30)]:
        key = url_key(f["url"])
        existing = items.get(key)
        if existing:
            for k in ("title", "summary", "authors", "tags"):
                if f.get(k):
                    existing[k] = f[k]
            if src.get("show_authors") is False:
                existing["authors"] = ""
            if f.get("published") and not existing.get("published"):
                existing["published"], existing["date_only"] = f["published"], f["date_only"]
            continue
        tk = title_key(f["title"])
        if src["kind"] == "news" and len(tk) >= 20 and tk in titles:
            continue                                        # même article déjà vu via une autre source
        if seed:        # premier lancement : on date la « découverte » à la parution, sinon tout serait « nouveau »
            first_seen = (local_day(f.get("published"), f["date_only"]) or today).isoformat()
        else:
            first_seen = today.isoformat()
        items[key] = {**f, "key": key, "source": src["id"], "kind": src["kind"],
                      "lang": src.get("lang", "fr"), "first_seen": first_seen, "added": now_iso}
        if src["kind"] == "news" and len(tk) >= 20:
            titles[tk] = key
        if alerts_on and src["kind"] == "revue" and not seed:
            items[key]["alert"] = "pending"             # « à envoyer » : reste en attente tant que l'issue n'est pas créée
        added += 1
    return added


def prune(archive: dict, cfg: dict, today: date) -> int:
    s = cfg["settings"]
    limits = {"news": today - timedelta(days=s["keep_days_news"]), "revue": today - timedelta(days=s["keep_days_revues"])}
    known = {src["id"] for src in cfg["sources"]}
    drop = [k for k, it in archive["items"].items()
            if it["source"] not in known                      # source retirée de sources.json
            or (eff_day(it, today) or today) < limits.get(it["kind"], limits["news"])]
    for k in drop:
        del archive["items"][k]
    return len(drop)


# --------------------------------------------------------------------------- calcul des fenêtres

def pub_day(it: dict) -> date | None:
    return local_day(it.get("published"), it.get("date_only", False))


def eff_day(it: dict, today: date) -> date | None:
    """Jour de rattachement : la parution pour la presse, la découverte pour les revues (qui publient rarement)."""
    first = date.fromisoformat(it["first_seen"]) if it.get("first_seen") else None
    day = first if it["kind"] == "revue" else (pub_day(it) or first)
    return min(day, today) if day else None


def sort_key(it: dict, today: date) -> datetime:
    if not it.get("date_only") and it.get("published"):
        dt = parse_dt(it["published"])
        if dt:
            return min(dt, datetime.now(timezone.utc))
    day = eff_day(it, today) or today
    return datetime.combine(day, dtime(0, 0)).astimezone()


# --------------------------------------------------------------------------- rendu HTML

esc = html.escape


def fmt_long(d: date) -> str:
    return f"{JOURS[d.weekday()]} {d.day}{'er' if d.day == 1 else ''} {MOIS[d.month - 1]}"


def fmt_short(d: date) -> str:
    return f"{d.day} {MOIS_ABR[d.month - 1]} {d.year}"


def item_html(it: dict, today: date, week_start: date, show_date: bool) -> str:
    pd = pub_day(it)
    meta = [f'<span class="src">{esc(it["name"])}</span>']
    if it["kind"] == "revue":
        if pd:
            meta.append(f"<span>Paru le {fmt_short(pd)}</span>")
    elif it.get("published") and not it.get("date_only"):
        dt = min(parse_dt(it["published"]), datetime.now(timezone.utc)).astimezone()
        label = dt.strftime("%H:%M")
        if show_date and dt.date() != today:            # onglet « Aujourd'hui » : les dernières 24 h couvrent hier soir
            label = ("hier " if dt.date() == today - timedelta(days=1) else f"{fmt_short(dt.date())} ") + label
        meta.append(f'<time datetime="{esc(dt.isoformat())}">{label}</time>')
    elif pd:
        meta.append(f"<span>{fmt_short(pd)}</span>")
    if it["kind"] == "revue":
        first = date.fromisoformat(it["first_seen"])
        if first >= week_start:
            meta.append('<span class="tag new">Nouveau</span>')
    if it.get("lang") == "en":
        meta.append('<span class="tag">EN</span>')
    for t in it.get("tags", []):
        cls = "tag lock" if t == "Abonnés" else "tag"
        meta.append(f'<span class="{cls}">{esc(t)}</span>')
    authors = f'<p class="by">{esc(it["authors"])}</p>' if it.get("authors") else ""
    summary = f'<p class="sum">{esc(it["summary"])}</p>' if it.get("summary") else ""
    text = norm(" ".join([it["title"], it.get("summary", ""), it["name"], it.get("authors", ""), " ".join(it.get("tags", []))]))
    lang = ' lang="en"' if it.get("lang") == "en" else ""
    return (
        f'<article class="item" data-source="{esc(it["source"])}" data-text="{esc(text)}">'
        f'<div class="meta">{"".join(meta)}</div>'
        f'<h3><a href="{esc(it["url"])}" target="_blank" rel="noopener noreferrer"{lang}>{esc(it["title"])}</a></h3>'
        f'{authors}{summary}</article>')


def day_heading(d: date, today: date) -> str:
    prefix = "Aujourd’hui · " if d == today else "Hier · " if d == today - timedelta(days=1) else ""
    return f"{prefix}{fmt_long(d)}"


def render(cfg: dict, archive: dict, status: dict, now: datetime) -> str:
    s = cfg["settings"]
    today = now.date()
    week_start = today - timedelta(days=s["week_days"] - 1)
    all_items = list(archive["items"].values())
    for it in all_items:
        it["_day"] = eff_day(it, today)
        it["_sort"] = sort_key(it, today)

    # Aujourd'hui : dernières 24 h (presse) ou jour courant (revues et dates sans heure)
    def is_today(it):
        if it["kind"] == "news" and not it.get("date_only") and it.get("published"):
            return now.astimezone(timezone.utc) - parse_dt(it["published"]) <= timedelta(hours=24)
        return it["_day"] == today

    in_week = sorted((i for i in all_items if i["_day"] and week_start <= i["_day"] <= today),
                     key=lambda i: i["_sort"], reverse=True)
    in_today = sorted((i for i in all_items if is_today(i)), key=lambda i: i["_sort"], reverse=True)

    # --- panneau « Aujourd'hui »
    if in_today:
        today_body = "".join(item_html(i, today, week_start, True) for i in in_today)
        today_empty = ""
    else:
        today_body = ""
        today_empty = '<p class="empty">Rien de nouveau ces dernières 24 heures. Consultez « Cette semaine ».</p>'

    # --- panneau « Cette semaine » (par jour)
    groups: dict[date, list[dict]] = {}
    for i in in_week:
        groups.setdefault(i["_day"], []).append(i)
    week_body = "".join(
        f'<section class="day" data-group><h2>{esc(day_heading(d, today))}</h2>'
        + "".join(item_html(i, today, week_start, False) for i in groups[d]) + "</section>"
        for d in sorted(groups, reverse=True))
    week_empty = '' if in_week else '<p class="empty">Aucun article cette semaine : les sources n’ont pas encore répondu.</p>'

    # --- panneau « Revues »
    revues = [src for src in cfg["sources"] if src["kind"] == "revue"]
    new_revue = sum(1 for i in in_week if i["kind"] == "revue")
    intro = (f"{new_revue} nouvelle{'s' if new_revue > 1 else ''} publication{'s' if new_revue > 1 else ''} cette semaine."
             if new_revue else "Aucune nouvelle publication cette semaine. Voici les dernières parutions de chaque revue.")
    sections = []
    for src in revues:
        mine = sorted((i for i in all_items if i["source"] == src["id"]), key=lambda i: i["_sort"], reverse=True)
        shown = mine[: s["revue_show"]]
        st = status.get(src["id"], {})
        notes = []
        latest = max((pub_day(i) for i in mine if pub_day(i)), default=None)
        if latest:
            notes.append(f"Dernière parution : {fmt_short(latest)}")
        if st.get("ok") is False:
            notes.append("source momentanément injoignable, dernières données connues")
        elif st.get("last_success"):
            notes.append("vérifiée " + ("aujourd’hui" if st["last_success"][:10] == today.isoformat()
                                         else "le " + fmt_short(date.fromisoformat(st["last_success"][:10]))))
        body = "".join(item_html(i, today, week_start, False) for i in shown) or \
            '<p class="empty">Aucune publication récupérée pour le moment.</p>'
        sections.append(
            f'<section class="journal" data-group><header><h2>{esc(src["name"])}</h2>'
            f'<a class="ext" href="{esc(src["homepage"])}" target="_blank" rel="noopener noreferrer">Site de la revue ↗</a></header>'
            f'<p class="note">{esc(" · ".join(notes))}</p>{body}</section>')
    revues_body = f'<p class="lede">{esc(intro)}</p>' + "".join(sections)

    # --- barre de contrôle
    default_tab = "aujourdhui" if in_today else "semaine"
    opts_revue = "".join(f'<option value="{esc(r["id"])}">{esc(r["name"])}</option>' for r in revues)
    opts_news = "".join(f'<option value="{esc(r["id"])}">{esc(r["name"])}</option>'
                        for r in cfg["sources"] if r["kind"] == "news")
    controls = f"""
<div class="bar"><div class="wrap bar-in">
  <div class="tabs" role="tablist" aria-label="Période">
    <button role="tab" id="t-aujourdhui" aria-controls="p-aujourdhui" data-tab="aujourdhui">Aujourd’hui <span class="n">{len(in_today)}</span></button>
    <button role="tab" id="t-semaine" aria-controls="p-semaine" data-tab="semaine">Cette semaine <span class="n">{len(in_week)}</span></button>
    <button role="tab" id="t-revues" aria-controls="p-revues" data-tab="revues">Revues{f' <span class="n" title="nouvelles publications cette semaine">{new_revue}</span>' if new_revue else ''}</button>
  </div>
  <div class="filters">
    <label class="sr" for="q">Rechercher</label>
    <input id="q" type="search" placeholder="Rechercher…" autocomplete="off">
    <label class="sr" for="src">Source</label>
    <select id="src"><option value="">Toutes les sources</option>
      <optgroup label="Revues">{opts_revue}</optgroup><optgroup label="Presse">{opts_news}</optgroup></select>
  </div>
</div></div>"""

    panels = (
        f'<section class="panel" role="tabpanel" id="p-aujourdhui" aria-labelledby="t-aujourdhui" data-panel="aujourdhui">'
        f'<h2 class="ph">Aujourd’hui <small>dernières 24 heures</small></h2>{today_body}{today_empty}<p class="nomatch" hidden>Aucun résultat avec ces filtres.</p></section>'
        f'<section class="panel" role="tabpanel" id="p-semaine" aria-labelledby="t-semaine" data-panel="semaine">'
        f'<h2 class="ph">Cette semaine <small>7 derniers jours</small></h2>{week_body}{week_empty}<p class="nomatch" hidden>Aucun résultat avec ces filtres.</p></section>'
        f'<section class="panel" role="tabpanel" id="p-revues" aria-labelledby="t-revues" data-panel="revues">'
        f'<h2 class="ph">Revues <small>suivies chaque semaine</small></h2>{revues_body}<p class="nomatch" hidden>Aucun résultat avec ces filtres.</p></section>')

    # --- en-tête et pied de page
    errors = [src["name"] for src in cfg["sources"] if status.get(src["id"], {}).get("ok") is False]
    mast = (f'<header class="mast"><div class="wrap"><p class="kicker">Veille professionnelle</p>'
            f'<h1>{esc(s["site_title"])}</h1>'
            f'<p class="dateline">{esc(JOURS[today.weekday()].capitalize())} {fmt_long(today).split(" ", 1)[1]} {today.year}'
            f' · mis à jour à {now.strftime("%H:%M")} · prochaine mise à jour {esc(s["schedule_label"])}</p></div></header>')
    rows = []
    for src in cfg["sources"]:
        st = status.get(src["id"], {})
        ok = st.get("ok")
        state = "—" if ok is None else "OK" if ok else "Erreur"
        detail = "" if ok is None else \
            f'{st.get("fetched", 0)} récupérés, {st.get("added", 0)} nouveaux' if ok else esc(st.get("error", ""))
        rows.append(f'<tr class="{"bad" if ok is False else ""}"><td>{esc(src["name"])}</td><td>{state}</td><td>{detail}</td></tr>')
    warn = f' · <strong>{len(errors)} source(s) en erreur</strong>' if errors else ""
    footer = (f'<footer class="foot"><div class="wrap"><details><summary>État des sources{warn}</summary>'
              f'<table><thead><tr><th>Source</th><th>État</th><th>Dernier passage</th></tr></thead><tbody>{"".join(rows)}</tbody></table>'
              f'<p>Les titres et résumés appartiennent à leurs éditeurs : chaque lien renvoie vers la source originale.</p>'
              f'</details></div></footer>')

    tpl = TEMPLATE_FILE.read_text(encoding="utf-8")
    return (tpl.replace("{{TITLE}}", esc(s["site_title"]))
               .replace("{{DEFAULT_TAB}}", default_tab)
               .replace("{{MASTHEAD}}", mast).replace("{{CONTROLS}}", controls)
               .replace("{{PANELS}}", panels).replace("{{FOOTER}}", footer))


# --------------------------------------------------------------------------- alertes (nouvelles parutions des revues)

def _md(text: str) -> str:
    return re.sub(r"([\[\]])", r"\\\1", text)


def write_alerts(cfg: dict, archive: dict, today: date, test: bool = False) -> int:
    """Prépare le texte d'une issue GitHub pour les parutions « pending » ; supprime les fichiers s'il n'y en a pas."""
    pending = [i for i in archive["items"].values() if i.get("alert") == "pending"]
    if not pending:
        ALERT_BODY.unlink(missing_ok=True)
        ALERT_TITLE.unlink(missing_ok=True)
        return 0
    order = {s["id"]: n for n, s in enumerate(cfg["sources"])}
    pending.sort(key=lambda i: i.get("published") or "", reverse=True)       # plus récent d'abord…
    pending.sort(key=lambda i: order.get(i["source"], 99))                  # …revue par revue (tri stable)
    owner, repo = os.environ.get("GITHUB_REPOSITORY_OWNER", ""), os.environ.get("GITHUB_REPOSITORY", "").split("/")[-1]
    page = f"https://{owner}.github.io/{repo}/#revues" if owner and repo else ""

    n = len(pending)
    lines = []
    if test:
        lines += ["**Ceci est un test d'alerte.** Aucune nouvelle parution n'a été détectée : la publication la plus récente "
                  "est reprise ci-dessous pour vérifier que cette notification vous parvient. Vous pouvez fermer cette issue.", ""]
    lines.append(f"{'@' + owner + ', ' if owner else ''}{n} nouvelle{'s' if n > 1 else ''} publication{'s' if n > 1 else ''} "
                 f"détectée{'s' if n > 1 else ''} dans vos revues (mise à jour du {fmt_short(today)}).")
    current = None
    for it in pending:
        if it["name"] != current:
            current = it["name"]
            lines += ["", f"### {_md(current)}"]
        pd = pub_day(it)
        tags = [t for t in it.get("tags", []) if t not in it["title"]]          # ex. « Vol. 25 · n° 2 » déjà dans le titre
        meta = " · ".join(filter(None, [it.get("authors"), f"paru le {fmt_short(pd)}" if pd else "", ", ".join(tags)]))
        lines.append(f"- [{_md(it['title'])}]({it['url']})" + (f" — {meta}" if meta else ""))
        if it.get("summary"):
            lines.append(f"  > {it['summary']}")
    if page:
        lines += ["", f"Voir aussi la page complète : {page}"]
    ALERT_BODY.parent.mkdir(parents=True, exist_ok=True)
    ALERT_BODY.write_text("\n".join(lines) + "\n", encoding="utf-8")

    if n == 1:
        title = f"{pending[0]['name']} : {pending[0]['title']}"
        title = (title[:117] + "…") if len(title) > 118 else title
        title = f"Nouvelle parution — {title}"
    else:
        title = f"{n} nouvelles parutions dans vos revues"
    ALERT_TITLE.write_text(("Test d'alerte — " + title if test else title) + "\n", encoding="utf-8")
    return n


def ack_alerts(archive: dict) -> int:
    """Appelé par le workflow APRÈS la création de l'issue : les alertes ne seront plus renvoyées."""
    n = 0
    for it in archive["items"].values():
        if it.get("alert") == "pending":
            it["alert"] = "sent"
            n += 1
    return n


# --------------------------------------------------------------------------- main

def wait_for_network(timeout: int = 90) -> bool:
    """Au réveil de la veille, le réseau met parfois quelques secondes à revenir."""
    deadline = time.monotonic() + timeout
    while True:
        try:
            socket.create_connection(("api.crossref.org", 443), timeout=5).close()
            return True
        except OSError:
            if time.monotonic() >= deadline:
                return False
            time.sleep(5)


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--no-fetch", action="store_true", help="ne contacte aucune source, régénère depuis l'archive")
    ap.add_argument("--if-stale", type=float, metavar="HEURES",
                    help="ne rien faire si la dernière mise à jour réussie date de moins de HEURES heures "
                         "(utilisé par la tâche planifiée, déclenchée à plusieurs moments : 7 h 30, ouverture de session, réveil)")
    ap.add_argument("--ack-alerts", action="store_true",
                    help="marque les alertes en attente comme envoyées (appelé par le workflow après la création de l'issue)")
    ap.add_argument("--test-alert", action="store_true",
                    help="met en attente la publication de revue la plus récente pour tester la chaîne d'alerte")
    args = ap.parse_args()

    LOG_FILE.parent.mkdir(parents=True, exist_ok=True)
    handlers: list[logging.Handler] = [logging.FileHandler(LOG_FILE, encoding="utf-8")]
    if sys.stdout is not None:                       # pythonw.exe n'a pas de console
        handlers.append(logging.StreamHandler(sys.stdout))
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s", handlers=handlers)
    if sys.stdout is not None and hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")

    cfg = json.loads(CONFIG_FILE.read_text(encoding="utf-8"))
    archive = load_archive()
    try:
        status = json.loads(STATUS_FILE.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        status = {}
    now = datetime.now().astimezone()
    today = now.date()
    if args.ack_alerts:
        n = ack_alerts(archive)
        write_atomic(ARCHIVE_FILE, json.dumps(archive, ensure_ascii=False, indent=1))
        ALERT_BODY.unlink(missing_ok=True)
        ALERT_TITLE.unlink(missing_ok=True)
        log.info("%d alerte(s) marquée(s) comme envoyées", n)
        return 0
    alerts_on = bool(os.environ.get("GITHUB_ACTIONS"))     # les alertes n'ont de sens que dans le cloud (issues GitHub)
    meta = status.setdefault("_meta", {})
    if args.if_stale and not args.no_fetch and meta.get("last_run"):
        age = now - datetime.fromisoformat(meta["last_run"])
        if age < timedelta(hours=args.if_stale):
            log.info("Déjà à jour (dernière mise à jour il y a %.1f h) : rien à faire", age.total_seconds() / 3600)
            return 0
    seed = not archive["items"]
    titles = {title_key(i["title"]): k for k, i in archive["items"].items()
              if i["kind"] == "news" and len(title_key(i["title"])) >= 20}
    if args.test_alert:
        revues = [i for i in archive["items"].values() if i["kind"] == "revue"]
        if revues:
            max(revues, key=lambda i: sort_key(i, today))["alert"] = "pending"

    if not args.no_fetch:
        log.info("Mise à jour : %d sources%s", len(cfg["sources"]), " (premier lancement)" if seed else "")
        if not wait_for_network():
            log.warning("  réseau indisponible après 90 s, on tente quand même")
        for src in cfg["sources"]:
            st = status.setdefault(src["id"], {})
            try:
                fetched = FETCHERS[src["type"]](src, archive)
                keep = cfg["settings"]["keep_days_revues" if src["kind"] == "revue" else "keep_days_news"]
                added = merge(archive, src, fetched, today, seed, titles, today - timedelta(days=keep), alerts_on)
                st.update(ok=True, fetched=len(fetched), added=added, error="", last_success=now.isoformat())
                log.info("  OK  %-34s %3d récupérés, %3d nouveaux", src["name"], len(fetched), added)
            except Exception as e:                   # une source en panne ne doit pas bloquer les autres
                st.update(ok=False, error=f"{type(e).__name__}: {e}"[:200])
                log.warning("  KO  %-34s %s", src["name"], st["error"])
        if any(status.get(s["id"], {}).get("ok") for s in cfg["sources"]):
            meta["last_run"] = now.isoformat()      # si tout a échoué (pas de réseau), le prochain déclencheur réessaiera
        dropped = prune(archive, cfg, today)
        if dropped:
            log.info("  %d anciens éléments retirés de l'archive", dropped)
        write_atomic(ARCHIVE_FILE, json.dumps(archive, ensure_ascii=False, indent=1))
        write_atomic(STATUS_FILE, json.dumps(status, ensure_ascii=False, indent=1))

    if alerts_on or args.test_alert:
        n_alerts = write_alerts(cfg, archive, today, test=args.test_alert)
        if n_alerts:
            log.info("%d parution(s) de revue à signaler (voir %s)", n_alerts, ALERT_BODY.name)
    write_atomic(SITE_FILE, render(cfg, archive, status, now))
    log.info("Site généré : %s (%d éléments en archive)", SITE_FILE, len(archive["items"]))
    return 0


if __name__ == "__main__":
    sys.exit(main())
