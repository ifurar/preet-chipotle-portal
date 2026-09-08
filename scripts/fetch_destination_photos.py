#!/usr/bin/env python3
"""Fetch a small photo set (landmark / lifestyle / scenic) for each destination.

The brief: these photos need to make someone think "I need to go there" and
help them judge whether they'd actually want to *live* somewhere, not just
prove a landmark exists. So for every destination this script fetches THREE
different shots from Wikimedia Commons:

  - landmark: the specific, actual thing the place is known for (Charles
    Bridge for Prague, Petronas Towers for Kuala Lumpur -- see
    NAMED_LANDMARKS), not a generic skyline.
  - lifestyle: an inviting street/old-town/café scene -- the kind of everyday
    charm that sells someone on day-to-day life there.
  - scenic: a natural-beauty or golden-hour shot (waterfront, sunset, park,
    coastline) with aspirational appeal.

For each shot it ranks candidates (names the landmark > otherwise-evocative
subject > color over black-and-white > pixel count), downloads them in that
order, and verifies each for correct location (city-name/category match, GPS
distance, name-collision exclusions) and for stitching-cutout artifacts,
skipping to the next candidate until one passes. Shots for the same
destination never reuse the same source file.

Output:
  - ./destination_photos/<slug>-<shot>.jpg for each of the three shots
  - ./photo_credits.txt: filename, Commons page URL, author, license per photo
  - a summary table (destination | shot | filename | resolution | license),
    noting anything that had to be skipped
"""

import os
import re
import sys
import time
from math import atan2, cos, radians, sin, sqrt

import requests
from PIL import Image

API_URL = "https://commons.wikimedia.org/w/api.php"
USER_AGENT = "DestinationPhotoFetcher/1.0 (https://commons.wikimedia.org; educational script)"

OUTPUT_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "destination_photos")
CREDITS_PATH = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "photo_credits.txt")

MIN_WIDTH = 1600
MIN_HEIGHT = 900
MIN_ASPECT = 1.15   # width / height must exceed this to count as "landscape"
MAX_ASPECT = 3.2    # reject extreme panorama crops / banners
MAX_CANDIDATES_TRIED = 5  # per shot, before giving up on that shot

DESTINATIONS = [
    "Prague, Czechia",
    "Valencia, Spain",
    "Ljubljana, Slovenia",
    "Porto, Portugal",
    "Budapest, Hungary",
    "Tbilisi, Georgia",
    "Kuala Lumpur, Malaysia",
    "Chiang Mai, Thailand",
    "Chester, United Kingdom",
    "Laguna Beach, California",
]

# Three shots per destination: the specific landmark, an inviting everyday
# street/lifestyle scene, and a natural-beauty/golden-hour scenic shot.
SHOT_TYPES = ["landmark", "lifestyle", "scenic"]

# Known city-center coordinates, used to reject same-name-different-place mismatches
# (e.g. Porto Alegre/Brazil matching a "Porto" search, or Chester/Massachusetts matching
# "Chester") for candidates that carry EXIF/Commons geodata.
DESTINATION_COORDS = {
    "Prague, Czechia": (50.0755, 14.4378),
    "Valencia, Spain": (39.4699, -0.3763),
    "Ljubljana, Slovenia": (46.0569, 14.5058),
    "Porto, Portugal": (41.1579, -8.6291),
    "Budapest, Hungary": (47.4979, 19.0402),
    "Tbilisi, Georgia": (41.7151, 44.8271),
    "Kuala Lumpur, Malaysia": (3.1390, 101.6869),
    "Chiang Mai, Thailand": (18.7061, 98.9817),
    "Chester, United Kingdom": (53.1934, -2.8931),
    "Laguna Beach, California": (33.5427, -117.7854),
}
MAX_DISTANCE_KM = 60

