#!/usr/bin/env python3
"""Build the profile README's stat cards from live GitHub data.

Fetches the owner's public repositories, aggregates the per-language byte counts
that GitHub Linguist reports, and renders two self-contained SVG cards plus the
"featured projects" table that is spliced into README.md between HTML markers.

Everything is stdlib: the workflow needs no pip install, and the profile page
depends on no third-party service to render.
"""

from __future__ import annotations

import functools
import hashlib
import itertools
import json
import math
import os
import re
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
ASSETS = ROOT / "assets"
README = ROOT / "README.md"
API = "https://api.github.com"

CARD_WIDTH = 860
PAD = 24


# --------------------------------------------------------------------------- #
# configuration (all overridable from the workflow)
# --------------------------------------------------------------------------- #

def env_str(name: str, default: str = "") -> str:
    return (os.environ.get(name) or default).strip()


def env_flag(name: str, default: bool) -> bool:
    raw = env_str(name)
    if not raw:
        return default
    return raw.lower() in {"1", "true", "yes", "on"}


def env_int(name: str, default: int) -> int:
    try:
        return int(env_str(name) or default)
    except ValueError:
        return default


def env_list(name: str) -> list[str]:
    return [item.strip() for item in env_str(name).split(",") if item.strip()]


USER = env_str("GH_USER") or env_str("GITHUB_REPOSITORY_OWNER")
TOKEN = env_str("GH_TOKEN") or env_str("GITHUB_TOKEN")

INCLUDE_FORKS = env_flag("INCLUDE_FORKS", False)
INCLUDE_ARCHIVED = env_flag("INCLUDE_ARCHIVED", True)
EXCLUDE_REPOS = {name.lower() for name in env_list("EXCLUDE_REPOS")}
EXCLUDE_LANGS = {name.lower() for name in env_list("EXCLUDE_LANGS")}
FEATURED_PINS = env_list("FEATURED_REPOS")
MAX_LANGS = env_int("MAX_LANGS", 6)
MIN_LANG_SHARE = float(env_str("MIN_LANG_SHARE", "0.5"))
FEATURED_COUNT = env_int("FEATURED_COUNT", 6)
STATIC_TILE_VALUE = env_str("STATIC_TILE_VALUE")
STATIC_TILE_LABEL = env_str("STATIC_TILE_LABEL", "Experience")


# --------------------------------------------------------------------------- #
# GitHub API
# --------------------------------------------------------------------------- #

def api_get(path: str, *, params: dict | None = None) -> tuple[object, dict]:
    """GET an API path, retrying the transient failures a scheduled job will hit."""
    url = path if path.startswith("http") else f"{API}{path}"
    if params:
        url += "?" + urllib.parse.urlencode(params)

    headers = {
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
        "User-Agent": f"{USER}-profile-stats",
    }
    if TOKEN:
        headers["Authorization"] = f"Bearer {TOKEN}"

    last_error: Exception | None = None
    for attempt in range(4):
        try:
            request = urllib.request.Request(url, headers=headers)
            with urllib.request.urlopen(request, timeout=30) as response:
                return json.loads(response.read().decode()), dict(response.headers)
        except urllib.error.HTTPError as error:
            # 404 on an optional endpoint is an answer, not a failure to retry.
            if error.code in (403, 429) and attempt < 3:
                last_error = error
                time.sleep(2 ** attempt * 2)
                continue
            raise
        except (urllib.error.URLError, TimeoutError) as error:
            last_error = error
            if attempt < 3:
                time.sleep(2 ** attempt * 2)
                continue
            raise
    raise RuntimeError(f"giving up on {url}: {last_error}")


def api_paged(path: str, *, params: dict | None = None) -> list:
    """Follow the Link header until the last page."""
    items: list = []
    page = 1
    while True:
        payload, headers = api_get(path, params={**(params or {}), "per_page": 100, "page": page})
        if not isinstance(payload, list) or not payload:
            break
        items.extend(payload)
        if 'rel="next"' not in headers.get("Link", ""):
            break
        page += 1
        if page > 20:  # a profile with >2000 repos is not what this renders
            break
    return items


# --------------------------------------------------------------------------- #
# colour: Linguist hues, stepped for each surface
#
# Language colour is identity, not rank, so the Linguist hue every GitHub user
# already associates with a language is the starting point. Raw Linguist values
# are not chart-safe though: PowerShell's #012456 is all but invisible on a dark
# canvas, and neighbouring segments can land too close to separate under colour
# vision deficiency. Each hue is therefore re-stepped per surface -- pulled into
# that mode's OKLCH lightness band, lifted to clear a 3:1 contrast floor, and
# nudged apart where an adjacent pair falls under the CVD separation target.
# Hue and chroma are left alone, so Python still reads blue and Vue still green.
# --------------------------------------------------------------------------- #

