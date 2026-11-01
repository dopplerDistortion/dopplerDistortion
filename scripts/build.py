"""Build the profile README and its panels from goksen.ing.

    python scripts/build.py --site ../goksen.ing   # read the site, then draw
    python scripts/build.py                        # draw from data/site.json

Every panel is an SVG in the site's design system: Geist Mono and Martian
Mono, bone ink, square corners, crop marks, and one red for the one Primary
action. Each SVG carries its own font subset, so GitHub draws it the same
everywhere. Each panel comes twice, for GitHub's dark and light themes; the
backgrounds are transparent and the index has no side walls, so the gaps
GitHub leaves between stacked images (wider on phones) never show.
"""

from __future__ import annotations

import argparse
import base64
import io
import json
import math
import random
import re
from dataclasses import dataclass
from pathlib import Path
from xml.sax.saxutils import escape

from fontTools import subset
from fontTools.ttLib import TTFont

ROOT = Path(__file__).resolve().parent.parent
DATA = ROOT / "data" / "site.json"
OUT = ROOT / "assets"
FONTS = ROOT / "fonts"
SITE = "https://goksen.ing"

# ── tokens (src/app/globals.css) ────────────────────────────────────────────

THEMES = {
    "dark": {
        "fg": "#e6e3de",
        "muted": "#8c8a86",
        "border": "#2a2a2b",
        "mark": "#5c5b58",
        "chip": "#1c1c1d",
        "signal": "#ff3b30",
        "signal_fg": "#0b0b0c",
    },
    # The site is dark only. On GitHub's light theme the inks swap: ink on
    # paper, the same greys darkened to keep their contrast, the same red.
    "light": {
        "fg": "#0b0b0c",
        "muted": "#6b6a66",
        "border": "#d8d6d2",
        "mark": "#8c8a86",
        "chip": "#ecebe8",
        "signal": "#ff3b30",
        "signal_fg": "#0b0b0c",
    },
}

W = 840  # panel width in px; GitHub scales it down on narrow screens
INSET = 8  # room outside the border for the crop marks
PAD = 24  # px-6
X0 = INSET + PAD  # content left
X1 = W - INSET - PAD  # content right
LABEL = 11  # --text-label
TRACK = 0.08  # --tracking-label, em
BOOT_MS = 1200  # --motion-boot
CURSOR_MS = 1000  # --motion-cursor


@dataclass(frozen=True)
class Face:
    family: str
    file: str
    advance: float  # em; every face here is monospaced


MONO = Face("gk-mono", "GeistMono-Regular.ttf", 0.6)
MONO_500 = Face("gk-mono-500", "GeistMono-Medium.ttf", 0.6)
DISPLAY = Face("gk-display", "MartianMono-Light-SemiExpanded.ttf", 0.75)


def width(text: str, size: float, face: Face = MONO, track: float = 0) -> float:
    return len(text) * (size * face.advance + track * size)