# Cities in our list whose name collides with a different, better-known place elsewhere
# (Porto Alegre/Brazil, Chester/Massachusetts, Valencia/Venezuela, etc). Most Commons files
# lack machine-readable geodata, so the coordinate check alone can't be relied on to catch
# these; explicitly reject candidates whose title/categories mention the conflicting place.
DISAMBIGUATION_EXCLUDE = {
    "Porto": re.compile(r"\b(alegre|brazil|brasil)\b", re.IGNORECASE),
    "Valencia": re.compile(r"\b(venezuela|alicante|carabobo|philippines)\b", re.IGNORECASE),
    "Chester": re.compile(
        r"\b(massachusetts|pennsylvania|new jersey|south carolina|west virginia|virginia"
        r"|illinois|vermont|connecticut|usa|united states|chester-le-street)\b",
        re.IGNORECASE,
    ),
}

# Titles containing one of these are a strong signal the photo is actually an evocative,
# on-message shot (landmark / charming street / natural beauty) rather than an incidental
# street corner, construction update, or building photo that happens to be huge. Preferred
# over raw resolution so a giant but mundane scan doesn't win by default -- pixel count is
# only used to break ties within the same tier.
GOOD_SUBJECT_PATTERN = re.compile(
    r"\b(skyline|old\s?town|cityscape|panorama|aerial|landmark|cathedral|castle|citadel"
    r"|fortress|palace|tower|bridge|square|harbou?r|waterfront|downtown|skyscraper"
    r"|old\s?city|historic\s?(centre|center)"
    r"|street|caf[eé]|terrace|market|promenade|pier|garden|park|plaza|piazza"
    r"|sunset|sunrise|golden\s?hour|dusk|twilight"
    r"|riverside|seaside|beach|coastline|coast|cove|lagoon|shoreline)\b",
    re.IGNORECASE,
)

# The single named landmark(s) each destination is actually known for. These drive BOTH
# the "landmark" shot's search queries (so we go looking for the specific thing, not just
# "skyline") and the top tier of its ranking -- a candidate whose title names the actual
# landmark always outranks a generic skyline/panorama/aerial shot, even a bigger one.
NAMED_LANDMARKS = {
    "Prague": ["Charles Bridge", "Prague Castle", "Old Town Square", "Astronomical Clock"],
    "Valencia": ["City of Arts and Sciences", "Valencia Cathedral", "Torres de Serranos", "Serranos Towers"],
    "Ljubljana": ["Triple Bridge", "Ljubljana Castle", "Dragon Bridge", "Preseren Square"],
    "Porto": ["Dom Luis Bridge", "Livraria Lello", "Porto Cathedral", "Ribeira"],
    "Budapest": ["Hungarian Parliament", "Fisherman's Bastion", "Chain Bridge", "Buda Castle"],
    "Tbilisi": ["Narikala Fortress", "Bridge of Peace", "Holy Trinity Cathedral"],
    "Kuala Lumpur": ["Petronas Towers", "KL Tower", "Batu Caves"],
    "Chiang Mai": ["Wat Chedi Luang", "Wat Phra Singh", "Tha Phae Gate", "Doi Suthep"],
    "Chester": ["Chester Cathedral", "Chester Rows", "Eastgate Clock", "Roman Amphitheatre"],
    "Laguna Beach": ["Main Beach", "Heisler Park"],
}
LANDMARK_PATTERNS = {
    city: re.compile(r"\b(" + "|".join(re.escape(n) for n in names) + r")\b", re.IGNORECASE)
    for city, names in NAMED_LANDMARKS.items()
}

# Search terms for the "lifestyle" shot: an inviting everyday street/café/market scene.
# Generic on purpose -- these are meant to surface whatever charming pedestrian scene each
# city actually has on Commons, not force a specific spot.
LIFESTYLE_SUFFIXES = ["old town street", "cafe terrace", "market square", "colorful houses street"]

# Search terms for the "scenic" shot: natural beauty / golden-hour / water.
SCENIC_SUFFIXES = ["sunset", "waterfront", "aerial view golden hour", "riverside park"]

# Generic fallback terms for the "landmark" shot, queried alongside the named landmarks so
# there's still a pool of candidates if a specific landmark search comes up empty.
LANDMARK_FALLBACK_SUFFIXES = ["skyline", "old town", "cityscape", "aerial view", "panorama", "landmark"]