LIGHTNESS_BAND = {"light": (0.43, 0.77), "dark": (0.46, 0.72)}
CVD_TARGET = 8.0          # OKLab dE x100 between adjacent slots, min(protan, deutan)
NORMAL_FLOOR = 15.0       # OKLab dE x100 between adjacent slots, unsimulated vision
CONTRAST_MIN = 3.0        # WCAG ratio of a mark against its surface

# How far a slot may travel from its Linguist colour. Unbounded, the optimiser
# maximises separation by turning Vue dark green and JavaScript olive -- it wins
# the metric and loses the reason for using Linguist hues at all. Where a hue set
# cannot separate within this budget, the answer is fewer slots, not more drift.
MAX_DRIFT = 15.0          # OKLab dE x100 from the Linguist colour
# Chroma is the second lever. Hue is never touched: it is what makes a Linguist
# colour recognisable, and rotating it would simply invent a new language colour.
CHROMA_STEPS = (0.75, 0.9, 1.0, 1.15, 1.3)

# Machado, Oliveira & Fernandes (2009), severity 1.0, applied in linear RGB.
MACHADO = {
    "protan": ((0.152286, 1.052583, -0.204868),
               (0.114503, 0.786281, 0.099216),
               (-0.003882, -0.048116, 1.051998)),
    "deutan": ((0.367322, 0.860646, -0.227968),
               (0.280085, 0.672501, 0.047413),
               (-0.011820, 0.042940, 0.968881)),
}

THEME = {
    "light": {
        "surface": "#ffffff",
        "border": "#d1d9e0",
        "fg": "#1f2328",
        "muted": "#59636e",
        "track": "#eff2f5",
        "accent": "#0969da",
    },
    "dark": {
        "surface": "#0d1117",
        "border": "#3d444d",
        "fg": "#f0f6fc",
        "muted": "#9198a1",
        "track": "#21262d",
        "accent": "#4493f8",
    },
}

OTHER_HUE = {"light": "#8c959f", "dark": "#7d8590"}

# Linguist hues for the languages a profile is likely to surface. Anything not
# listed falls back to a deterministic hue derived from the language name, so a
# new language never renders colourless.
LANGUAGE_COLORS = {
    "assembly": "#6e4c13", "astro": "#ff5a03", "batchfile": "#c1f12e", "blade": "#f7523f",
    "c": "#555555", "c#": "#178600", "c++": "#f34b7d", "clojure": "#db5855",
    "cmake": "#da3434", "coffeescript": "#244776", "css": "#663399", "cython": "#fedf5b",
    "dart": "#00b4ab", "dockerfile": "#384d54", "elixir": "#6e4a7e", "elm": "#60b5cc",
    "emacs lisp": "#c065db", "erlang": "#b83998", "f#": "#b845fc", "fortran": "#4d41b1",
    "go": "#00add8", "gradle": "#02303a", "groovy": "#4298b8", "haskell": "#5e5086",
    "hcl": "#844fba", "html": "#e34c26", "java": "#b07219", "javascript": "#f1e05a",
    "json": "#292929", "jsonnet": "#0064bd", "julia": "#a270ba", "jupyter notebook": "#da5b0b",
    "kotlin": "#a97bff", "less": "#1d365d", "lua": "#000080", "makefile": "#427819",
    "markdown": "#083fa1", "matlab": "#e16737", "nix": "#7e7eff", "objective-c": "#438eff",
    "ocaml": "#ef7a08", "perl": "#0298c3", "php": "#4f5d95", "powershell": "#012456",
    "prolog": "#74283c", "python": "#3572a5", "r": "#198ce7", "roff": "#ecdebe",
    "ruby": "#701516", "rust": "#dea584", "sass": "#a53b70", "scala": "#c22d40",
    "scss": "#c6538c", "shell": "#89e051", "smarty": "#f0c040", "solidity": "#aa6746",
    "sql": "#e38c00", "svelte": "#ff3e00", "swift": "#f05138", "tex": "#3d6117",
    "twig": "#c1d026", "typescript": "#3178c6", "vba": "#867db1", "vbscript": "#15dcdc",
    "vim script": "#199f4b", "visual basic .net": "#945db7", "vue": "#41b883",
    "xml": "#0060ac", "xslt": "#eb8ceb", "yaml": "#cb171e", "zig": "#ec915c",
}


@functools.lru_cache(maxsize=8192)
def hex_to_srgb(value: str) -> tuple[float, float, float]:
    value = value.strip().lstrip("#")
    return tuple(int(value[i:i + 2], 16) / 255 for i in (0, 2, 4))


def srgb_to_hex(rgb) -> str:
    return "#" + "".join(f"{round(max(0.0, min(1.0, c)) * 255):02x}" for c in rgb)


def to_linear(c: float) -> float:
    return c / 12.92 if c <= 0.04045 else ((c + 0.055) / 1.055) ** 2.4


def from_linear(c: float) -> float:
    c = max(0.0, min(1.0, c))
    return 12.92 * c if c <= 0.0031308 else 1.055 * c ** (1 / 2.4) - 0.055