def fit(text: str, size: float, room: float, face: Face = MONO) -> str:
    """Truncate with an ellipsis, as the site's `truncate` does."""
    chars = int(room // (size * face.advance))
    if len(text) <= chars:
        return text
    return text[: max(0, chars - 1)].rstrip() + "…"


def wrap(text: str, size: float, room: float, face: Face = MONO) -> list[str]:
    chars = max(1, int(room // (size * face.advance)))
    lines: list[str] = []
    line = ""
    for word in text.split():
        candidate = f"{line} {word}".strip()
        if len(candidate) <= chars:
            line = candidate
        else:
            if line:
                lines.append(line)
            line = word
    if line:
        lines.append(line)
    return lines


# ── SVG ─────────────────────────────────────────────────────────────────────


class Svg:
    """One panel image. Collects the glyphs each face needs while drawing."""

    def __init__(self, height: float, title: str, theme: str):
        self.h = height
        self.title = title
        self.t = THEMES[theme]
        self.parts: list[str] = []
        self.css: list[str] = []
        self.motion: list[str] = []
        self.glyphs: dict[Face, set[str]] = {}

    def text(
        self,
        x: float,
        y: float,
        s: str,
        size: float = 14,
        fill: str = "fg",
        face: Face = MONO,
        anchor: str = "start",
        track: float = 0,
        attrs: str = "",
        upper: bool = False,
    ) -> None:
        s = s.upper() if upper else s
        self.glyphs.setdefault(face, set()).update(s)
        spacing = f' letter-spacing="{track * size:.2f}"' if track else ""
        self.parts.append(
            f'<text x="{x:.1f}" y="{y:.1f}" font-family="{face.family}, ui-monospace, monospace" '
            f'font-size="{size}" fill="{self.t[fill]}" text-anchor="{anchor}"{spacing}{attrs}>'
            f"{escape(s)}</text>"
        )

    def label(self, x: float, y: float, s: str, anchor: str = "start") -> None:
        """`micro-label`: 11 px, uppercase, tracked, muted."""
        self.text(x, y, s, LABEL, "muted", anchor=anchor, track=TRACK, upper=True)

    def raw(self, s: str) -> None:
        self.parts.append(s)

    def rect(self, x, y, w, h, fill="none", stroke=None, attrs="") -> None:
        stroke_attr = f' stroke="{self.t[stroke]}"' if stroke else ""
        fill_attr = self.t.get(fill, fill)
        self.parts.append(
            f'<rect x="{x:.1f}" y="{y:.1f}" width="{w:.1f}" height="{h:.1f}" '
            f'fill="{fill_attr}"{stroke_attr}{attrs}/>'
        )

    def hline(self, y: float, x0: float = INSET, x1: float = W - INSET) -> None:
        self.parts.append(
            f'<path d="M{x0} {y + 0.5}H{x1}" stroke="{self.t["border"]}"/>'
        )

    def vlines(self, y0: float, y1: float) -> None:
        """The panel's two side borders."""
        b = self.t["border"]
        self.parts.append(
            f'<path d="M{INSET + 0.5} {y0}V{y1}M{W - INSET - 0.5} {y0}V{y1}" stroke="{b}"/>'
        )

    def crop(self, corner: str, x: float, y: float) -> None:
        """A crop mark: 10 px, 6 px outside the border's corner."""
        m = self.t["mark"]
        if corner == "tl":
            d = f"M{x - 5.5} {y + 4.5}V{y - 5.5}H{x + 4.5}"
        else:
            d = f"M{x + 5.5} {y - 4.5}V{y + 5.5}H{x - 4.5}"
        self.parts.append(f'<path d="{d}" fill="none" stroke="{m}"/>')

    def panel_top(self, y: float, label: str, right: str | None = None) -> float:
        """Crop mark, top border, and the label row. Returns the row's bottom."""
        self.crop("tl", INSET, y)
        self.hline(y)
        self.label(X0, y + 25, label)
        if right:
            self.label(X1, y + 25, right, anchor="end")
        self.hline(y + 41)
        return y + 42

    def render(self) -> str:
        fonts = "".join(font_face(face, chars) for face, chars in self.glyphs.items())
        motion = "".join(self.motion)
        style = fonts + "".join(self.css)
        if motion:
            style += f"@media (prefers-reduced-motion: no-preference){{{motion}}}"
        return (
            f'<svg xmlns="http://www.w3.org/2000/svg" width="{W}" height="{self.h:.0f}" '
            f'viewBox="0 0 {W} {self.h:.0f}" role="img" aria-label="{escape(self.title)}">'
            f"<title>{escape(self.title)}</title><style>{style}</style>"
            + "".join(self.parts)
            + "</svg>\n"
        )


_subsets: dict[tuple[str, str], str] = {}


def font_face(face: Face, chars: set[str]) -> str:
    """An @font-face with only the glyphs this image draws, as WOFF2."""
    text = "".join(sorted(chars | {" "}))
    key = (face.file, text)
    if key not in _subsets:
        font = TTFont(FONTS / face.file, recalcTimestamp=False)
        options = subset.Options()
        options.flavor = "woff2"
        options.layout_features = ["kern", "liga", "calt", "tnum"]
        options.name_IDs = [0, 1, 2, 13, 14]  # keep copyright and licence
        sub = subset.Subsetter(options)
        sub.populate(text=text)
        sub.subset(font)
        buf = io.BytesIO()
        font.flavor = "woff2"
        font.save(buf)
        _subsets[key] = base64.b64encode(buf.getvalue()).decode()
    return (
        f"@font-face{{font-family:{face.family};"
        f"src:url(data:font/woff2;base64,{_subsets[key]}) format('woff2')}}"
    )


# ── content (read from a goksen.ing checkout) ───────────────────────────────

FENCE = re.compile(r"^(```|~~~)[^\n]*\n[\s\S]*?^\1[^\S\n]*$", re.M)


def frontmatter(path: Path) -> tuple[dict, str]:
    text = path.read_text(encoding="utf-8")
    match = re.match(r"^---\n(.*?)\n---\n(.*)$", text, re.S)
    if not match:
        raise ValueError(f"{path}: no frontmatter")
    data: dict = {}
    parent = None
    for line in match.group(1).splitlines():
        if not line.strip() or line.lstrip().startswith("#"):
            continue
        key, _, value = line.strip().partition(":")
        value = value.strip().strip('"').strip("'")
        if line.startswith((" ", "\t")) and parent:
            data[parent][key] = value
        elif value == "":
            parent = key
            data[key] = {}
        else:
            parent = None
            data[key] = value
    return data, match.group(2)


def reading_minutes(body: str) -> int:
    """src/lib/content/parse.ts: prose words at 200 a minute."""
    prose = FENCE.sub("", body)
    prose = re.sub(r"^(import|export)\s.*$", "", prose, flags=re.M)
    prose = re.sub(r"</?[A-Za-z][^>]*>", "", prose)
    words = [w for w in prose.split() if re.search(r"[^\W_]", w)]
    return max(1, math.ceil(len(words) / 200))


def period(p: dict) -> str:
    start, end = p.get("start", "")[:4], (p.get("end") or "")[:4]
    if not end:
        return f"{start}–"
    return start if start == end else f"{start}–{end}"


def newest(entries: list[dict]) -> list[dict]:
    entries.sort(key=lambda e: e["slug"])
    entries.sort(key=lambda e: e["date"], reverse=True)
    return entries


def folders(path: Path, file: str) -> list[Path]:
    return sorted(
        p / file for p in path.iterdir() if p.is_dir() and not p.name.startswith(("_", ".")) and (p / file).exists()
    )


def read_site(site: Path) -> dict:
    identity_src = (site / "content" / "identity.ts").read_text(encoding="utf-8")

    def field(name: str) -> str:
        return re.search(rf'\b{name}:\s*"([^"]*)"', identity_src).group(1)

    identity = {
        "name": field("name"),
        "role": field("role"),
        "now": field("now"),
        "mail": field("mail"),
        "links": [
            {"label": label, "href": href}
            for label, href in re.findall(r'label:\s*"([^"]+)",\s*href:\s*"([^"]+)"', identity_src)
        ],
    }

    posts = []
    for path in folders(site / "content" / "writing", "index.mdx"):
        data, body = frontmatter(path)
        posts.append(
            {
                "slug": path.parent.name,
                "title": data["title"],
                "summary": data["summary"],
                "date": data["publishedAt"],
                "size": f"{reading_minutes(body):02d} min",
            }
        )

    projects = []
    for path in folders(site / "content" / "work", "index.mdx"):
        data, _ = frontmatter(path)
        projects.append(
            {
                "slug": path.parent.name,
                "title": data["title"],
                "summary": data["summary"],
                "date": data["publishedAt"],
                "size": period(data["period"]),
            }
        )

    experiments = []
    for path in folders(site / "src" / "app" / "lab", "page.mdx"):
        data, _ = frontmatter(path)
        experiments.append(
            {
                "slug": path.parent.name,
                "title": data["title"],
                "summary": data["summary"],
                "date": data["publishedAt"],
                "size": data["status"],
            }
        )

    training = json.loads((site / "content" / "training.json").read_text(encoding="utf-8"))["stats"]

    return {
        "identity": identity,
        "sections": [
            {"name": "writing", "noun": "post", "entries": newest(posts)},
            {"name": "work", "noun": "project", "entries": newest(projects)},
            {"name": "lab", "noun": "experiment", "entries": newest(experiments)},
        ],
        "training": training,
    }


# ── panels ──────────────────────────────────────────────────────────────────


def plural(n: int, noun: str) -> str:
    return f"{n} {noun}{'' if n == 1 else 's'}"


def boot_lines(sections: list[dict]) -> list[str]:
    """`mount /writing ........ 2 posts`, then `ready.` (index-listing/model.ts)."""
    lines = [f"mount /{s['name']} ".ljust(23, ".") + f" {plural(len(s['entries']), s['noun'])}" for s in sections]
    return [*lines, "ready."]


def identity_card(data: dict, theme: str) -> str:
    ident = data["identity"]
    boot = boot_lines(data["sections"])
    first, _, rest = ident["name"].partition(" ")
    name_size = 60
    name_lh = name_size * 0.92

    facts_x = X0 + width(max(first, rest, key=len), name_size, DISPLAY) + 40
    value_x = facts_x + width("x" * 6, 14) + 16
    value_room = X1 - value_x
    site_link = SITE.removeprefix("https://")
    links = "   ".join(f"{l['label']} ↗" for l in ident["links"] if "github.com" not in l["href"])
    facts = [
        ("role", wrap(ident["role"], 14, value_room)),
        ("now", wrap(ident["now"], 14, value_room)),
        ("mail", [ident["mail"]]),
        ("links", [f"{site_link} ↗   {links}".strip()]),
    ]
    facts_h = sum(len(lines) * 20 for _, lines in facts) + 8 * (len(facts) - 1)

    boot_top = INSET + 42 + PAD
    boot_h = (len(boot) + 1) * 18
    name_block = max(2 * name_lh, facts_h)
    height = boot_top + boot_h + PAD + name_block + PAD + INSET

    svg = Svg(height, f"{ident['name']}. {ident['role']} {ident['now']}", theme)
    svg.panel_top(INSET, "identity card", f"{site_link} — index")
    svg.vlines(INSET, height - INSET)
    svg.hline(height - INSET - 1)
    svg.crop("br", W - INSET, height - INSET)

    # Boot log: each line types in its own slot of --motion-boot.
    slot = BOOT_MS / len(boot)
    for i, line in enumerate(boot):
        y = boot_top + 13 + i * 18
        svg.text(X0, y, line, 12, "muted", attrs=f' class="boot b{i}"')
        svg.motion.append(
            f".b{i}{{animation:type {slot:.0f}ms steps({len(line)}) {slot * i:.0f}ms both}}"
        )
    cursor_y = boot_top + 13 + len(boot) * 18
    svg.rect(X0, cursor_y - 11, 7.2, 14, "muted", attrs=' class="cursor"')
    svg.motion.append(
        "@keyframes type{from{clip-path:inset(0 100% 0 0)}to{clip-path:inset(0 0 0 0)}}"
        f"@keyframes blink{{50%{{opacity:0}}}}"
        f".cursor{{animation:blink {CURSOR_MS}ms steps(1) {BOOT_MS}ms 5}}"
    )

    # The name, two lines in display type, settling in by Dissolve.
    bottom = height - INSET - PAD
    rng = random.Random(ident["name"])
    name_top = bottom - 2 * name_lh
    for row, word in enumerate((first, rest)):
        y = name_top + name_size * 0.78 + row * name_lh
        for col, ch in enumerate(word):
            delay = BOOT_MS + rng.randint(0, 300)
            cls = f"d{row}{col}"
            svg.text(
                X0 + col * name_size * DISPLAY.advance, y, ch, name_size, "fg", DISPLAY, attrs=f' class="{cls}"'
            )
            svg.motion.append(f".{cls}{{animation:ink {delay}ms steps(1) both}}")
    svg.motion.append("@keyframes ink{from{opacity:0}to{opacity:1}}")

    # Facts, keys beside values, bottom-aligned with the name.
    y = bottom - facts_h + 14
    for key, lines in facts:
        svg.label(facts_x, y, key)
        for line in lines:
            svg.text(value_x, y, line, 14)
            y += 20
        y += 8
    return svg.render()


ROW_H = 28


def index_rows(data: dict) -> list[dict]:
    """The rows of `ls /`: each section, its newest three, then `+N more`."""
    rows = []
    for i, section in enumerate(data["sections"], start=1):
        entries = section["entries"]
        name = section["name"]
        rows.append(
            {
                "kind": "dir",
                "href": f"{SITE}/{name}",
                "name": f"{name}/",
                "summary": f"{section['noun']}s" if entries else "nothing yet",
                "meta": [f"{len(entries):02d}", entries[0]["date"] if entries else "—"],
                "key": str(i),
                "file": f"row-{name}",
            }
        )
        for entry in entries[:3]:
            rows.append(
                {
                    "kind": "entry",
                    "href": f"{SITE}/{name}/{entry['slug']}",
                    "name": entry["title"],
                    "summary": entry["summary"],
                    "meta": [entry["date"], entry["size"]],
                    "file": f"row-{name}-{entry['slug']}",
                }
            )
        hidden = len(entries) - 3
        if hidden > 0:
            rows.append(
                {
                    "kind": "more",
                    "href": f"{SITE}/{name}",
                    "name": f"+{hidden} more in {name}/",
                    "meta": [],
                    "file": f"row-{name}-more",
                }
            )
    pages = [
        ("about", "/about", "4", "the longer story"),
        ("system", "/system", "5", "the design system, in public"),
        ("feed.xml", "/feed.xml", None, "writing, as RSS"),
    ]
    for j, (name, path, key, summary) in enumerate(pages):
        rows.append(
            {
                "kind": "file",
                "href": f"{SITE}{path}",
                "name": name,
                "summary": summary,
                "meta": [],
                "key": key,
                "file": f"row-page-{name.replace('.', '-')}",
                "gap": j == 0,
            }
        )
    return rows


def index_head(total: int, theme: str) -> str:
    h = INSET + 42 + 8
    svg = Svg(h, f"ls / — {total} entries", theme)
    svg.panel_top(INSET, "ls /", f"{total} entries")
    return svg.render()


def index_row(row: dict, theme: str) -> str:
    h = ROW_H + (12 if row.get("gap") else 0)
    svg = Svg(h, f"{row['name']}{' — ' + row['summary'] if row.get('summary') else ''}", theme)
    y = h - ROW_H + 19

    # Meta, right-aligned in columns, then the key in a kbd chip.
    right = X1
    if row.get("key"):
        svg.rect(right - 20, y - 14, 20, 20, "chip")
        svg.text(right - 10, y, row["key"], 12, "muted", MONO_500, anchor="middle")
    right -= 20 + 16
    for item in reversed(row["meta"]):
        svg.text(right, y, item, 14, "muted", anchor="end")
        right -= width(item, 14) + 16

    # Marker column (2ch), then name and summary, truncated to fit.
    x = X0 + width("> ", 14) + 12
    if row["kind"] in ("entry", "more"):
        x += 16
    svg.text(X0, y, ">", 14, "muted", attrs=' class="marker"')
    svg.css.append(".marker{opacity:0}")
    face = MONO_500 if row["kind"] == "dir" else MONO
    room = right - x
    name = fit(row["name"], 14, room)
    svg.text(x, y, name, 14, "muted" if row["kind"] == "more" else "fg", face)
    if row.get("summary"):
        sx = x + width(name, 14) + 12
        if right - sx > 14 * 4:
            svg.text(sx, y, fit(row["summary"], 14, right - sx), 14, "muted")
    return svg.render()


def index_foot(theme: str) -> str:
    h = 8 + 41 + INSET
    svg = Svg(h, "Every row opens on goksen.ing", theme)
    svg.hline(8)
    svg.label(X0, 8 + 25, "click a row to open it on goksen.ing")
    svg.label(X1, 8 + 25, SITE.removeprefix("https://") + " ↗", anchor="end")
    svg.hline(h - INSET - 1)
    svg.crop("br", W - INSET, h - INSET)
    return svg.render()


def training_log(t: dict, theme: str) -> str:
    totals = t["totals"]
    facts = [
        ("workouts", f"{totals['workouts']:,}"),
        ("this year", f"{t['thisYear']:,}"),
        ("streak", plural(t["streakWeeks"], "week")),
        ("last", t["last"]),
        ("moved", f"{totals['tonnes']:,} t"),
        ("time", f"{totals['hours']:,} h"),
    ]
    top = INSET + 42 + PAD
    facts_h = 16 + 44 + 16
    chart_top = top + facts_h + 32
    chart_h = 214
    lifts = t.get("lifts", [])[:3]
    lifts_top = chart_top + chart_h + 32
    lifts_h = (16 + len(lifts) * 37) if lifts else 0
    footer_top = lifts_top + lifts_h + PAD
    height = footer_top + 41 + INSET

    svg = Svg(
        height,
        f"Training log: {totals['workouts']} workouts since {totals['since']}, "
        f"a {t['streakWeeks']}-week streak, as of {t['asOf']}.",
        theme,
    )
    svg.panel_top(INSET, "training log", f"as of {t['asOf']}")
    svg.vlines(INSET, height - INSET)

    # Six facts between two rules.
    svg.hline(top, X0, X1)
    col = (X1 - X0 + 24) / len(facts)
    for i, (label, value) in enumerate(facts):
        x = X0 + i * col
        svg.label(x, top + 16 + 13, label)
        svg.text(x, top + 16 + 13 + 22, value, 14)
    svg.hline(top + facts_h, X0, X1)

    # Workouts per week: bars on one baseline.
    split = X0 + (X1 - X0) * 0.6 - 20
    weeks = t["weeks"]
    svg.label(X0, chart_top + 11, f"per week · last {len(weeks)}")
    peak = max([1, *(w["workouts"] for w in weeks)])
    bar_top, base = chart_top + 28, chart_top + 28 + 120
    gap = 2
    bw = (split - X0 - gap * (len(weeks) - 1)) / len(weeks)
    for i, w in enumerate(weeks):
        x = X0 + i * (bw + gap)
        if w["workouts"] == 0:
            svg.rect(x, base - 1, bw, 1, "border")
        else:
            bh = (w["workouts"] / peak) * (base - bar_top)
            svg.rect(x, base - bh, bw, bh, "fg")
    svg.hline(base, X0, split)
    svg.text(X0, base + 20, weeks[0]["start"], 12, "muted")
    svg.text(split, base + 20, "this week", 12, "muted", anchor="end")

    # Focus: one axis per muscle area, this week filled, last week dashed.
    fx0 = split + 40
    svg.label(fx0, chart_top + 11, "focus · working sets")
    focus = t["focus"]
    cx, cy, r = (fx0 + X1) / 2, chart_top + 28 + 72, 58
    fpeak = max([1, *(v for f in focus for v in (f["thisWeek"], f["lastWeek"]))])

    def pt(i: int, rad: float) -> tuple[float, float]:
        a = -math.pi / 2 + i * 2 * math.pi / len(focus)
        return cx + rad * math.cos(a), cy + rad * math.sin(a)

    def poly(values, rad_of):
        return " ".join(f"{x:.1f},{y:.1f}" for x, y in (pt(i, rad_of(v)) for i, v in enumerate(values)))

    border, fg, muted = svg.t["border"], svg.t["fg"], svg.t["muted"]
    for share in (1 / 3, 2 / 3, 1):
        svg.raw(f'<polygon points="{poly(focus, lambda _: share * r)}" fill="none" stroke="{border}"/>')
    for i, f in enumerate(focus):
        x, y = pt(i, r)
        svg.raw(f'<path d="M{cx:.1f} {cy:.1f}L{x:.1f} {y:.1f}" stroke="{border}"/>')
        lx, ly = pt(i, r + 12)
        anchor = "middle" if abs(lx - cx) < 1 else ("start" if lx > cx else "end")
        ly += 4 + (-4 if ly < cy - 1 else 4 if ly > cy + 1 else 0)
        label = f"{f['focus']} {f['thisWeek']}·{f['lastWeek']}"
        svg.text(lx, ly, label, 11, "fg", anchor=anchor)
    svg.raw(
        f'<polygon points="{poly([f["lastWeek"] for f in focus], lambda v: v / fpeak * r)}" '
        f'fill="none" stroke="{muted}" stroke-dasharray="3 3"/>'
    )
    svg.raw(
        f'<polygon points="{poly([f["thisWeek"] for f in focus], lambda v: v / fpeak * r)}" '
        f'fill="{fg}" fill-opacity="0.15" stroke="{fg}"/>'
    )
    this_week = sum(f["thisWeek"] for f in focus)
    last_week = sum(f["lastWeek"] for f in focus)
    legend_y = cy + r + 46
    legend = f"this week {this_week}"
    legend2 = f"┄ last week {last_week}"
    lw = 14 + width(legend, 12) + 24 + width(legend2, 12)
    lx = cx - lw / 2
    svg.rect(lx, legend_y - 9, 9, 9, "fg")
    svg.text(lx + 14, legend_y, legend, 12, "muted")
    svg.text(lx + 14 + width(legend, 12) + 24, legend_y, legend2, 12, "muted")

    # Top lifts.
    if lifts:
        svg.label(X0, lifts_top + 11, "top lifts · same weeks")
        for i, lift in enumerate(lifts):
            y = lifts_top + 16 + i * 37 + 24
            kg = lambda v: f"{v:g} kg"
            svg.text(X1, y, f"{lift['sessions']}×", 14, "muted", anchor="end")
            e1rm = f"e1rm {kg(lift['e1rm'])}"
            svg.text(X1 - 60, y, e1rm, 14, "muted", anchor="end")
            best = f"{kg(lift['best']['kg'])} × {lift['best']['reps']}"
            svg.text(X1 - 60 - width(e1rm, 14) - 24, y, best, 14, anchor="end")
            svg.text(X0, y, fit(lift["title"].lower(), 14, 360), 14)
            if i < len(lifts) - 1:
                svg.hline(y + 12, X0, X1)

    svg.hline(footer_top)
    svg.label(X0, footer_top + 25, "source hevy")
    svg.label(X0 + width("source hevy", LABEL, track=TRACK) + 24, footer_top + 25, f"since {totals['since']}")
    svg.hline(height - INSET - 1)
    svg.crop("br", W - INSET, height - INSET)
    return svg.render()


def button(label: str, theme: str, variant: str) -> str:
    """A Button at the site's sizes, scaled up a step for the README."""
    size, h, px = 13, 36, 14
    w = width(label, size) + 2 * px
    t = THEMES[theme]
    svg = Svg(h, label.removesuffix(" ↗"), theme)
    global_w = w
    if variant == "signal":
        svg.rect(0, 0, w, h, t["signal"])
        svg.text(px, 23, label, size, "signal_fg", MONO_500)
    else:
        svg.rect(0.5, 0.5, w - 1, h - 1, "none", "muted")
        svg.text(px, 23, label, size, "fg", MONO_500)
    out = svg.render()
    return out.replace(f'width="{W}"', f'width="{global_w:.0f}"', 1).replace(
        f'viewBox="0 0 {W} ', f'viewBox="0 0 {global_w:.0f} ', 1
    )


# ── README ──────────────────────────────────────────────────────────────────


def picture(name: str, alt: str, width_attr: str = "100%") -> str:
    dark = f"assets/{name}-dark.svg"
    light = f"assets/{name}-light.svg"
    return (
        f'<picture><source media="(prefers-color-scheme: dark)" srcset="{dark}">'
        f'<source media="(prefers-color-scheme: light)" srcset="{light}">'
        f'<img src="{dark}" alt="{escape(alt, {chr(34): "&quot;"})}" width="{width_attr}" align="top"></picture>'
    )


def write(name: str, draw) -> None:
    for theme in THEMES:
        (OUT / f"{name}-{theme}.svg").write_text(draw(theme), encoding="utf-8")


def build(data: dict) -> None:
    OUT.mkdir(exist_ok=True)
    for old in OUT.glob("*.svg"):
        old.unlink()
    ident = data["identity"]

    write("identity", lambda th: identity_card(data, th))

    linkedin = next((l["href"] for l in ident["links"] if "linkedin" in l["href"]), None)
    buttons = [("open", "goksen.ing ↗", SITE, "signal"), ("mail", "mail", f"mailto:{ident['mail']}", "outline")]
    if linkedin:
        buttons.append(("linkedin", "linkedin ↗", linkedin, "outline"))
    buttons.append(("rss", "rss ↗", f"{SITE}/feed.xml", "outline"))
    action_html = []
    for name, label, href, variant in buttons:
        write(f"button-{name}", lambda th, l=label, v=variant: button(l, th, v))
        w = width(label, 13) + 28
        action_html.append(f'<a href="{href}">{picture(f"button-{name}", label.removesuffix(" ↗"), f"{w:.0f}")}</a>')

    rows = index_rows(data)
    total = sum(len(s["entries"]) for s in data["sections"])
    write("index-head", lambda th: index_head(total, th))
    row_html = []
    for row in rows:
        write(row["file"], lambda th, r=row: index_row(r, th))
        alt = row["name"] + (f" — {row['summary']}" if row.get("summary") else "")
        row_html.append(f'<a href="{row["href"]}">{picture(row["file"], alt)}</a>')
    write("index-foot", index_foot)

    write("training", lambda th: training_log(data["training"], th))

    readme = "\n".join(
        [
            "<!-- Generated by scripts/build.py from goksen.ing. Edit the site, not this file. -->",
            f'<a href="{SITE}">{picture("identity", ident["name"] + ". " + ident["role"] + " " + ident["now"])}</a>',
            "",
            "<p>" + " ".join(action_html) + "</p>",
            "",
            "<p>"
            + picture("index-head", f"ls / — {total} entries")
            + "<br>"
            + "<br>".join(row_html)
            + "<br>"
            + picture("index-foot", "Every row opens on goksen.ing")
            + "</p>",
            "",
            f'<a href="{SITE}">{picture("training", "Training log from Hevy, as on goksen.ing")}</a>',
            "",
            f'<sub>Drawn from the <a href="{SITE}/system">goksen.ing design system</a> by '
            f"<code>scripts/build.py</code>; it rebuilds from the site every day.</sub>",
            "",
        ]
    )
    (ROOT / "README.md").write_text(readme, encoding="utf-8")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--site", type=Path, help="a goksen.ing checkout to read")
    args = parser.parse_args()
    if args.site:
        data = read_site(args.site)
        DATA.parent.mkdir(exist_ok=True)
        DATA.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    data = json.loads(DATA.read_text(encoding="utf-8"))
    build(data)


if __name__ == "__main__":
    main()
