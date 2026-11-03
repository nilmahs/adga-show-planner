#!/usr/bin/env python3
from __future__ import annotations
import json, re, sys, time
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlencode
import requests
from bs4 import BeautifulSoup

ROOT=Path(__file__).resolve().parents[1]
DATA_JS=ROOT/"data"/"shows.js"
CACHE_JSON=ROOT/"data"/"geocode_cache.json"
SOURCE_URL="https://adga.org/events/"
UA="Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/151.0.0.0 Safari/537.36"
EMAIL_RE=re.compile(r"[\w.+-]+@[\w.-]+\.[A-Za-z]{2,}")
PHONE_RE=re.compile(r"(?:\+?1[\s.\-]?)?(?:\(?\d{3}\)?[\s.\-]?)\d{3}[\s.\-]\d{4}")

def compact(s): return re.sub(r"\s+"," ",(s or "").strip())
def norm(s): return compact(s).lower()

def parse_date_piece(s):
    s=compact(s)
    m=re.fullmatch(r"(\d{1,2})/(\d{1,2})/(\d{4})",s)
    if not m: raise ValueError(f"Unrecognized ADGA date: {s!r}")
    mm,dd,yy=map(int,m.groups())
    return datetime(yy,mm,dd).date().isoformat()

def parse_date_range(raw):
    raw=compact(raw).replace("–","-").replace("—","-")
    parts=re.split(r"\s*-\s*(?=\d{1,2}/\d{1,2}/\d{4})",raw,maxsplit=1)
    start=parse_date_piece(parts[0])
    return start, parse_date_piece(parts[1]) if len(parts)==2 else start

def parts(td): return [compact(x) for x in td.stripped_strings if compact(x)]
def joined(p,sep=", "): return sep.join(dict.fromkeys(p))

def contact(p):
    if not p: return {"name":"","address":"","phone":"","email":"","raw":""}
    email=next((EMAIL_RE.search(v).group(0) for v in p if EMAIL_RE.search(v)),"")
    phone=next((PHONE_RE.search(v).group(0) for v in p if PHONE_RE.search(v)),"")
    addr=[v for v in p[1:] if not(email and email in v) and not(phone and phone in v)]
    return {"name":p[0],"address":", ".join(addr),"phone":phone,"email":email,"raw":"\n".join(p)}