# A landmark-name or subject-keyword match on title alone doesn't guarantee an exterior/
# establishing shot -- searches deliberately also surface interior photos (a cathedral
# choir, a temple's prayer hall, a library reading room), which don't read as "this is the
# place" the way an exterior view does. These are rejected outright rather than deprioritized.
INTERIOR_PATTERN = re.compile(
    r"\b(interior|innenaufnahme|indoor|inside|nave|choir|cloister|crypt|sacristy"
    r"|lady\s?chapel|rood\s?screen|chancel|querhaus|organ|orgel|stained\s?glass\s?close"
    r"|assembly\s?hall|ordination\s?hall|prayer\s?hall|shrine\s?hall|wihan|viharn|ubosot"
    r"|monks?\s?(praying|chanting|ceremony)|reading\s?room|library\s?hall"
    r"|ceiling|fresco|frescoed|painted\s?ceiling|chandelier\s?close)\b",
    re.IGNORECASE,
)

# For a small number of destinations, the search results contained an outright better
# exterior/establishing shot of the named landmark that the generic ranking didn't surface
# -- usually because a bigger but more tightly-cropped or foreground-cluttered photo from
# the same series won on resolution. Verified by hand against real candidate pools; matched
# candidates jump to the top of the "landmark" shot's ranking specifically.
# NOTE: verify any addition by actually looking at the downloaded image, not just its
# title -- "Wat Chedi Luang Assembly Hall ... - Diliff" sounded like an exterior shot of
# the temple grounds and turned out to be an interior photo of monks in prayer.
PREFERRED_TITLE_SUBSTRINGS = {
    "Kuala Lumpur": [
        "Petronas Twin Towers, Kuala Lumpur, Malaysia",
        "Menara Kembar Petronas, Bandaraya Kuala Lumpur",
        "Kuala Lumpur Tower and Petronas Towers during Merdeka midnight",
    ],
    "Porto": ["View of Porto Cathedral from Clérigos Tower"],
    "Chiang Mai": [
        "Wat Chedi Luang, Stupa, Chiang Mai",
        "Wat Chedi Luang, Buddhist temple, Chiang Mai",
    ],
    # Broadened to match ANY Roman Amphitheatre exterior upload rather than one specific
    # file -- Commons full-text search result ordering isn't fully stable across runs, so
    # pinning a single exact title meant it sometimes just wasn't in that run's top-15
    # results per suffix and silently fell back to a worse pick ("Chester Rows, Bridge
    # Street", dominated by Hugo Boss/Jigsaw shop windows).
    "Chester": ["Roman Amphitheatre"],
}

MONOCHROME_PATTERN = re.compile(
    r"\b(black\s?and\s?white|b(&|and)w|monochrome|sepia|grayscale|greyscale)\b", re.IGNORECASE
)

# A stitched panorama that couldn't fill its full rectangle often leaves large solid-black
# cutout regions in the corners -- distinguishable from a legitimately dark night photo by
# having near-zero color variance (a night skyline still has texture/lights in its corners).
BORDER_PATCH = 48
BORDER_MEAN_THRESHOLD = 10
BORDER_STDDEV_THRESHOLD = 4


def has_stitching_cutouts(path):
    try:
        with Image.open(path) as im:
            im = im.convert("L")
            w, h = im.size
            corners = [
                (0, 0, min(BORDER_PATCH, w), min(BORDER_PATCH, h)),
                (max(0, w - BORDER_PATCH), 0, w, min(BORDER_PATCH, h)),
                (0, max(0, h - BORDER_PATCH), min(BORDER_PATCH, w), h),
                (max(0, w - BORDER_PATCH), max(0, h - BORDER_PATCH), w, h),
            ]
            flat_black_corners = 0
            for box in corners:
                patch = im.crop(box)
                pixels = list(patch.getdata())
                if not pixels:
                    continue
                mean = sum(pixels) / len(pixels)
                variance = sum((p - mean) ** 2 for p in pixels) / len(pixels)
                stddev = variance ** 0.5
                if mean < BORDER_MEAN_THRESHOLD and stddev < BORDER_STDDEV_THRESHOLD:
                    flat_black_corners += 1
            return flat_black_corners >= 1
    except Exception as exc:
        print(f"    WARNING: could not inspect {path} for stitching artifacts: {exc}", file=sys.stderr)
        return False


