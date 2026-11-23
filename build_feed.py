#!/usr/bin/env python3
"""Unofficial Chaburah.org -> one combined podcast RSS feed.

Runs in GitHub Actions. Does not download or rehost audio. It discovers
public audio URLs, checks that clients can fetch them, and hosts only XML.
"""
import argparse
import asyncio
import email.utils
import html
import json
import os
import re
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from urllib.parse import urljoin, urlparse, parse_qs
import xml.etree.ElementTree as ET

import requests
from bs4 import BeautifulSoup
from playwright.async_api import async_playwright, TimeoutError as PlaywrightTimeout

SITE = "https://chaburah.org"
LISTING = SITE + "/shiurim"
BASE = Path(__file__).resolve().parent
OUT = BASE / "public"
STATE_FILE = BASE / "episodes.json"
USER_AGENT = "ChaburahPodcastFeed/1.0 (personal noncommercial RSS; polite rate limiting)"
DATE_PATTERN = re.compile(r"\b(?:Mon|Tue|Wed|Thu|Fri|Sat|Sun)(?:day)?,?\s+(?:Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec)[a-z]*\s+\d{1,2},?\s+\d{4}", re.I)
AUDIO_EXT = re.compile(r"\.(?:mp3|m4a|aac|mp4|ogg|oga|opus|wav|flac)(?:$|[?#])", re.I)
ABS_URL = re.compile(r"https?://[^\s\"'<>\\]+", re.I)
MEDIA_KEYS = re.compile(r"(?:audio|download|recording|media|mp3|file)(?:_?url|_?src|_?link|_?path)?", re.I)
MONTHS = ["Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"]


def page_id(url):
    match = re.search(r"/shiurim/(wa[\w-]+)(?:[/?#]|$)", url, re.I)
    return match.group(1) if match else None


def normalize_url(url, base=LISTING):
    if not url or url.startswith(("data:", "blob:", "javascript:")):
        return None
    u = urljoin(base, html.unescape(url).replace("\\/", "/").replace("\\u0026", "&"))
    return u if urlparse(u).scheme in ("http", "https") else None


def media_candidate(url):
    if not url:
        return False
    parsed = urlparse(url)
    if parsed.scheme not in ("http", "https"):
        return False
    return bool(AUDIO_EXT.search(url) or re.search(r"/(?:audio|download|recording|stream)(?:/|\?|$)", parsed.path, re.I))


def temporary_url(url):
    params = {x.lower() for x in parse_qs(urlparse(url).query)}
    return bool(params & {"x-amz-signature", "x-amz-expires", "x-goog-signature", "x-goog-expires", "se", "sig", "signature", "expires"})


def scan_html_for_audio(page_html, page_url):
    """Discover media from DOM attributes and embedded state. Never assume an ID is an MP3."""
    soup = BeautifulSoup(page_html, "html.parser")
    found = []
    for el in soup.select("audio, source, a, meta, button, [data-audio], [data-audio-url], [data-download-url]"):
        attrs = dict(el.attrs)
        for key, value in attrs.items():
            if isinstance(value, list):
                value = " ".join(str(v) for v in value)
            value = str(value)
            if key in ("src", "href", "content") or MEDIA_KEYS.search(key):
                u = normalize_url(value, page_url)
                if u and media_candidate(u):
                    found.append(u)
    # Many React/Next pages embed serialized JSON containing the audio URL.
    deescaped = html.unescape(page_html).replace("\\/", "/").replace("\\u0026", "&")
    for raw in ABS_URL.findall(deescaped):
        u = normalize_url(raw.rstrip(",)}];"), page_url)
        if u and media_candidate(u):
            found.append(u)
    # JSON can also have a same-origin relative path, under explicit audio keys.
    for key, raw in re.findall(r'(?i)["\'](audioUrl|audio_url|downloadUrl|download_url|recordingUrl|mediaUrl|mp3Url|audioSrc)["\']\s*:\s*["\']([^"\']+)', deescaped):
        u = normalize_url(raw, page_url)
        if u and media_candidate(u):
            found.append(u)
    return list(dict.fromkeys(found))


def parse_date(s):
    if not s:
        return None
    match = DATE_PATTERN.search(s)
    if match:
        s = match.group(0)
    s = s.strip().replace("Sept", "Sep")
    for fmt in ("%a, %b %d, %Y", "%A, %B %d, %Y", "%a %b %d, %Y", "%b %d, %Y", "%Y-%m-%d", "%Y-%m-%dT%H:%M:%S%z"):
        try:
            d = datetime.strptime(s, fmt)
            return d.replace(tzinfo=timezone.utc).isoformat() if not d.tzinfo else d.isoformat()
        except ValueError:
            continue
    return None