def fetch_rows():
    """Read ADGA's Events Calendar month pages and show detail pages.

    The old /adga-sanctioned-show-list/ endpoint was removed (HTTP 404).
    Month views are used for discovery; individual events supply structured
    date, venue, city, state, judges and show type information.
    """
    from calendar import monthrange
    from urllib.parse import urljoin, urlparse
    today = datetime.now(timezone.utc).date()
    session = requests.Session()
    session.headers.update({"User-Agent": UA, "Accept": "text/html"})
    # Revisit the current year and the following year; retain older archive.
    months = [(y, m) for y in (today.year, today.year + 1)
              for m in range(1, 13)
              if (y, m) >= (today.year, today.month)]
    event_urls = set()
    for year, month in months:
        url = f"https://adga.org/events/month/{year}-{month:02d}/"
        response = session.get(url, timeout=60)
        if response.status_code == 404:
            # The calendar does not publish an archive for every month.
            print(f"INFO: ADGA calendar month unavailable (404); skipping {url}")
            continue
        response.raise_for_status()
        soup = BeautifulSoup(response.text, "html.parser")
        for a in soup.select('a[href*="/event/"]'):
            href = urljoin(response.url, a.get("href", ""))
            parsed = urlparse(href)
            if parsed.netloc.lower() in ("adga.org", "www.adga.org") and re.match(r"^/event/[^/]+/?$", parsed.path):
                event_urls.add(parsed.scheme + "://" + parsed.netloc + parsed.path)
    if not event_urls:
        raise RuntimeError("ADGA calendar returned zero event links; preserving existing archive.")

    def labeled_value(soup, label):
        # The Events Calendar 'Other' section uses adjacent dt/dd pairs.
        for dt in soup.select("dt"):
            if norm(dt.get_text(" ", strip=True)).rstrip(":") == norm(label):
                dd = dt.find_next_sibling("dd")
                if dd: return compact(dd.get_text(" ", strip=True))
        return ""

    out = []
    failures = []
    for url in sorted(event_urls):
        try:
            response = session.get(url, timeout=60)
            response.raise_for_status()
            soup = BeautifulSoup(response.text, "html.parser")
            # Do not import conventions, meetings or other non-show events.
            categories = [norm(a.get_text(" ", strip=True))
                          for a in soup.select(".tribe-events-event-categories a, .tribe-events-event-meta a[href*='category']")]
            if "shows" not in categories:
                # ADGA's event pages show a 'Shows' link under Details.
                # Some templates omit the conventional category CSS classes.
                category_links = [norm(a.get_text(" ", strip=True))
                                  for a in soup.select('a[href*="event-category"], a[href*="/category/"]')]
                if "shows" not in category_links:
                    print(f"INFO: Ignoring non-show event: {url}")
                    continue
            title = soup.select_one(".tribe-events-single-event-title, h1")
            name = compact(title.get_text(" ", strip=True)) if title else ""
            # ADGA renders ISO dates in the Details section, for example
            # 'Start: November 6 (2026-11-06)' and 'End: November 8 (2026-11-08)'.
            # Read that first instead of relying on the calendar plugin's
            # optional attributes, which are absent on the current theme.
            details = soup.select_one('.tribe-events-meta-group-details, .tribe-events-event-meta')
            dates = []
            if details:
                dates = list(dict.fromkeys(re.findall(r'\b20\d{2}-\d{2}-\d{2}\b', details.get_text(' ', strip=True))))
            if not dates:
                dates = list(dict.fromkeys(re.findall(r'\b20\d{2}-\d{2}-\d{2}\b', soup.get_text(' ', strip=True))))
            if not dates:
                # Fallback for events that use HTML5 time attributes.
                for el in soup.select('time[datetime], abbr[title], [itemprop="startDate"], [itemprop="endDate"]'):
                    value = el.get('datetime') or el.get('title') or el.get('content') or ''
                    m = re.search(r'20\d{2}-\d{2}-\d{2}', value)
                    if m and m.group(0) not in dates: dates.append(m.group(0))
            if not dates:
                # The page can contain JSON-LD dates even when visible date attributes are absent.
                for key in ('startDate', 'endDate'):
                    matches = re.findall(r'["\']' + key + r'["\']\s*:\s*["\'](20\d{2}-\d{2}-\d{2})', response.text)
                    for date in matches:
                        if date not in dates: dates.append(date)
            if not dates or not name:
                raise ValueError("missing title or machine-readable event date")
            start, end = dates[0], dates[1] if len(dates) > 1 else dates[0]
            if end < start: end = start
            city, state = labeled_value(soup, 'City'), labeled_value(soup, 'State')
            # Most ADGA show pages explicitly include City and State under Other.
            # For exceptions, use the venue address, not the contact's address.
            venue = soup.select_one('.tribe-events-meta-group-venue, .tribe-events-address, .tribe-venue, .tribe-events-venue-details')
            venue_text = compact(venue.get_text(' ', strip=True)) if venue else ''
            if not city or not state:
                match = re.search(r'([A-Za-z][A-Za-z .\'-]+?),\s*([A-Z]{2})\b', venue_text)
                if match:
                    city = city or compact(match.group(1))
                    state = state or match.group(2)
            location = city or venue_text
            if not state or not location:
                raise ValueError('missing city/location or state')
            description = soup.select_one(".tribe-events-single-event-description")
            contact_parts = parts(description) if description else []
            c = contact(contact_parts)
            raw = start.replace("-", "/") + (" - " + end.replace("-", "/") if end != start else "")
            key = " | ".join([start[:4], norm(name), norm(state), norm(location)])
            out.append({
                "date_adga": raw, "start": start, "end": end,
                "month": int(start[5:7]), "show_name": name,
                "state_original": state, "state": state, "location": location,
                "judges": labeled_value(soup, "Judges"),
                "show_type": labeled_value(soup, "Show Type"),
                "contact_name": c["name"], "contact_address": c["address"],
                "contact_phone": c["phone"], "contact_email": c["email"],
                "contact_raw": c["raw"], "source_key": key, "source_url": url,
            })
        except Exception as exc:
            failures.append(f"{url}: {exc}")
    if failures:
        for issue in failures[:15]:
            print(f"WARNING skipped ADGA event: {issue}", file=sys.stderr)
    if not out:
        raise RuntimeError("No valid sanctioned shows parsed; preserving existing archive.")
    # A wholesale parse failure must not be committed as a successful refresh.
    if len(failures) > max(5, len(out)):
        raise RuntimeError(f"Too many ADGA event parse failures ({len(failures)} vs {len(out)} shows); preserving archive.")
    print(f"Fetched {len(out)} show events from {len(event_urls)} ADGA calendar events.")
    return out

