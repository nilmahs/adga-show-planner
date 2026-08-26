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
SOURCE_URL="https://adga.org/adga-sanctioned-show-list/"
UA="HardwickeFarms-ADGA-Show-Planner/1.0"
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
    r=requests.get(SOURCE_URL,timeout=45,headers={"User-Agent":UA,"Accept":"text/html"})
    r.raise_for_status()
    soup=BeautifulSoup(r.text,"html.parser")
    table=None
    for t in soup.find_all("table"):
        h=" | ".join(compact(x.get_text(" ",strip=True)).lower() for x in t.find_all("th"))
        if "date of judging" in h and "show name" in h and "location" in h:
            table=t; break
    if table is None:
        raise RuntimeError("ADGA sanctioned-show table not found; existing archive was not changed.")
    out=[]
    for tr in table.find_all("tr"):
        td=tr.find_all("td")
        if len(td)<7: continue
        p=[parts(c) for c in td[:7]]
        raw=joined(p[0]," ")
        try: start,end=parse_date_range(raw)
        except Exception as e:
            print(f"WARNING skipped date {raw!r}: {e}",file=sys.stderr); continue
        c=contact(p[6]); name=joined(p[1]," — "); st=joined(p[2]," "); loc=joined(p[3]," ")
        key=" | ".join([start[:4],norm(name),norm(st),norm(loc)])
        out.append({"date_adga":raw,"start":start,"end":end,"month":int(start[5:7]),
          "show_name":name,"state_original":st,"state":st,"location":loc,
          "judges":joined(p[4],", "),"show_type":joined(p[5],", "),
          "contact_name":c["name"],"contact_address":c["address"],"contact_phone":c["phone"],
          "contact_email":c["email"],"contact_raw":c["raw"],"source_key":key,"source_url":SOURCE_URL})
    if not out:
        raise RuntimeError("Zero valid ADGA show rows parsed; existing archive was not changed.")
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
        preview = s[:300].replace("
", "\n")
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