@functools.lru_cache(maxsize=8192)
def linear_rgb(value: str) -> tuple[float, float, float]:
    return tuple(to_linear(c) for c in hex_to_srgb(value))


def relative_luminance(value: str) -> float:
    r, g, b = linear_rgb(value)
    return 0.2126 * r + 0.7152 * g + 0.0722 * b


def contrast_ratio(a: str, b: str) -> float:
    high, low = sorted((relative_luminance(a), relative_luminance(b)), reverse=True)
    return (high + 0.05) / (low + 0.05)


def cbrt(value: float) -> float:
    return math.copysign(abs(value) ** (1 / 3), value)


def linear_to_oklab(rgb) -> tuple[float, float, float]:
    r, g, b = rgb
    l = cbrt(0.4122214708 * r + 0.5363325363 * g + 0.0514459929 * b)
    m = cbrt(0.2119034982 * r + 0.6806995451 * g + 0.1073969566 * b)
    s = cbrt(0.0883024619 * r + 0.2817188376 * g + 0.6299787005 * b)
    return (
        0.2104542553 * l + 0.7936177850 * m - 0.0040720468 * s,
        1.9779984951 * l - 2.4285922050 * m + 0.4505937099 * s,
        0.0259040371 * l + 0.7827717662 * m - 0.8086757660 * s,
    )


def oklab_to_linear(lab) -> tuple[float, float, float]:
    L, a, b = lab
    l = (L + 0.3963377774 * a + 0.2158037573 * b) ** 3
    m = (L - 0.1055613458 * a - 0.0638541728 * b) ** 3
    s = (L - 0.0894841775 * a - 1.2914855480 * b) ** 3
    return (
        4.0767416621 * l - 3.3077115913 * m + 0.2309699292 * s,
        -1.2684380046 * l + 2.6097574011 * m - 0.3413193965 * s,
        -0.0041960863 * l - 0.7034186147 * m + 1.7076147010 * s,
    )


def oklab(value: str) -> tuple[float, float, float]:
    return linear_to_oklab(linear_rgb(value))


def oklch(value: str) -> tuple[float, float, float]:
    L, a, b = oklab(value)
    return L, math.hypot(a, b), math.atan2(b, a)


def oklch_to_hex(L: float, C: float, h: float) -> str:
    """Back to sRGB, shrinking chroma until the colour is inside the gamut."""
    for _ in range(48):
        rgb = oklab_to_linear((L, C * math.cos(h), C * math.sin(h)))
        if all(-0.0005 <= c <= 1.0005 for c in rgb):
            break
        C *= 0.94
    return srgb_to_hex(tuple(from_linear(c) for c in rgb))


@functools.lru_cache(maxsize=8192)
def simulate(value: str, kind: str) -> tuple[float, float, float]:
    r, g, b = linear_rgb(value)
    m = MACHADO[kind]
    return tuple(
        max(0.0, min(1.0, m[i][0] * r + m[i][1] * g + m[i][2] * b)) for i in range(3)
    )


def delta_e(a: str, b: str, kind: str | None = None) -> float:
    """Euclidean distance in OKLab, x100, optionally through a CVD simulation."""
    lab_a = linear_to_oklab(simulate(a, kind) if kind else linear_rgb(a))
    lab_b = linear_to_oklab(simulate(b, kind) if kind else linear_rgb(b))
    return 100 * math.dist(lab_a, lab_b)


def cvd_separation(a: str, b: str) -> float:
    return min(delta_e(a, b, "protan"), delta_e(a, b, "deutan"))


def language_hue(name: str) -> str:
    known = LANGUAGE_COLORS.get(name.lower())
    if known:
        return known
    # Deterministic fallback: a mid-lightness, mid-chroma hue keyed off the name.
    digest = int(hashlib.sha256(name.lower().encode()).hexdigest()[:8], 16)
    return oklch_to_hex(0.60, 0.13, (digest % 360) * math.pi / 180)


# A large filled segment may sit below the 3:1 contrast target and still read
# clearly -- but only where the value is also reachable without colour. The card
# ships a full legend with percentages and the README carries a table view, so
# the relief rule applies and anything between these two floors is a warning
# rather than a rewrite. Below RELIEF_FLOOR a segment starts to dissolve into
# the surface, so that one is enforced.
RELIEF_FLOOR = 1.6

def step_for_surface(hues: list[str], mode: str, neutral: frozenset[int] = frozenset()) -> list[str]:
    """Re-step Linguist hues for one surface: band, contrast, then separation.

    Slots in `neutral` may move in lightness but keep their chroma -- the "Other"
    grey has to stay grey, or a reader takes it for one more language.
    """
    low, high = LIGHTNESS_BAND[mode]
    surface = THEME[mode]["surface"]
    lighten = relative_luminance(surface) < 0.5

    anchors: list[str] = []
    for hue in hues:
        L, C, h = oklch(hue)
        L = min(high, max(low, L))
        color = oklch_to_hex(L, C, h)
        for _ in range(60):
            if contrast_ratio(color, surface) >= RELIEF_FLOOR:
                break
            L += 0.012 if lighten else -0.012
            if not 0.03 < L < 0.97:
                break
            color = oklch_to_hex(L, C, h)
        anchors.append(color)

    return _separate_slots(anchors, mode, neutral)