BAD_TITLE_PATTERNS = re.compile(
    r"\b(map|logo|flag|coat of arms|icon|diagram|chart|graph|stamp|banknote|coin|screenshot"
    r"|luge|toboggan|go-?kart|karting|roller\s?coaster|theme\s?park|amusement\s?park"
    r"|water\s?park|zip\s?line|model\s?kit|die-?cast|nissan|toyota|honda"
    r"|airport|runway|terminal|airlines?|flight|aircraft|airliner|airbus|boeing"
    r"|atr\d|boarding\s?pass"
    r"|glass\s?plate|daguerreotype|stereograph"
    r"|construction|building\s?site|under\s?construction|gradnj|renovation\s?work"
    r"|equirectangular|\b360\b|spherical\s?panorama|virtual\s?tour)\b",
    re.IGNORECASE,
)


def haversine_km(lat1, lon1, lat2, lon2):
    r = 6371.0
    dlat = radians(lat2 - lat1)
    dlon = radians(lon2 - lon1)
    a = sin(dlat / 2) ** 2 + cos(radians(lat1)) * cos(radians(lat2)) * sin(dlon / 2) ** 2
    return 2 * r * atan2(sqrt(a), sqrt(1 - a))


def slugify(destination: str) -> str:
    city = destination.split(",")[0].strip().lower()
    city = re.sub(r"[^a-z0-9]+", "_", city).strip("_")
    return city


def city_regex(city: str) -> re.Pattern:
    """Word-boundary regex requiring the full city name (spaces or underscores) to appear."""
    escaped = re.escape(city).replace(r"\ ", r"[ _]")
    return re.compile(rf"\b{escaped}\b", re.IGNORECASE)


def is_relevant(page, pattern: re.Pattern) -> bool:
    """A candidate is relevant only if the city name shows up in its title or its categories.

    Commons full-text search also matches uploader usernames, upload locations, and unrelated
    descriptions, which produces confident-looking but wrong hits (e.g. a car called "Skyline"
    photographed in Germany matching a "Chester skyline" search via unrelated metadata). Anchoring
    relevance to the title or the file's own categories filters those out.
    """
    if pattern.search(page.get("title", "")):
        return True
    for cat in page.get("categories", []) or []:
        if pattern.search(cat.get("title", "")):
            return True
    return False


def api_get(session, params):
    params = {**params, "format": "json"}
    for attempt in range(3):
        try:
            resp = session.get(API_URL, params=params, timeout=30)
            resp.raise_for_status()
            return resp.json()
        except requests.RequestException as exc:
            if attempt == 2:
                raise
            time.sleep(1.5 * (attempt + 1))
    return {}


def search_candidates(session, city, suffix):
    """Return imageinfo dicts for files matching `"city" suffix` search."""
    query = f'"{city}" {suffix} filetype:bitmap'
    data = api_get(
        session,
        {
            "action": "query",
            "generator": "search",
            "gsrsearch": query,
            "gsrnamespace": 6,
            "gsrlimit": 30,
            "prop": "imageinfo|categories|coordinates",
            "iiprop": "url|size|mime|extmetadata",
            "cllimit": 50,
            "colimit": 1,
        },
    )
    pages = data.get("query", {}).get("pages", {})
    return list(pages.values())