def extract_episode(page_html, page_url, listing_text=""):
    soup = BeautifulSoup(page_html, "html.parser")
    heading = soup.find("h1")
    title = heading.get_text(" ", strip=True) if heading else ""
    if not title:
        title = listing_text[:150] or page_id(page_url)
    # The page heading is followed by the maggid-shiur link and a date/duration line.
    start = heading.parent if heading else soup
    header_text = start.get_text(" ", strip=True)[:700]
    if not DATE_PATTERN.search(header_text):
        header_text = soup.get_text(" ", strip=True)[:1200]
    date = parse_date(header_text)
    if not date:
        for time_el in soup.find_all("time"):
            date = parse_date(time_el.get("datetime", "") or time_el.get_text(" ", strip=True))
            if date:
                break
    if not date:
        # Preserve an estimated discovery date only when the publisher gives none.
        date = datetime.now(timezone.utc).isoformat()
        estimated_date = True
    else:
        estimated_date = False

    author = "Chaburah"
    if heading:
        # Maggid-shiur's dedicated link is more precise than running together
        # all nearby text (which could accidentally include the weekday).
        for link in heading.parent.find_all("a", limit=8):
            candidate = link.get_text(" ", strip=True)
            if re.match(r"^(?:Rabbi|Rav)\s+", candidate):
                author = candidate[:120]
                break
    if author == "Chaburah":
        header_without_date = DATE_PATTERN.sub(" ", header_text)
        for match in re.finditer(r"\b(?:Rabbi|Rav)\s+[\w.'-]+(?:\s+[\w.'-]+)?|\b(?:Yoeli Doppelt|Aviv Reif|Ian Turk)\b", header_without_date):
            author = match.group(0)
            break
    duration_match = re.search(r"\b(\d{1,3})\s*min\b", header_text)
    seconds = int(duration_match.group(1)) * 60 if duration_match else 0

    description = ""
    summary_header = soup.find(string=re.compile("AI summary", re.I))
    if summary_header:
        parent = summary_header.parent
        for sibling in parent.find_all_next(["p", "div"], limit=8):
            candidate = sibling.get_text(" ", strip=True)
            if 35 < len(candidate) < 1500 and not candidate.lower().startswith("ai summary"):
                description = candidate
                break
    if not description:
        tag = soup.find("meta", attrs={"name": "description"})
        if tag:
            description = tag.get("content", "")
    if not description:
        description = "Listen to this shiur on Chaburah.org."
    if "Chaburah" not in description:
        description += "\n\nSource: Chaburah.org"

    return {
        "id": page_id(page_url), "page": page_url, "title": title,
        "author": author, "published": date, "date_estimated": estimated_date,
        "duration": seconds, "description": description[:2500],
    }


def validate_audio(url):
    """Check public playability without transferring the whole recording."""
    if temporary_url(url):
        return None, "temporary signed URL (will expire)"
    s = requests.Session()
    s.headers.update({"User-Agent": "Mozilla/5.0 (compatible; ChaburahRSS/1.0)"})
    headers = {}
    final_url = url
    try:
        r = s.head(url, timeout=15, allow_redirects=True)
        headers = r.headers
        final_url = r.url
        status = r.status_code
        r.close()
        if status >= 400 or not (headers.get("Content-Type", "").lower().startswith("audio/") or AUDIO_EXT.search(final_url)):
            # Some CDNs reject HEAD; GET with Range checks first bytes only.
            with s.get(url, timeout=15, headers={"Range": "bytes=0-3"}, stream=True, allow_redirects=True) as r:
                status = r.status_code
                headers = r.headers
                final_url = r.url
    except requests.RequestException as exc:
        return None, f"audio HTTP check failed: {exc.__class__.__name__}"
    if status not in (200, 206):
        return None, f"audio returned HTTP {status}"
    # A stable source URL is safe even when its server redirects to a freshly
    # signed CDN URL. Preserve the original source URL in RSS.
    mime = headers.get("Content-Type", "").lower().split(";")[0].strip()
    if mime.startswith("text/html") or mime.startswith("application/json"):
        return None, f"audio endpoint returned {mime}"
    if not (mime.startswith("audio/") or mime in ("application/octet-stream", "binary/octet-stream") or (AUDIO_EXT.search(final_url) and mime not in ("text/plain", "application/xml"))):
        return None, f"not recognizable audio: {mime or '(missing content type)'}"
    try:
        size = int(headers.get("Content-Length", "0"))
        if status == 206:
            m = re.search(r"/(\d+)$", headers.get("Content-Range", ""))
            if m:
                size = int(m.group(1))
        if size < 0:
            size = 0
    except ValueError:
        size = 0
    extension = re.search(r"\.(mp3|m4a|aac|mp4|ogg|oga|opus|wav|flac)(?:$|[?#])", final_url, re.I)
    if mime in ("application/octet-stream", "binary/octet-stream", ""):
        ext = extension.group(1).lower() if extension else "mp3"
        mime = {"mp3":"audio/mpeg", "m4a":"audio/mp4", "mp4":"audio/mp4", "aac":"audio/aac", "ogg":"audio/ogg", "oga":"audio/ogg", "opus":"audio/ogg", "wav":"audio/wav", "flac":"audio/flac"}.get(ext, "audio/mpeg")
    return {"url": url, "length": size, "type": mime}, None