def pair_score(a: str, b: str) -> float:
    """How well two slots separate, as a fraction of what each check demands.

    1.0 means the pair clears both the CVD target and the normal-vision floor;
    below 1.0 names how far short the weaker of the two falls.
    """
    return min(cvd_separation(a, b) / CVD_TARGET, delta_e(a, b) / NORMAL_FLOOR)


def _scores(colors: list[str]) -> list[float]:
    """Every pair, ascending -- the legend shows all slots at once, so a reader
    matches any swatch against any other, not just against its neighbour."""
    return sorted(pair_score(a, b) for a, b in itertools.combinations(colors, 2))


def _candidates(anchor: str, mode: str, chroma_steps: tuple[float, ...]) -> list[str]:
    """The colours a slot may take: its own hue, re-stepped in lightness and
    chroma, inside the band, clearing contrast, and within the drift budget.

    Candidates are always derived from the anchor rather than from the slot's
    current value, so a sweep cannot compound its own chroma and wander off.
    """
    low, high = LIGHTNESS_BAND[mode]
    surface = THEME[mode]["surface"]
    L, C, h = oklch(anchor)

    options = []
    for step in range(-14, 15):
        candidate_L = L + step * 0.015
        if not low <= candidate_L <= high:
            continue
        for factor in chroma_steps:
            color = oklch_to_hex(candidate_L, C * factor, h)
            if (contrast_ratio(color, surface) >= RELIEF_FLOOR
                    and delta_e(color, anchor) <= MAX_DRIFT
                    and color not in options):
                options.append(color)
    return options or [anchor]


def _separate_slots(anchors: list[str], mode: str, neutral: frozenset[int]) -> list[str]:
    """Spread slots until every pair separates, by coordinate descent.

    Scored maximin over the whole pair vector: a move is kept only when the
    ascending score vector improves lexicographically. Chasing the single worst
    pair instead will ping-pong a slot squeezed between two others, and scoring
    only neighbours leaves same-hue slots -- Python and PowerShell are both
    Linguist blues -- indistinguishable in the legend.
    """
    out = list(anchors)
    if len(out) < 2:
        return out

    options = {
        index: _candidates(anchor, mode, (1.0,) if index in neutral else CHROMA_STEPS)
        for index, anchor in enumerate(anchors)
    }

    best = _scores(out)
    for _ in range(8):
        improved = False
        for index, choices in options.items():
            for color in choices:
                if color == out[index]:
                    continue
                trial = list(out)
                trial[index] = color
                score = _scores(trial)
                if score > best:
                    out, best, improved = trial, score, True
        if not improved:
            break

    return out


def palette_report(names: list[str], palette: dict[str, list[str]]) -> list[str]:
    """Report the checks that are computable from colour alone."""
    lines = []
    for mode, colors in palette.items():
        surface = THEME[mode]["surface"]
        low, high = LIGHTNESS_BAND[mode]

        off_band = [n for n, c in zip(names, colors) if not low - 5e-3 <= oklch(c)[0] <= high + 5e-3]
        relief = [f"{n} {contrast_ratio(c, surface):.2f}:1"
                  for n, c in zip(names, colors) if contrast_ratio(c, surface) < CONTRAST_MIN]
        pairs = list(itertools.combinations(colors, 2))
        worst_cvd = min((cvd_separation(a, b) for a, b in pairs), default=99.0)
        worst_normal = min((delta_e(a, b) for a, b in pairs), default=99.0)

        lines.append(f"  [{mode}] {len(colors)} slots on {surface}")
        lines.append(f"    lightness band  : {'ok' if not off_band else 'outside: ' + ', '.join(off_band)}")
        lines.append(f"    all-pairs CVD dE: {worst_cvd:.1f} (target {CVD_TARGET:.0f})"
                     + ("" if worst_cvd >= CVD_TARGET else "  WARN - legend labels carry identity"))
        lines.append(f"    normal-vision dE: {worst_normal:.1f} (floor {NORMAL_FLOOR:.0f})"
                     + ("" if worst_normal >= NORMAL_FLOOR else "  FAIL"))
        lines.append(f"    contrast        : {'all >= 3:1' if not relief else 'relief rule: ' + ', '.join(relief)}")
    return lines


# --------------------------------------------------------------------------- #
# data
# --------------------------------------------------------------------------- #