def rank_candidates(pages, city, coords, shot_type, exclude_urls):
    """Return all qualifying candidates for this shot, best first."""
    pattern = city_regex(city)
    exclude = DISAMBIGUATION_EXCLUDE.get(city)
    landmark_pattern = LANDMARK_PATTERNS.get(city) if shot_type == "landmark" else None
    preferred = PREFERRED_TITLE_SUBSTRINGS.get(city, []) if shot_type == "landmark" else []
    candidates = []
    seen_titles = set()
    rejects = {
        "bad_title": 0, "interior": 0, "not_relevant": 0, "disambiguation": 0,
        "too_far": 0, "already_used": 0, "wrong_mime": 0, "no_size": 0,
        "too_small": 0, "bad_aspect": 0,
    }
    total_pages = 0
    for page in pages:
        total_pages += 1
        title = page.get("title", "")
        if title in seen_titles:
            continue
        seen_titles.add(title)
        if BAD_TITLE_PATTERNS.search(title):
            rejects["bad_title"] += 1
            continue
        if INTERIOR_PATTERN.search(title):
            rejects["interior"] += 1
            continue
        if not is_relevant(page, pattern):
            rejects["not_relevant"] += 1
            continue
        if exclude and (
            exclude.search(title) or any(exclude.search(c.get("title", "")) for c in page.get("categories", []) or [])
        ):
            rejects["disambiguation"] += 1
            continue
        geo = page.get("coordinates")
        if geo and coords:
            lat, lon = geo[0].get("lat"), geo[0].get("lon")
            if lat is not None and lon is not None:
                dist = haversine_km(lat, lon, coords[0], coords[1])
                if dist > MAX_DISTANCE_KM:
                    rejects["too_far"] += 1
                    continue
        is_pref = any(sub.lower() in title.lower() for sub in preferred)
        infos = page.get("imageinfo")
        if not infos:
            continue
        info = infos[0]
        descriptionurl = info.get("descriptionurl")
        if descriptionurl in exclude_urls:
            rejects["already_used"] += 1
            continue
        mime = info.get("mime", "")
        if mime not in ("image/jpeg", "image/png"):
            rejects["wrong_mime"] += 1
            if is_pref:
                print(f"      preferred candidate {title!r} dropped: mime={mime!r}", file=sys.stderr)
            continue
        width = info.get("width", 0)
        height = info.get("height", 0)
        if not width or not height:
            rejects["no_size"] += 1
            continue
        if width < MIN_WIDTH or height < MIN_HEIGHT:
            rejects["too_small"] += 1
            if is_pref:
                print(f"      preferred candidate {title!r} dropped: {width}x{height} below minimum", file=sys.stderr)
            continue
        aspect = width / height
        if aspect < MIN_ASPECT or aspect > MAX_ASPECT:
            rejects["bad_aspect"] += 1
            if is_pref:
                print(f"      preferred candidate {title!r} dropped: aspect {aspect:.2f} out of range", file=sys.stderr)
            continue
        candidate = {
            "title": title,
            "url": info.get("url"),
            "descriptionurl": descriptionurl,
            "width": width,
            "height": height,
            "mime": mime,
            "extmetadata": info.get("extmetadata", {}),
            "good_subject": bool(GOOD_SUBJECT_PATTERN.search(title)),
            "is_color": not bool(MONOCHROME_PATTERN.search(title)),
            "is_named_landmark": bool(landmark_pattern and landmark_pattern.search(title)),
            "is_preferred": is_pref,
        }
        candidate["rank_key"] = (
            candidate["is_preferred"],
            candidate["is_named_landmark"],
            candidate["good_subject"],
            candidate["is_color"],
            candidate["width"] * candidate["height"],
        )
        candidates.append(candidate)
    candidates.sort(key=lambda c: c["rank_key"], reverse=True)
    if not candidates:
        active_rejects = {k: v for k, v in rejects.items() if v}
        print(f"      0 candidates from {total_pages} results; rejections: {active_rejects}", file=sys.stderr)
    return candidates


def find_photos(session, destination, shot_type, exclude_urls):
    city = destination.split(",")[0].strip()
    coords = DESTINATION_COORDS.get(destination)
    if shot_type == "landmark":
        suffixes = NAMED_LANDMARKS.get(city, []) + LANDMARK_FALLBACK_SUFFIXES
    elif shot_type == "lifestyle":
        suffixes = LIFESTYLE_SUFFIXES
    else:
        suffixes = SCENIC_SUFFIXES
    all_pages = []
    for suffix in suffixes:
        all_pages.extend(search_candidates(session, city, suffix))
    return rank_candidates(all_pages, city, coords, shot_type, exclude_urls)


def extract_meta(value_dict, key, default="Unknown"):
    entry = value_dict.get(key)
    if not entry:
        return default
    value = entry.get("value", default)
    # Strip HTML tags that Commons often embeds in Artist/LicenseShortName fields.
    value = re.sub(r"<[^>]+>", "", value).strip()
    return value or default