async def discover_audio(page, page_url):
    candidates = scan_html_for_audio(await page.content(), page_url)
    # DOM media sources and browser resource-timing entries, post hydration.
    try:
        found = await page.evaluate("""() => {
           const out = [];
           for(const e of document.querySelectorAll('audio,source,[data-audio-url],[data-download-url]')) {
             for(const a of e.attributes) if (/src|url|href|download|media|audio/i.test(a.name)) out.push(a.value);
             if(e.currentSrc) out.push(e.currentSrc);
           }
           for(const r of performance.getEntriesByType('resource')) if(/\\.(mp3|m4a|aac|ogg)(\\?|$)/i.test(r.name)) out.push(r.name);
           return out;
        }""")
        candidates += [normalize_url(u, page_url) for u in found]
    except Exception:
        pass
    candidates = [u for u in dict.fromkeys(candidates) if media_candidate(u)]
    if candidates:
        return candidates

    # If audio is generated by clicking rather than in HTML, observe downloads
    # and media requests. Do not save any audio files ourselves.
    seen = []
    def on_download(download):
        seen.append(download.url)
        # Capture the requested URL and cancel the transfer immediately;
        # the feed does not download or host recordings.
        asyncio.create_task(download.cancel())
    def on_request(request):
        if media_candidate(request.url) or request.resource_type == "media":
            seen.append(request.url)
    def on_response(response):
        try:
            kind = response.headers.get("content-type", "").lower()
            if kind.startswith("audio/") or ("octet-stream" in kind and response.request.resource_type == "media"):
                seen.append(response.url)
        except Exception:
            pass
    page.on("download", on_download)
    page.on("request", on_request)
    page.on("response", on_response)
    try:
        control = page.get_by_text("Download", exact=True).first
        if await control.count():
            await control.click(timeout=4000, no_wait_after=True)
            await page.wait_for_timeout(1800)
    except Exception:
        pass
    try:
        candidates += scan_html_for_audio(await page.content(), page_url)
    except Exception:
        pass
    candidates = [u for u in dict.fromkeys(candidates) if media_candidate(u)]
    # Network-observed media and actual browser downloads are trustworthy
    # candidates even if their URLs have no recognizable file extension.
    candidates += [normalize_url(u, page_url) for u in seen]
    return [u for u in dict.fromkeys(candidates) if u]


def parse_listing(page_html):
    soup = BeautifulSoup(page_html, "html.parser")
    out = {}
    for a in soup.find_all("a", href=True):
        href = normalize_url(a["href"], LISTING)
        sid = page_id(href) if href else None
        if sid:
            out[sid] = {"id": sid, "page": href, "label": a.get_text(" ", strip=True)}
    return list(out.values())


def load_state(feed_url):
    if STATE_FILE.exists():
        try:
            return json.loads(STATE_FILE.read_text(encoding="utf8"))
        except (ValueError, OSError):
            pass
    state_url = feed_url.rsplit("/", 1)[0] + "/episodes.json"
    try:
        r = requests.get(state_url, timeout=12, headers={"User-Agent": USER_AGENT})
        if r.ok:
            obj = r.json()
            if isinstance(obj, dict) and isinstance(obj.get("episodes"), dict):
                print(f"Restored {len(obj['episodes'])} previous episodes from GitHub Pages")
                return obj
    except Exception as exc:
        print(f"No previously published cache available ({exc.__class__.__name__}); starting fresh")
    return {"episodes": {}, "failed": {}}