def collect() -> dict:
    if not USER:
        sys.exit("set GH_USER (or run inside Actions, where GITHUB_REPOSITORY_OWNER is set)")

    profile, _ = api_get(f"/users/{USER}")
    repos = api_paged(f"/users/{USER}/repos", params={"type": "owner", "sort": "pushed"})

    counted = [
        repo for repo in repos
        if (INCLUDE_FORKS or not repo.get("fork"))
        and (INCLUDE_ARCHIVED or not repo.get("archived"))
        and repo["name"].lower() not in EXCLUDE_REPOS
    ]

    languages: dict[str, int] = {}
    for repo in counted:
        # The size on a listing (and especially on a search result) can lag badly
        # -- reBSGO reported 25 KB through search against 43,490 KB here -- so the
        # counted repos get their size, stars and forks from the repo endpoint,
        # which is authoritative.
        detail, _ = api_get(f"/repos/{repo['full_name']}")
        for field in ("size", "stargazers_count", "forks_count"):
            if field in detail:
                repo[field] = detail[field]

        payload, _ = api_get(f"/repos/{repo['full_name']}/languages")
        for name, size in (payload or {}).items():
            if name.lower() in EXCLUDE_LANGS:
                continue
            languages[name] = languages.get(name, 0) + int(size)

    return {
        "profile": profile,
        "repos": repos,
        "counted": counted,
        "languages": dict(sorted(languages.items(), key=lambda kv: -kv[1])),
        "stars": sum(r.get("stargazers_count", 0) for r in counted),
        "forks": sum(r.get("forks_count", 0) for r in counted),
        "repo_bytes": sum(r.get("size", 0) for r in counted) * 1024,
        "repo_sizes": {r["name"]: r.get("size", 0) * 1024 for r in counted},
    }


def language_shares(languages: dict[str, int], max_langs: int = 0) -> list[dict]:
    """Top languages by byte share, with the tail folded into a single slot."""
    total = sum(languages.values())
    if not total:
        return []

    max_langs = max_langs or MAX_LANGS
    shares = [{"name": n, "bytes": b, "share": 100 * b / total} for n, b in languages.items()]
    head = []
    for share in shares[:max_langs]:
        if share["share"] < MIN_LANG_SHARE:
            break
        head.append(share)
    tail = shares[len(head):]

    if tail:
        head.append({
            "name": "Other",
            "bytes": sum(s["bytes"] for s in tail),
            "share": sum(s["share"] for s in tail),
            "other": True,
            "count": len(tail),
        })
    return head


def build_palette(languages: dict[str, int]) -> tuple[list[dict], dict[str, list[str]], str]:
    """Pick the most detailed slot count whose colours actually separate.

    Linguist hues are fixed, and some of them collide: Python, PowerShell and
    Visual Basic .NET are all blues and purples. Where stepping lightness cannot
    pull a set apart, the honest move is fewer slots rather than a legend of
    swatches a reader cannot tell apart -- the folded languages keep their exact
    share in the README's table, so no number is lost.
    """
    attempts = []
    for count in range(MAX_LANGS, 2, -1):
        shares = language_shares(languages, count)
        neutral = frozenset(i for i, s in enumerate(shares) if s.get("other"))
        palette = {
            mode: step_for_surface(
                [OTHER_HUE[mode] if s.get("other") else language_hue(s["name"]) for s in shares],
                mode, neutral)
            for mode in ("light", "dark")
        }
        worst = min(min(_scores(colors)) for colors in palette.values())
        attempts.append((shares, palette, worst, count))
        if worst >= 1.0:
            return shares, palette, f"{len(shares)} slots separate cleanly"
        if len(shares) <= 3:
            break

    shares, palette, worst, count = max(attempts, key=lambda a: a[2])
    return shares, palette, (f"{len(shares)} slots, best achievable separation "
                             f"{worst * 100:.0f}% of target - legend labels carry identity")


def featured(data: dict) -> list[dict]:
    """The repositories the profile leads with: pinned by name, else most starred."""
    pool = [
        repo for repo in data["repos"]
        if not repo.get("fork")
        and repo["name"].lower() != (USER or "").lower()
        and repo["name"].lower() not in EXCLUDE_REPOS
    ]
    if FEATURED_PINS:
        wanted = {name.lower(): index for index, name in enumerate(FEATURED_PINS)}
        picked = [r for r in pool if r["name"].lower() in wanted]
        picked.sort(key=lambda r: wanted[r["name"].lower()])
        return picked[:FEATURED_COUNT]

    pool.sort(key=lambda r: (r.get("stargazers_count", 0), r.get("pushed_at", "")), reverse=True)
    return pool[:FEATURED_COUNT]


# --------------------------------------------------------------------------- #
# rendering
#
# One file per card, carrying both modes as CSS custom properties. GitHub serves
# README images through an <img>, so a relative path renders on every branch and
# fork without rewriting -- and the media query inside the file picks the steps
# that were selected for that surface.
# --------------------------------------------------------------------------- #