def load_existing():
    s = DATA_JS.read_text(encoding="utf-8")

    # Parse the JavaScript assignments independently so formatting,
    # whitespace, or line breaks do not matter.
    shows_match = re.search(
        r"window\.ADGA_SHOWS\s*=\s*(\[.*?\])\s*;",
        s,
        re.S,
    )
    meta_match = re.search(
        r"window\.ADGA_META\s*=\s*(\{.*?\})\s*;",
        s,
        re.S,
    )

    if not shows_match:
        preview = s[:300].replace("\n", "\\n")
        raise RuntimeError(
            "Existing data/shows.js was found, but ADGA_SHOWS could not be parsed. "
            f"File begins with: {preview}"
        )

    rows = json.loads(shows_match.group(1))
    meta = json.loads(meta_match.group(1)) if meta_match else {}

    if not isinstance(rows, list):
        raise RuntimeError("ADGA_SHOWS in data/shows.js is not a list.")

    for x in rows:
        if not x.get("source_key"):
            x["source_key"] = " | ".join([
                (x.get("start") or "")[:4],
                norm(x.get("show_name")),
                norm(x.get("state_original")),
                norm(x.get("location")),
            ])
    return rows, meta

def load_cache():
    return json.loads(CACHE_JSON.read_text(encoding="utf-8")) if CACHE_JSON.exists() else {}

def geocode(loc,st,cache):
    key=f"{norm(loc)}|{norm(st)}"
    if key in cache: return cache[key].get("lat"),cache[key].get("lon")
    country="Canada" if ("canada" in norm(st) or "ontario" in norm(st)) else "USA"
    q=f"{loc}, {st}, {country}"
    url="https://nominatim.openstreetmap.org/search?"+urlencode({"q":q,"format":"jsonv2","limit":1})
    time.sleep(1.05)
    try:
        r=requests.get(url,timeout=35,headers={"User-Agent":UA}); r.raise_for_status(); d=r.json()
        if d:
            lat,lon=float(d[0]["lat"]),float(d[0]["lon"])
            cache[key]={"lat":lat,"lon":lon,"query":q,"source":"OpenStreetMap Nominatim"}
            return lat,lon
    except Exception as e:
        print(f"WARNING geocoding failed for {q}: {e}",file=sys.stderr)
    cache[key]={"lat":None,"lon":None,"query":q,"source":"geocoding failed"}
    return None,None

def merge(existing,current,cache):
    by={x["source_key"]:x for x in existing}
    next_id=max([int(x.get("id",-1)) for x in existing]+[-1])+1
    added=updated=0
    for inc in current:
        old=by.get(inc["source_key"])
        if old:
            inc["id"]=old["id"]; inc["lat"]=old.get("lat"); inc["lon"]=old.get("lon")
            if inc["lat"] is None or inc["lon"] is None:
                inc["lat"],inc["lon"]=geocode(inc["location"],inc["state_original"],cache)
            by[inc["source_key"]]={**old,**inc}; updated+=1
        else:
            inc["id"]=next_id; next_id+=1
            inc["lat"],inc["lon"]=geocode(inc["location"],inc["state_original"],cache)
            by[inc["source_key"]]=inc; added+=1
    rows=list(by.values())
    for x in rows: x["distance"]=None; x["distance_band"]=""
    rows.sort(key=lambda x:(x.get("start",""),x.get("show_name",""),int(x.get("id",0))))
    return rows,added,updated

def main():
    existing,oldmeta=load_existing(); cache=load_cache(); current=fetch_rows()
    rows,added,updated=merge(existing,current,cache)
    meta={"source_url":SOURCE_URL,
      "last_updated_utc":datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00","Z"),
      "record_count":len(rows),"current_page_records":len(current),
      "records_added_this_run":added,"records_refreshed_this_run":updated,
      "previous_last_updated_utc":oldmeta.get("last_updated_utc","")}
    tmp=DATA_JS.with_suffix(".js.tmp")
    tmp.write_text("window.ADGA_SHOWS = "+json.dumps(rows,ensure_ascii=False,separators=(",",":"))+";\nwindow.ADGA_META = "+json.dumps(meta,ensure_ascii=False,separators=(",",":"))+";\n",encoding="utf-8")
    tmp.replace(DATA_JS)
    CACHE_JSON.write_text(json.dumps(cache,indent=2,ensure_ascii=False),encoding="utf-8")
    print(f"SUCCESS: {len(current)} ADGA current-page rows; {added} added; {updated} refreshed; {len(rows)} archived total.")

if __name__=="__main__": main()