def license_label(extmetadata):
    short = extract_meta(extmetadata, "LicenseShortName", "")
    if short and short != "Unknown":
        return short
    usage_terms = extract_meta(extmetadata, "UsageTerms", "")
    if usage_terms:
        return usage_terms
    return "Unknown license"


def download_image(session, url, dest_path):
    with session.get(url, stream=True, timeout=60) as resp:
        resp.raise_for_status()
        with open(dest_path, "wb") as f:
            for chunk in resp.iter_content(chunk_size=1 << 16):
                f.write(chunk)


def main():
    os.makedirs(OUTPUT_DIR, exist_ok=True)
    session = requests.Session()
    session.headers.update({"User-Agent": USER_AGENT})

    results = []
    credits_by_destination = {}

    for destination in DESTINATIONS:
        slug = slugify(destination)
        used_urls = set()
        credits_by_destination[destination] = []
        print(f"=== {destination} ===", file=sys.stderr)

        for shot_type in SHOT_TYPES:
            filename = f"{slug}-{shot_type}.jpg"
            print(f"  Searching Commons for: {shot_type} ...", file=sys.stderr)
            try:
                candidates = find_photos(session, destination, shot_type, used_urls)
            except requests.RequestException as exc:
                print(f"    ERROR querying API: {exc}", file=sys.stderr)
                results.append((destination, shot_type, None, None, f"SKIPPED (API error: {exc})"))
                continue

            if not candidates:
                print(f"    No suitable {shot_type} photo found; skipping.", file=sys.stderr)
                results.append((destination, shot_type, None, None, "SKIPPED (no suitable photo found)"))
                continue

            dest_path = os.path.join(OUTPUT_DIR, filename)
            best = None
            for i, candidate in enumerate(candidates[:MAX_CANDIDATES_TRIED]):
                try:
                    download_image(session, candidate["url"], dest_path)
                except requests.RequestException as exc:
                    print(f"    ERROR downloading {candidate['url']}: {exc}", file=sys.stderr)
                    continue
                if has_stitching_cutouts(dest_path):
                    print(
                        f"    candidate #{i + 1} {candidate['title']!r} has stitching cutout borders; trying next",
                        file=sys.stderr,
                    )
                    continue
                best = candidate
                break

            if best is None:
                print(f"    All candidates for {shot_type} failed quality checks; skipping.", file=sys.stderr)
                if os.path.exists(dest_path):
                    os.remove(dest_path)
                results.append((destination, shot_type, None, None, "SKIPPED (no clean candidate passed checks)"))
                continue

            used_urls.add(best["descriptionurl"])
            author = extract_meta(best["extmetadata"], "Artist")
            license_name = license_label(best["extmetadata"])
            resolution = f"{best['width']}x{best['height']}"

            results.append((destination, shot_type, filename, resolution, license_name))
            credits_by_destination[destination].append(
                "\n".join(
                    [
                        f"  [{shot_type}] {filename}",
                        f"  Commons page: {best['descriptionurl']}",
                        f"  Author: {author}",
                        f"  License: {license_name}",
                    ]
                )
            )
            print(f"    Saved {filename} ({resolution}, {license_name})", file=sys.stderr)
            time.sleep(0.5)  # be polite to the API

    with open(CREDITS_PATH, "w") as f:
        any_written = False
        for destination in DESTINATIONS:
            entries = credits_by_destination.get(destination, [])
            if not entries:
                continue
            f.write(f"{destination}\n")
            f.write("\n\n".join(entries))
            f.write("\n\n")
            any_written = True
        if not any_written:
            f.write("No photos were downloaded.\n")

    # Summary table
    print()
    header = f"{'Destination':<26} {'Shot':<10} {'Filename':<24} {'Resolution':<12} {'License'}"
    print(header)
    print("-" * len(header))
    for destination, shot_type, filename, resolution, license_name in results:
        if filename:
            print(f"{destination:<26} {shot_type:<10} {filename:<24} {resolution:<12} {license_name}")
        else:
            print(f"{destination:<26} {shot_type:<10} {'-':<24} {'-':<12} {license_name}")


if __name__ == "__main__":
    main()