FONT = ('-apple-system, BlinkMacSystemFont, "Segoe UI", "Noto Sans", '
        "Helvetica, Arial, sans-serif")


_NARROW = set("ijlt.,:;'!|()[]{} /\\")
_WIDE = set("MW@mw")


def text_width(text: str, size: float) -> float:
    """Approximate rendered width, for deciding what fits before drawing it."""
    units = 0.0
    for char in text:
        if char in _NARROW:
            units += 0.32
        elif char in _WIDE:
            units += 0.92
        elif char.isupper() or char.isdigit():
            units += 0.62
        else:
            units += 0.55
    return units * size


def esc(text: str) -> str:
    return (str(text).replace("&", "&amp;").replace("<", "&lt;")
            .replace(">", "&gt;").replace('"', "&quot;"))


def compact(value: int) -> str:
    if value >= 1_000_000:
        return f"{value / 1_000_000:.1f}M".replace(".0M", "M")
    if value >= 10_000:
        return f"{value / 1_000:.1f}K".replace(".0K", "K")
    return f"{value:,}"


def humanize_bytes(value: int) -> str:
    for unit, size in (("GB", 1024 ** 3), ("MB", 1024 ** 2), ("KB", 1024)):
        if value >= size:
            return f"{value / size:,.1f} {unit}".replace(".0 ", " ")
    return f"{value} B"


def ink_on(fill: str) -> str:
    """Pick whichever label colour actually contrasts better against the fill.

    A fixed luminance threshold gets mid-tones wrong: on the stepped JavaScript
    yellow and the neutral grey it chose white at under 3:1, where ink reads at
    over 6:1.
    """
    return max(("#ffffff", "#10161d"), key=lambda ink: contrast_ratio(ink, fill))


def theme_vars(extra: dict[str, dict[str, str]] | None = None) -> str:
    """Emit the light defaults and the dark block, both selected per surface."""
    extra = extra or {}

    def block(mode: str) -> str:
        values = dict(THEME[mode])
        values.update({key: modes[mode] for key, modes in extra.items()})
        return "".join(f"--{key}:{value};" for key, value in values.items())

    return (
        f"  svg{{{block('light')}}}\n"
        f"  @media (prefers-color-scheme: dark){{svg{{{block('dark')}}}}}\n"
    )


def document(width: int, height: int, title: str, desc: str, style: str, body: str) -> str:
    return f"""<svg xmlns="http://www.w3.org/2000/svg" width="{width}" height="{height}"
     viewBox="0 0 {width} {height}" role="img" aria-labelledby="t d" fill="none">
  <title id="t">{esc(title)}</title>
  <desc id="d">{esc(desc)}</desc>
  <style>
{style}  .card{{fill:var(--surface);stroke:var(--border)}}
  text{{font-family:{FONT};dominant-baseline:auto}}
  .h{{font-size:15px;font-weight:600;fill:var(--fg)}}
  .sub{{font-size:11.5px;fill:var(--muted)}}
  .key{{font-size:12px;fill:var(--fg)}}
  .val{{font-size:12px;fill:var(--muted);font-variant-numeric:tabular-nums}}
  .stat{{font-size:26px;font-weight:600;fill:var(--fg)}}
  .lbl{{font-size:11.5px;fill:var(--muted)}}
  .seg{{font-size:10.5px;font-weight:600}}
  </style>
  <rect class="card" x=".5" y=".5" width="{width - 1}" height="{height - 1}" rx="11.5"/>
{body}
</svg>
"""