async def scrape_one(browser, lock, item):
    async with lock:
        page = await browser.new_page(accept_downloads=True)
        try:
            await page.goto(item["page"], wait_until="domcontentloaded", timeout=25000)
            await page.wait_for_timeout(900)
            page_html = await page.content()
            episode = extract_episode(page_html, item["page"], item["label"])
            candidates = await discover_audio(page, item["page"])
            if not candidates:
                return item["id"], None, "no direct audio URL detected in page/player/download control"
            # Check only the best few candidate URLs; avoid hammering the source.
            errors = []
            for url in candidates[:5]:
                audio, error = await asyncio.to_thread(validate_audio, url)
                if audio:
                    episode["audio"] = audio
                    return item["id"], episode, None
                errors.append(error)
            return item["id"], None, "; ".join(x for x in errors if x)[:250]
        except Exception as exc:
            return item["id"], None, f"{exc.__class__.__name__}: {str(exc)[:130]}"
        finally:
            await page.close()


def feed_xml(episodes, feed_url):
    ET.register_namespace("itunes", "http://www.itunes.com/dtds/podcast-1.0.dtd")
    ET.register_namespace("atom", "http://www.w3.org/2005/Atom")
    ET.register_namespace("content", "http://purl.org/rss/1.0/modules/content/")
    root = ET.Element("rss", version="2.0")
    channel = ET.SubElement(root, "channel")
    for name, content in [
        ("title", "Chaburah — All Shiurim"),
        ("link", LISTING),
        ("description", "An unofficial automatically updated podcast feed of publicly available shiurim from Chaburah.org. All audio remains hosted by its original publisher."),
        ("language", "en-us"),
        ("lastBuildDate", email.utils.format_datetime(datetime.now(timezone.utc))),
        ("generator", "Chaburah RSS Feed Builder"),
    ]:
        ET.SubElement(channel, name).text = content
    ET.SubElement(channel, "{http://www.w3.org/2005/Atom}link", {"href": feed_url, "rel": "self", "type": "application/rss+xml"})
    ET.SubElement(channel, "{http://www.itunes.com/dtds/podcast-1.0.dtd}author").text = "Chaburah (unofficial feed)"
    ET.SubElement(channel, "{http://www.itunes.com/dtds/podcast-1.0.dtd}explicit").text = "false"
    ET.SubElement(channel, "{http://www.itunes.com/dtds/podcast-1.0.dtd}type").text = "episodic"
    ET.SubElement(channel, "{http://www.itunes.com/dtds/podcast-1.0.dtd}category", {"text": "Religion & Spirituality"})
    for episode in sorted(episodes, key=lambda x:(x.get("published", ""), x.get("id", "")), reverse=True):
        item = ET.SubElement(channel, "item")
        title = episode["title"]
        for key, value in [
            ("title", title), ("link", episode["page"]),
            ("guid", episode["page"]), ("description", episode["description"]),
            ("pubDate", email.utils.format_datetime(datetime.fromisoformat(episode["published"]).astimezone(timezone.utc))),
        ]:
            el = ET.SubElement(item, key)
            if key == "guid":
                el.set("isPermaLink", "true")
            el.text = value
        ET.SubElement(item, "enclosure", {"url": episode["audio"]["url"], "length": str(episode["audio"]["length"]), "type": episode["audio"]["type"]})
        ET.SubElement(item, "{http://www.itunes.com/dtds/podcast-1.0.dtd}author").text = episode["author"]
        if episode.get("duration"):
            ET.SubElement(item, "{http://www.itunes.com/dtds/podcast-1.0.dtd}duration").text = str(episode["duration"])
        ET.SubElement(item, "{http://www.itunes.com/dtds/podcast-1.0.dtd}explicit").text = "false"
    return ET.tostring(root, encoding="utf-8", xml_declaration=True)


def make_status_page(total, episodes, failures):
    content = f"""<!DOCTYPE html><html lang='en'><meta charset='utf-8'><meta name='viewport' content='width=device-width,initial-scale=1'><title>Chaburah Shiurim RSS</title><style>body{{font:17px/1.65 system-ui,sans-serif;max-width:740px;margin:3rem auto;padding:0 20px;color:#202b32}}h1{{font-size:2rem}}a{{color:#175a8b}}code{{background:#eee;padding:3px 6px;word-break:break-all}}</style><h1>Chaburah — All Shiurim</h1><p>Unofficial podcast feed for listening to publicly available shiurim from <a href='{LISTING}'>Chaburah.org</a>.</p><p><strong>{episodes} episodes</strong> currently in the podcast feed, out of {total} shiurim discovered. Older recordings are added gradually.</p><p><a href='./feed.xml'>Subscribe to the RSS feed</a> in a podcast app such as Podcast Addict.</p><p>The audio is streamed from the original publisher; this site does not rehost recordings.</p><small>Last successful update: {datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M UTC')} · {failures} entries currently need rechecking.</small></html>"""
    (OUT / "index.html").write_text(content, encoding="utf8")


async def main(args):
    repo = os.environ.get("GITHUB_REPOSITORY", "YOUR-GITHUB-USERNAME/chaburah-rss")
    owner, _, name = repo.partition("/")
    url = os.environ.get("FEED_URL", f"https://{owner}.github.io/{name}/feed.xml")
    OUT.mkdir(parents=True, exist_ok=True)
    state = load_state(url)
    state.setdefault("episodes", {})
    state.setdefault("failed", {})
    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True, args=["--no-sandbox"])
        listing_page = await browser.new_page()
        await listing_page.goto(LISTING, wait_until="domcontentloaded", timeout=45000)
        await listing_page.wait_for_timeout(1500)
        listing = parse_listing(await listing_page.content())
        # The library may initially render only 50 links. Expand its normal
        # "Show 50 more" UI until all available episodes are discoverable.
        for _ in range(16):
            more = listing_page.get_by_role("button", name=re.compile(r"Show\s+\d+\s+more", re.I))
            if not await more.count():
                break
            try:
                await more.first.click(timeout=2500)
                await listing_page.wait_for_timeout(250)
                expanded = parse_listing(await listing_page.content())
                if len(expanded) <= len(listing):
                    break
                listing = expanded
            except Exception:
                break
        await listing_page.close()
        if not listing:
            await browser.close()
            raise RuntimeError("No shiurim could be read from the library. Page structure may have changed.")
        print(f"Found {len(listing)} shiurim on Chaburah.org")
        # Check unprocessed newest episodes first, then retry failed ones only
        # after a day. Historical backlog fills in automatically over time.
        now = datetime.now(timezone.utc)
        work = []
        for it in listing:
            sid = it["id"]
            if sid in state["episodes"]:
                continue
            record = state["failed"].get(sid)
            if record:
                try:
                    attempted = datetime.fromisoformat(record["checked"])
                    if now - attempted < timedelta(hours=20):
                        continue
                except (ValueError, KeyError):
                    pass
            work.append(it)
        work = work[:args.batch_size]
        print(f"Checking {len(work)} new/backlog items, with up to {args.concurrency} concurrent pages")
        lock = asyncio.Semaphore(args.concurrency)
        tasks = [scrape_one(browser, lock, it) for it in work]
        for fut in asyncio.as_completed(tasks):
            sid, episode, problem = await fut
            if episode:
                state["episodes"][sid] = episode
                state["failed"].pop(sid, None)
                print(f"OK {sid}: {episode['title']} ({episode['audio']['type']})")
            else:
                state["failed"][sid] = {"reason": problem, "checked": now.isoformat()}
                print(f"FAILED {sid}: {problem}")
        await browser.close()

    # Persist metadata in the published Pages site, so each update can fetch
    # previous episodes without writing anything to the source repository.
    state["site_total"] = len(listing)
    state["updated"] = datetime.now(timezone.utc).isoformat()
    (OUT / "episodes.json").write_text(json.dumps(state, ensure_ascii=False, indent=2), encoding="utf8")
    saved = sorted(state["episodes"].values(), key=lambda v: v.get("published", ""), reverse=True)
    if not saved:
        raise RuntimeError("No playable audio sources were discovered yet; RSS NOT published. See job logs.")
    (OUT / "feed.xml").write_bytes(feed_xml(saved, url))
    make_status_page(len(listing), len(saved), len(state["failed"]))
    (OUT / ".nojekyll").touch()
    print(f"Generated {len(saved)} podcast episodes at public/feed.xml; {len(state['failed'])} could not be retrieved")


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--batch-size", type=int, default=65)
    parser.add_argument("--concurrency", type=int, default=3)
    options = parser.parse_args()
    try:
        asyncio.run(main(options))
    except Exception as exc:
        print("ERROR:", exc, file=sys.stderr)
        sys.exit(1)