def languages_card(shares: list[dict], palette: dict[str, list[str]],
                   repo_count: int, total_bytes: int) -> str:
    inner = CARD_WIDTH - 2 * PAD
    bar_y, bar_h = 88, 22
    legend_top, row_h, columns = 134, 26, 3
    rows = math.ceil(len(shares) / columns)
    height = legend_top + rows * row_h + 6

    style = theme_vars({
        f"c{i}": {"light": palette["light"][i], "dark": palette["dark"][i]}
        for i in range(len(shares))
    } | {
        f"i{i}": {"light": ink_on(palette["light"][i]), "dark": ink_on(palette["dark"][i])}
        for i in range(len(shares))
    })

    spoken = ", ".join(f"{s['name']} {s['share']:.1f}%" for s in shares)
    body = [
        f'  <text class="h" x="{PAD}" y="44">Language distribution</text>',
        f'  <text class="sub" x="{PAD}" y="64">{len(shares)} slots across {repo_count} '
        f'public repositories \u00b7 {esc(humanize_bytes(total_bytes))} of source</text>',
        '  <clipPath id="bar">'
        f'<rect x="{PAD}" y="{bar_y}" width="{inner}" height="{bar_h}" '
        f'rx="{bar_h / 2}"/></clipPath>',
        f'  <rect x="{PAD}" y="{bar_y}" width="{inner}" height="{bar_h}" rx="{bar_h / 2}" '
        'fill="var(--track)"/>',
        '  <g clip-path="url(#bar)">',
    ]

    cursor = 0.0
    for index, share in enumerate(shares):
        width = inner * share["share"] / 100
        last = index == len(shares) - 1
        # A 2px gap in the surface colour separates touching segments; the final
        # segment runs to the edge so the bar keeps its rounded end.
        drawn = max(1.0, width if last else width - 2)
        body.append(f'    <rect x="{PAD + cursor:.2f}" y="{bar_y}" width="{drawn:.2f}" '
                    f'height="{bar_h}" fill="var(--c{index})"/>')
        # Direct-label only where the text fits inside the segment with padding.
        label = f"{share['share']:.0f}%"
        if drawn >= text_width(label, 10.5) + 22:
            body.append(f'    <text class="seg" x="{PAD + cursor + drawn / 2:.2f}" '
                        f'y="{bar_y + bar_h / 2 + 3.6:.1f}" text-anchor="middle" '
                        f'fill="var(--i{index})">{label}</text>')
        cursor += width

    body.append("  </g>")

    column_width = inner / columns
    labels = [s["name"] + (f" ({s['count']})" if s.get("other") else "") for s in shares]
    # One value column per legend column, placed just past that column's longest
    # name so the percentage always reads as belonging to the name on its left.
    name_span = [
        max((text_width(labels[i], 12) for i in range(c, len(labels), columns)), default=0)
        for c in range(columns)
    ]
    for index, share in enumerate(shares):
        column, row = index % columns, index // columns
        x = PAD + column * column_width
        y = legend_top + row * row_h
        value_x = x + 18 + min(name_span[column] + 52, column_width - 42)
        body += [
            f'  <circle cx="{x + 5:.1f}" cy="{y - 4:.1f}" r="5" fill="var(--c{index})"/>',
            f'  <text class="key" x="{x + 18:.1f}" y="{y}">{esc(labels[index])}</text>',
            f'  <text class="val" x="{value_x:.1f}" y="{y}" '
            f'text-anchor="end">{share["share"]:.1f}%</text>',
        ]

    return document(CARD_WIDTH, int(height), "Language distribution",
                    f"Share of code by language: {spoken}.", style, "\n".join(body))


def placeholder_card(title: str, message: str, height: int = 132) -> str:
    """Card chrome with no data, so the profile reads as intentional before the
    first workflow run rather than showing two broken images."""
    body = [
        f'  <text class="h" x="{PAD}" y="44">{esc(title)}</text>',
        f'  <text class="sub" x="{PAD}" y="68">{esc(message)}</text>',
        f'  <rect x="{PAD}" y="88" width="{CARD_WIDTH - 2 * PAD}" height="22" rx="11" '
        'fill="var(--track)"/>',
    ]
    return document(CARD_WIDTH, height, title, message, theme_vars(), "\n".join(body))


def stats_card(tiles: list[tuple[str, str]], subtitle: str) -> str:
    height = 152
    inner = CARD_WIDTH - 2 * PAD
    column_width = inner / len(tiles)

    body = [
        f'  <text class="h" x="{PAD}" y="44">GitHub at a glance</text>',
        f'  <text class="sub" x="{PAD}" y="64">{esc(subtitle)}</text>',
    ]
    for index, (value, label) in enumerate(tiles):
        x = PAD + index * column_width
        if index:
            body.append(f'  <line x1="{x:.1f}" y1="84" x2="{x:.1f}" y2="130" '
                        'stroke="var(--border)" stroke-width="1"/>')
        body += [
            f'  <text class="stat" x="{x + 20:.1f}" y="112">{esc(value)}</text>',
            f'  <text class="lbl" x="{x + 20:.1f}" y="130">{esc(label)}</text>',
        ]

    spoken = "; ".join(f"{label}: {value}" for value, label in tiles)
    return document(CARD_WIDTH, height, "GitHub at a glance", spoken,
                    theme_vars(), "\n".join(body))


# --------------------------------------------------------------------------- #
# README splicing
# --------------------------------------------------------------------------- #

def stamp(text: str, versions: dict[str, str]) -> str:
    """Point each card's <img> at its current content hash."""
    def swap(match: re.Match) -> str:
        path = match.group(1)
        version = versions.get(path)
        return f'src="{path}?v={version}"' if version else match.group(0)

    return re.sub(r'src="(assets/[A-Za-z0-9_-]+\.svg)(?:\?v=[0-9a-f]+)?"', swap, text)


def write_card(name: str, svg: str) -> str:
    """Write a card and return the hash of what was written."""
    (ASSETS / name).write_text(svg, encoding="utf-8")
    return hashlib.sha1(svg.encode()).hexdigest()[:8]


def splice(text: str, marker: str, payload: str) -> str:
    start, end = f"<!-- {marker}:START -->", f"<!-- {marker}:END -->"
    pattern = re.compile(re.escape(start) + r".*?" + re.escape(end), re.DOTALL)
    if not pattern.search(text):
        sys.exit(f"README.md is missing the {start} / {end} markers")
    return pattern.sub(f"{start}\n{payload}\n{end}", text)


def md_cell(value: str) -> str:
    return re.sub(r"\s+", " ", str(value or "")).replace("|", "\\|").strip()


def projects_table(repos: list[dict]) -> str:
    if not repos:
        return "_No public repositories yet._"

    cells = []
    for repo in repos:
        facts = []
        if repo.get("language"):
            facts.append(f"`{md_cell(repo['language'])}`")
        if repo.get("stargazers_count"):
            facts.append(f"&#9733; {repo['stargazers_count']}")
        if repo.get("forks_count"):
            count = repo["forks_count"]
            facts.append(f"{count} fork" + ("s" if count != 1 else ""))
        if repo.get("archived"):
            facts.append("_archived_")

        description = md_cell(repo.get("description"))
        cells.append(
            f"**[{md_cell(repo['name'])}]({repo['html_url']})**<br>"
            + (f"<sub>{description}</sub><br>" if description else "")
            + f"<sub>{' &#183; '.join(facts)}</sub>"
        )

    rows = ["|  |  |", "| :-- | :-- |"]
    for index in range(0, len(cells), 2):
        pair = cells[index:index + 2]
        if len(pair) == 1:
            pair.append("&nbsp;")
        rows.append(f"| {pair[0]} | {pair[1]} |")
    return "\n".join(rows)


def language_table(languages: dict[str, int]) -> str:
    """Every language, not just the charted slots.

    The card folds its tail into "Other" so the colours stay separable; this is
    where the folded languages keep their own exact share, so the chart never
    gates a number.
    """
    total = sum(languages.values()) or 1
    rows = ["| Language | Share | Bytes |", "| :-- | --: | --: |"]
    for name, size in languages.items():
        rows.append(f"| {md_cell(name)} | {100 * size / total:.1f}% | {size:,} |")
    return "\n".join(rows)


# --------------------------------------------------------------------------- #

def main() -> int:
    data = collect()
    if not data["languages"]:
        sys.exit("no language bytes reported for any repository")

    shares, palette, verdict = build_palette(data["languages"])

    print(f"languages: {len(data['languages'])} across {len(data['counted'])} repositories")
    print(f"palette: {verdict}")
    print("palette checks:")
    for line in palette_report([s["name"] for s in shares], palette):
        print(line)

    total_bytes = sum(data["languages"].values())
    profile = data["profile"]

    tiles = [(compact(profile.get("public_repos", len(data["repos"]))), "Public repos")]
    if data["stars"]:
        tiles.append((compact(data["stars"]), "Stars earned"))
    if data["forks"]:
        tiles.append((compact(data["forks"]), "Forks"))
    tiles.append((str(len(data["languages"])), "Languages"))
    if STATIC_TILE_VALUE:
        tiles.append((STATIC_TILE_VALUE, STATIC_TILE_LABEL))
    if profile.get("followers"):
        tiles.append((compact(profile["followers"]), "Followers"))

    since = (profile.get("created_at") or "")[:4]
    subtitle = f"Public repositories owned by @{USER}" + (f" \u00b7 on GitHub since {since}" if since else "")

    ASSETS.mkdir(exist_ok=True)
    versions = {
        "assets/languages.svg": write_card(
            "languages.svg", languages_card(shares, palette, len(data["counted"]), total_bytes)),
        "assets/stats.svg": write_card("stats.svg", stats_card(tiles[:6], subtitle)),
    }
    (ASSETS / "data.json").write_text(
        json.dumps({
            "user": USER,
            "repositories": len(data["counted"]),
            "stars": data["stars"],
            "forks": data["forks"],
            "source_bytes": total_bytes,
            "repo_bytes": data["repo_bytes"],
            "repo_sizes": data["repo_sizes"],
            "languages": data["languages"],
        }, indent=2) + "\n", encoding="utf-8")

    readme = README.read_text(encoding="utf-8")
    readme = splice(readme, "PROJECTS", projects_table(featured(data)))
    readme = splice(readme, "LANGTABLE", language_table(data["languages"]))
    readme = stamp(readme, versions)
    README.write_text(readme, encoding="utf-8")

    print("wrote assets/languages.svg, assets/stats.svg, assets/data.json and README.md")
    return 0


def write_placeholders() -> int:
    ASSETS.mkdir(exist_ok=True)
    message = "Populated by the profile-stats workflow on its next run."
    (ASSETS / "stats.svg").write_text(
        placeholder_card("GitHub at a glance", message), encoding="utf-8")
    (ASSETS / "languages.svg").write_text(
        placeholder_card("Language distribution", message), encoding="utf-8")
    print("wrote placeholder cards")
    return 0


if __name__ == "__main__":
    sys.exit(write_placeholders() if "--placeholder" in sys.argv else main())
