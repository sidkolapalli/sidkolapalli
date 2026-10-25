#!/usr/bin/env python3
"""Build the profile README's images and generated sections.

Renders the instrument-cluster banner, the road-line divider and the drive log
as self-contained animated SVGs (Day and Night, wide and phone), refreshes the
writing list from the site's RSS feed, and rewrites the marked sections of
README.md. Run daily by .github/workflows/refresh-profile.yml.
"""

from __future__ import annotations

import argparse
import base64
import io
import json
import math
import os
import re
import urllib.request
from datetime import datetime, timezone
from functools import lru_cache
from html import escape
from pathlib import Path
from xml.etree import ElementTree as ET

ROOT = Path(__file__).resolve().parent.parent
FONT_DIR = ROOT / "scripts" / "fonts"
ASSETS = ROOT / "assets"
README = ROOT / "README.md"
FEED_URL = "https://sidkolapalli.com/feed.xml"
GRAPHQL_URL = "https://api.github.com/graphql"

# Family key -> (CSS family name, file under FONT_DIR, weight range).
FONTS = {
    "serif": ("Newsreader", "newsreader/newsreader-latin-opsz-normal.woff2", "400 600"),
    "sans": ("Instrument Sans", "instrument-sans/instrument-sans-latin-wght-normal.woff2", "400 700"),
    "mono": ("Martian Mono", "martian-mono/martian-mono-latin-wght-normal.woff2", "400 500"),
}
STACKS = {
    "serif": "Newsreader, Georgia, serif",
    "sans": "'Instrument Sans', system-ui, sans-serif",
    "mono": "'Martian Mono', ui-monospace, monospace",
}

# Graphite tokens from the Lab Report design system.
THEMES = {
    "day": {"pg": "#ececeb", "pl": "#e1e1df", "ink": "#18191a", "mu": "#5c5f62", "lr": "#d3d3d0", "la": "#3d4a57", "ok": "#3f6b4f"},
    "night": {"pg": "#0e0f10", "pl": "#1a1b1d", "ink": "#f1f2f4", "mu": "#b2bbc3", "lr": "#2c302f", "la": "#b7c3cf", "ok": "#a9c7a0"},
}
MARK, MARK_INK = "#253948", "#f4f1f6"
EASE = "cubic-bezier(.2, .8, .2, 1)"
MONTHS = "Jan Feb Mar Apr May Jun Jul Aug Sep Oct Nov Dec".split()
TOPICS = ["SIMULATION", "MBSE", "ADAS", "POWERTRAIN", "AI"]

# Gauge faces are drawn in a 240-unit box: a 240-degree arc of radius 96.
ARC = "M 36.86 168 A 96 96 0 1 1 203.14 168"
ARC_LENGTH = 402.12

# A top-down car, 32 x 16 units, nose pointing right.
CAR = (
    '<rect x="6" y="0.5" width="5" height="2.5" rx="1" fill="{ink}"/>'
    '<rect x="21" y="0.5" width="5" height="2.5" rx="1" fill="{ink}"/>'
    '<rect x="6" y="13" width="5" height="2.5" rx="1" fill="{ink}"/>'
    '<rect x="21" y="13" width="5" height="2.5" rx="1" fill="{ink}"/>'
    '<rect x="1" y="2" width="30" height="12" rx="4" fill="{ink}"/>'
    '<rect x="18" y="3.5" width="5" height="9" rx="1.5" fill="{pg}"/>'
    '<rect x="6" y="4" width="3.5" height="8" rx="1" fill="{pg}"/>'
)


# Data ---------------------------------------------------------------------


def weekly_totals(calendar):
    """Sum each week of a GitHub contributionCalendar, keeping its first date."""
    weeks = []
    for week in calendar["weeks"]:
        days = week["contributionDays"]
        if days:
            weeks.append({"start": days[0]["date"], "total": sum(d["contributionCount"] for d in days)})
    return weeks


def drive_log_stats(weeks):
    totals = [w["total"] for w in weeks]
    total = sum(totals)
    peak = max(totals, default=0)
    return {
        "total": total,
        "peak": peak,
        "peak_index": totals.index(peak) if totals else 0,
        "average": round(total / len(totals), 1) if totals else 0.0,
        "quiet": totals.count(0),
        "weeks": len(totals),
    }


def trace_points(values, x0, x1, top, base):
    """Place weekly totals on a plot; the scale rounds up to a multiple of ten."""
    scale = max(10, math.ceil(max(values, default=0) / 10) * 10)
    step = (x1 - x0) / max(len(values) - 1, 1)
    points = [(round(x0 + i * step, 1), round(base - v / scale * (base - top), 1)) for i, v in enumerate(values)]
    return points, scale


def month_marks(weeks, every=1):
    """(week index, month name) where a new month starts, skipping the ragged ends."""
    marks, prev = [], None
    for i, week in enumerate(weeks):
        month = int(week["start"][5:7]) - 1
        if prev is not None and month != prev and i <= len(weeks) - 3 and month % every == 0:
            marks.append((i, MONTHS[month]))
        prev = month
    return marks


def parse_feed(xml_text):
    """Items of an RSS feed in feed order, keeping only https links."""
    items = []
    for item in ET.fromstring(xml_text).iter("item"):
        link = (item.findtext("link") or "").strip()
        if not link.startswith("https://"):
            continue
        items.append(
            {
                "title": " ".join((item.findtext("title") or "").split()),
                "link": link,
                "category": (item.findtext("category") or "").strip(),
            }
        )
    return items


# README sections ------------------------------------------------------------


def replace_between(text, name, body):
    start, end = f"<!-- {name}:START -->", f"<!-- {name}:END -->"
    pattern = re.compile(re.escape(start) + ".*?" + re.escape(end), re.S)
    if not pattern.search(text):
        raise ValueError(f"README is missing the {name} markers")
    return pattern.sub(lambda _: f"{start}\n{body}\n{end}", text, count=1)


def writing_markdown(items, limit=5):
    lines = []
    for item in items[:limit]:
        title = item["title"].replace("[", r"\[").replace("]", r"\]")
        link = item["link"].replace("(", "%28").replace(")", "%29")
        prefix = f"<sub>{escape(item['category'])}</sub> · " if item["category"] else ""
        lines.append(f"- {prefix}[{title}]({link})")
    return "\n".join(lines)


def picture(name, alt):
    """An image that follows the viewer's theme and swaps to its phone layout on narrow screens."""
    return "\n".join(
        [
            "<picture>",
            f'  <source media="(max-width: 600px) and (prefers-color-scheme: dark)" srcset="assets/{name}-phone-night.svg">',
            f'  <source media="(max-width: 600px)" srcset="assets/{name}-phone-day.svg">',
            f'  <source media="(prefers-color-scheme: dark)" srcset="assets/{name}-night.svg">',
            f'  <img src="assets/{name}-day.svg" width="100%" alt="{escape(alt)}">',
            "</picture>",
        ]
    )


def drive_log_markdown(stats, updated):
    if stats is None:
        return picture("drive-log", "Drive log: waiting for the first daily run.")
    return picture(
        "drive-log",
        f"Drive log: {stats['total']:,} contributions in the last 12 months. Peak week {stats['peak']}, "
        f"average {stats['average']} a week, {stats['quiet']} quiet weeks. Updated {updated}.",
    )


# Fonts ----------------------------------------------------------------------


@lru_cache(maxsize=None)
def subset_woff2(path, text):
    """A WOFF2 holding only the glyphs `text` needs, with the font's notices kept."""
    from fontTools import subset
    from fontTools.ttLib import TTFont

    options = subset.Options()
    options.flavor = "woff2"
    options.layout_features = ["*"]
    options.name_IDs = ["*"]
    options.name_languages = ["*"]
    options.notdef_outline = True
    # Keep the source timestamp so unchanged images stay byte-identical between runs.
    font = TTFont(str(path), recalcTimestamp=False)
    subsetter = subset.Subsetter(options)
    subsetter.populate(text=text)
    subsetter.subset(font)
    out = io.BytesIO()
    font.flavor = "woff2"
    font.save(out)
    return out.getvalue()


class Doc:
    """One SVG's text, tracked per family so only the glyphs it uses get embedded."""

    def __init__(self):
        self.used = {family: set() for family in FONTS}

    def text(self, family, value):
        self.used[family].update(value)
        return escape(value, quote=False)

    def font_css(self):
        rules = []
        for family, chars in self.used.items():
            if not chars:
                continue
            name, file, weight = FONTS[family]
            data = base64.b64encode(subset_woff2(FONT_DIR / file, "".join(sorted(chars)))).decode("ascii")
            rules.append(
                f"@font-face{{font-family:'{name}';src:url(data:font/woff2;base64,{data}) format('woff2');"
                f"font-weight:{weight};font-style:normal}}"
            )
        return "".join(rules)


# SVG pieces -----------------------------------------------------------------


def t(doc, family, value, x, y, size, fill, *, weight=400, anchor="start", tracking=0, cls="", delay=None):
    extra = ""
    if anchor != "start":
        extra += f' text-anchor="{anchor}"'
    if tracking:
        extra += f' letter-spacing="{tracking}"'
    if cls:
        extra += f' class="{cls}"'
    if delay is not None:
        extra += f' style="animation-delay:{delay}s"'
    return (
        f'<text x="{x}" y="{y}" font-family="{STACKS[family]}" font-size="{size}" font-weight="{weight}" '
        f'fill="{fill}"{extra}>{doc.text(family, value)}</text>'
    )


def lines(doc, family, rows, x, y, leading, size, fill, *, weight=400, cls="", delay=None):
    spans = "".join(f'<tspan x="{x}" y="{y + i * leading}">{doc.text(family, row)}</tspan>' for i, row in enumerate(rows))
    extra = f' class="{cls}"' if cls else ""
    if delay is not None:
        extra += f' style="animation-delay:{delay}s"'
    return (
        f'<text font-family="{STACKS[family]}" font-size="{size}" font-weight="{weight}" fill="{fill}"{extra}>'
        f"{spans}</text>"
    )


def reduced(rules):
    return f"@media (prefers-reduced-motion: reduce){{{rules}}}"


def rise_css(distance):
    return (
        f".rise{{animation:rise .9s {EASE} both;transform-box:fill-box;transform-origin:left bottom}}"
        f"@keyframes rise{{from{{opacity:0;transform:translateY({distance}px) scale(.98)}}to{{opacity:1;transform:none}}}}"
        "@keyframes fade{from{opacity:0}to{opacity:1}}"
    )


CLUSTER_CSS = (
    f".arc{{animation:draw .9s {EASE} .5s both}}@keyframes draw{{from{{stroke-dashoffset:{ARC_LENGTH}}}}}"
    ".sweep,.cycle{transform-origin:120px 120px}"
    f".sweep{{animation:sweep 1.5s {EASE} .9s both}}"
    "@keyframes sweep{0%,100%{transform:rotate(0)}47%{transform:rotate(240deg)}}"
    f".cycle{{animation:cycle 12s {EASE} 2.4s infinite both}}"
    "@keyframes cycle{0%,18%{transform:rotate(0)}20%,38%{transform:rotate(60deg)}40%,58%{transform:rotate(120deg)}"
    "60%,78%{transform:rotate(180deg)}80%,98%{transform:rotate(240deg)}100%{transform:rotate(360deg)}}"
    ".topic{opacity:0;animation:topic 12s linear infinite both}"
    "@keyframes topic{0%{opacity:0}1%,19%{opacity:1}20%,100%{opacity:0}}"
    ".ready{animation:fade .4s ease-out 2.4s both}"
)
REDUCED_CSS = ".rise{animation:fade .4s ease-out both}.arc,.sweep{animation:none}.cycle{animation-timing-function:step-end}"


def polar(radius, angle):
    a = math.radians(angle)
    return 120 + radius * math.sin(a), 120 - radius * math.cos(a)


def dial(doc, c, kind, x, y, size, *, label_px, readout_px, caption_px, readout_y, caption_y, delay):
    """A gauge face: the topic dial steps through TOPICS, the speedometer rests at READY."""
    if kind == "topic":
        marks = [(-120 + 15 * i, i % 4 == 0) for i in range(17)]
        numbers = [(-120 + 60 * i, f"{i + 1:02d}") for i in range(5)]
        caption = "TOPIC"
    else:
        marks = [(-120 + 12 * i, i % 2 == 0) for i in range(21)]
        numbers = [(-120 + v * 1.2, str(v)) for v in range(0, 201, 40)]
        caption = "KM/H"
    parts = [
        f'<g class="rise" style="animation-delay:{delay}s">',
        f'<svg x="{x}" y="{y}" width="{size}" height="{size}" viewBox="0 0 240 240" overflow="visible">',
        f'<circle cx="120" cy="120" r="116" fill="{c["pl"]}" stroke="{c["lr"]}"/>',
        f'<path d="{ARC}" fill="none" stroke="{c["lr"]}" stroke-width="2" stroke-linecap="round"/>',
        f'<path class="arc" d="{ARC}" fill="none" stroke="{c["ink"]}" stroke-width="2" stroke-linecap="round" '
        f'stroke-dasharray="{ARC_LENGTH}"/>',
    ]
    for angle, major in marks:
        x1, y1 = polar(80 if major else 89, angle)
        x2, y2 = polar(96, angle)
        parts.append(
            f'<line x1="{x1:.2f}" y1="{y1:.2f}" x2="{x2:.2f}" y2="{y2:.2f}" stroke="{c["ink"] if major else c["mu"]}" '
            f'stroke-width="{2 if major else 1}" stroke-linecap="round"/>'
        )
    for angle, value in numbers:
        lx, ly = polar(64, angle)
        parts.append(t(doc, "mono", value, f"{lx:.2f}", f"{ly + label_px * 0.35:.2f}", label_px, c["mu"], weight=500, anchor="middle"))
    cycle = ' class="cycle"' if kind == "topic" else ""
    parts += [
        f'<g class="sweep"><g{cycle}><g transform="rotate(-120 120 120)">'
        f'<line x1="120" y1="136" x2="120" y2="42" stroke="{c["la"]}" stroke-width="3" stroke-linecap="round"/></g></g></g>',
        f'<circle cx="120" cy="120" r="8" fill="{c["ink"]}"/>',
        f'<circle cx="120" cy="120" r="3" fill="{c["pl"]}"/>',
    ]
    if kind == "topic":
        for i, name in enumerate(TOPICS):
            parts.append(
                t(doc, "mono", f"{i + 1:02d} · {name}", 120, readout_y, readout_px, c["la"], weight=500, anchor="middle",
                  cls="topic", delay=round(2.4 + 2.4 * i, 1))
            )
    else:
        width = 5 * readout_px * 0.68 + 14
        start = 120 - width / 2
        parts.append(
            f'<g class="ready"><circle cx="{start + 4:.1f}" cy="{readout_y - readout_px * 0.33:.1f}" r="{readout_px * 0.28:.1f}" fill="{c["ok"]}"/>'
            + t(doc, "mono", "READY", f"{start + 14:.1f}", readout_y, readout_px, c["la"], weight=500)
            + "</g>"
        )
    parts.append(t(doc, "mono", caption, 120, caption_y, caption_px, c["mu"], anchor="middle", tracking=round(caption_px * 0.1, 2)))
    parts.append("</svg></g>")
    return "".join(parts)


def svg_document(doc, width, height, title, desc, css, body, *, stretch=False):
    aspect = ' preserveAspectRatio="none"' if stretch else ""
    return (
        f'<svg xmlns="http://www.w3.org/2000/svg" width="{width}" height="{height}" viewBox="0 0 {width} {height}"{aspect} '
        f'role="img" aria-labelledby="title desc">'
        f'<title id="title">{escape(title, quote=False)}</title><desc id="desc">{escape(desc, quote=False)}</desc>'
        f"<style>{doc.font_css()}{css}</style>{''.join(body)}</svg>\n"
    )


def frame(c, width, height):
    return f'<rect x="0.5" y="0.5" width="{width - 1}" height="{height - 1}" rx="12" fill="{c["pg"]}" stroke="{c["lr"]}"/>'


# Renderers ------------------------------------------------------------------


def render_banner(theme, layout="wide"):
    c, doc = THEMES[theme], Doc()
    title = "Siddharth Kolapalli: automotive systems and applied AI"
    desc = (
        "Hi, I’m Sid. Every article on sidkolapalli.com started as a problem I ran into on a project. "
        "An instrument cluster: a topic dial for Simulation, MBSE, ADAS, Powertrain and AI, and a speedometer reading READY."
    )
    if layout == "wide":
        width, height = 1200, 360
        body = [
            frame(c, width, height),
            '<g class="rise">',
            f'<circle cx="82" cy="74" r="18" fill="{MARK}"/>',
            t(doc, "sans", "sk", 82, 79, 14, MARK_INK, weight=700, anchor="middle", tracking=-0.28),
            t(doc, "sans", "Siddharth Kolapalli", 110, 80, 18, c["ink"], weight=700, tracking=-0.36),
            "</g>",
            t(doc, "mono", "Automotive systems · applied AI", 64, 170, 13, c["la"], weight=500, cls="rise", delay=0.12),
            t(doc, "serif", "Hi, I’m Sid.", 62, 228, 58, c["ink"], weight=500, tracking=-1.04, cls="rise", delay=0.22),
            lines(doc, "serif", ["Every article on sidkolapalli.com started as a", "problem I ran into on a project."],
                  64, 268, 30, 20, c["mu"], cls="rise", delay=0.34),
            dial(doc, c, "topic", 636, 60, 240, label_px=13, readout_px=15, caption_px=11, readout_y=186, caption_y=207, delay=0.2),
            dial(doc, c, "speed", 896, 60, 240, label_px=13, readout_px=15, caption_px=11, readout_y=186, caption_y=207, delay=0.28),
        ]
        css = rise_css(40)
    else:
        width, height = 390, 410
        body = [
            frame(c, width, height),
            '<g class="rise">',
            f'<circle cx="38" cy="38" r="14" fill="{MARK}"/>',
            t(doc, "sans", "sk", 38, 42, 11, MARK_INK, weight=700, anchor="middle", tracking=-0.22),
            t(doc, "sans", "Siddharth Kolapalli", 62, 44, 16, c["ink"], weight=700, tracking=-0.32),
            "</g>",
            t(doc, "mono", "Automotive systems · applied AI", 24, 92, 12, c["la"], weight=500, cls="rise", delay=0.12),
            t(doc, "serif", "Hi, I’m Sid.", 23, 132, 40, c["ink"], weight=500, tracking=-0.72, cls="rise", delay=0.22),
            lines(doc, "serif", ["Every article on sidkolapalli.com started", "as a problem I ran into on a project."],
                  24, 164, 26, 17, c["mu"], cls="rise", delay=0.34),
            dial(doc, c, "topic", 24, 226, 160, label_px=16, readout_px=17, caption_px=13, readout_y=188, caption_y=210, delay=0.42),
            dial(doc, c, "speed", 206, 226, 160, label_px=16, readout_px=17, caption_px=13, readout_y=188, caption_y=210, delay=0.5),
        ]
        css = rise_css(24)
    return svg_document(doc, width, height, title, desc, css + CLUSTER_CSS + reduced(REDUCED_CSS), body)


def render_divider(theme):
    c, doc = THEMES[theme], Doc()
    body = [
        f'<rect width="1200" height="1" fill="{c["lr"]}"/>',
        f'<rect y="17" width="1200" height="1" fill="{c["lr"]}"/>',
        f'<line class="lane" x1="0" y1="9" x2="1200" y2="9" stroke="{c["la"]}" stroke-width="2" stroke-dasharray="24 24"/>',
    ]
    css = ".lane{animation:lane 1.6s linear infinite}@keyframes lane{to{stroke-dashoffset:48}}" + reduced(".lane{animation:none}")
    return svg_document(doc, 1200, 18, "Road-line divider", "A lane marking between README sections.", css, body, stretch=True)


def trace(doc, c, weeks, *, x, y, w, h, x0, top, base, label_px, label_x, car_scale):
    """The weekly-contributions line, its reveal cover, playhead car and peak marker."""
    values = [week["total"] for week in weeks]
    points, scale = trace_points(values, x0, w, top, base)
    pts = " ".join(f"{px},{py}" for px, py in points)
    stats = drive_log_stats(weeks)
    px, py = points[stats["peak_index"]]
    right = stats["peak_index"] > len(values) * 0.75
    cover = w - x0 + 4
    mid = (top + base) / 2
    car = CAR.format(ink=c["ink"], pg=c["pg"])
    body = (
        f'<svg x="{x}" y="{y}" width="{w}" height="{h}" viewBox="0 0 {w} {h}" overflow="visible">'
        f'<line x1="{x0}" y1="{top}" x2="{w}" y2="{top}" stroke="{c["lr"]}"/>'
        f'<line x1="{x0}" y1="{mid}" x2="{w}" y2="{mid}" stroke="{c["lr"]}"/>'
        f'<polygon points="{x0},{base} {pts} {w},{base}" fill="{c["la"]}" fill-opacity="0.1"/>'
        f'<polyline points="{pts}" fill="none" stroke="{c["la"]}" stroke-width="2" stroke-linejoin="round" stroke-linecap="round"/>'
        f'<rect class="cover" x="{x0 - 2}" y="0" width="{cover}" height="{base - 1}" fill="{c["pg"]}"/>'
        f'<line x1="{x0}" y1="{base}" x2="{w}" y2="{base}" stroke="{c["mu"]}"/>'
        + t(doc, "mono", str(scale), label_x, top + label_px * 0.35, label_px, c["mu"], anchor="end")
        + t(doc, "mono", str(scale // 2), label_x, mid + label_px * 0.35, label_px, c["mu"], anchor="end")
        + t(doc, "mono", "0", label_x, base + label_px * 0.35, label_px, c["mu"], anchor="end")
        + f'<g class="headfade"><g class="head"><line x1="{x0}" y1="{top}" x2="{x0}" y2="{base}" stroke="{c["ink"]}"/>'
        f'<g transform="translate({x0 - 16 * car_scale:.1f} {base - 8 * car_scale:.1f}) scale({car_scale})">{car}</g></g></g>'
        f'<g class="peak"><circle cx="{px}" cy="{py}" r="4" fill="{c["la"]}" stroke="{c["pg"]}" stroke-width="2"/>'
        + t(doc, "mono", f"Peak · {stats['peak']}", round(px - 10 if right else px + 10, 1), round(py + 4, 1),
            label_px + 0.3, c["ink"], weight=500, anchor="end" if right else "start")
        + "</g></svg>"
    )
    css = (
        f".cover,.head{{animation:reveal 2.4s linear .3s both}}@keyframes reveal{{to{{transform:translateX({cover}px)}}}}"
        ".headfade{animation:out .4s ease-out 2.7s both}@keyframes out{to{opacity:0}}"
        ".peak{animation:fade .5s ease-out 2.8s both}@keyframes fade{from{opacity:0}to{opacity:1}}"
        + reduced(
            f".cover,.head{{animation:none;transform:translateX({cover}px)}}"
            ".headfade{animation:none;opacity:0}.peak{animation:fade .4s ease-out both}"
        )
    )
    return body, css


def render_drive_log(theme, layout, weeks, updated):
    c, doc = THEMES[theme], Doc()
    stats = drive_log_stats(weeks) if weeks else None
    title = "Drive log: weekly GitHub contributions over the last 12 months"
    if stats:
        desc = (
            f"{stats['total']:,} contributions; peak week {stats['peak']}; average {stats['average']} a week; "
            f"{stats['quiet']} quiet weeks. Updated {updated}."
        )
        values = [str(stats["peak"]), f"{stats['average']} / week", str(stats["quiet"])]
    else:
        desc = "Waiting for the first daily run."
        values = ["—", "—", "—"]
    stamp = f"UPDATED {updated}" if stats else "FIRST RUN PENDING"
    source = "Redrawn daily from the GitHub contribution calendar"
    labels = ["PEAK WEEK", "AVERAGE", "QUIET WEEKS"]
    css = ""
    if layout == "wide":
        width, height = 1200, 380
        body = [
            frame(c, width, height),
            t(doc, "mono", "Drive log · weekly contributions", 48, 52, 12.2, c["la"], weight=500),
            t(doc, "serif", f"{stats['total']:,} contributions in 12 months" if stats else "Waiting for the first daily run",
              48, 88, 26, c["ink"], weight=500, tracking=-0.39),
        ]
        for i, (label, value) in enumerate(zip(labels, values)):
            body.append(t(doc, "mono", label, 812 + 120 * i, 64, 10.5, c["mu"], tracking=0.84))
            body.append(t(doc, "sans", value, 812 + 120 * i, 90, 18, c["ink"], weight=600))
        body.append(f'<rect x="48" y="103" width="1104" height="1.5" fill="{c["ink"]}"/>')
        if stats:
            chart, css = trace(doc, c, weeks, x=48, y=110, w=1104, h=168, x0=36, top=8, base=156, label_px=11, label_x=28, car_scale=0.75)
            body.append(chart)
            step = 1068 / max(len(weeks) - 1, 1)
            for i, name in month_marks(weeks):
                body.append(t(doc, "mono", name, round(48 + 36 + i * step, 1), 296, 11.3, c["mu"]))
        else:
            body.append(f'<line x1="84" y1="266" x2="1152" y2="266" stroke="{c["mu"]}"/>')
            body.append(t(doc, "sans", "The chart appears after the first daily run.", 618, 196, 15, c["mu"], anchor="middle"))
        body += [
            f'<rect x="48" y="312" width="1104" height="1" fill="{c["lr"]}"/>',
            t(doc, "mono", stamp, 48, 336, 10.5, c["mu"], tracking=0.84),
            t(doc, "sans", source, 1152, 336, 13.5, c["mu"], anchor="end"),
        ]
    else:
        width, height = 390, 440
        head = [f"{stats['total']:,} contributions", "in 12 months"] if stats else ["Waiting for the", "first daily run"]
        values[1] = values[1].replace(" / week", " / wk")
        body = [
            frame(c, width, height),
            t(doc, "mono", "Drive log · weekly contributions", 24, 36, 12, c["la"], weight=500),
            lines(doc, "serif", head, 24, 70, 29, 24, c["ink"], weight=500),
            f'<rect x="24" y="112" width="342" height="1.5" fill="{c["ink"]}"/>',
        ]
        if stats:
            chart, css = trace(doc, c, weeks, x=24, y=118, w=342, h=140, x0=28, top=8, base=124, label_px=10, label_x=22, car_scale=0.6)
            body.append(chart)
            step = 314 / max(len(weeks) - 1, 1)
            for i, name in month_marks(weeks, every=3):
                body.append(t(doc, "mono", name, round(24 + 28 + i * step, 1), 274, 10.5, c["mu"]))
        else:
            body.append(f'<line x1="52" y1="242" x2="366" y2="242" stroke="{c["mu"]}"/>')
            body.append(t(doc, "sans", "The chart appears after the first run.", 209, 190, 13, c["mu"], anchor="middle"))
        for i, (label, value) in enumerate(zip(labels, values)):
            body.append(t(doc, "mono", label, 24 + 117 * i, 302, 10, c["mu"], tracking=0.8))
            body.append(t(doc, "sans", value, 24 + 117 * i, 326, 18, c["ink"], weight=600))
        body += [
            f'<rect x="24" y="344" width="342" height="1" fill="{c["lr"]}"/>',
            t(doc, "mono", stamp, 24, 366, 10, c["mu"], tracking=0.8),
            t(doc, "sans", source, 24, 388, 13, c["mu"]),
        ]
    return svg_document(doc, width, height, title, desc, css, body)


# Build ----------------------------------------------------------------------


def fetch_calendar(login, token):
    query = (
        "query($login:String!){user(login:$login){contributionsCollection{contributionCalendar"
        "{weeks{contributionDays{date contributionCount}}}}}}"
    )
    request = urllib.request.Request(
        GRAPHQL_URL,
        data=json.dumps({"query": query, "variables": {"login": login}}).encode(),
        headers={"Authorization": f"bearer {token}", "Content-Type": "application/json", "User-Agent": "profile-builder"},
    )
    with urllib.request.urlopen(request, timeout=30) as response:
        payload = json.load(response)
    if payload.get("errors"):
        raise RuntimeError(f"GitHub GraphQL error: {payload['errors']}")
    return payload["data"]["user"]["contributionsCollection"]["contributionCalendar"]


def fetch_text(url):
    request = urllib.request.Request(url, headers={"User-Agent": "profile-builder"})
    with urllib.request.urlopen(request, timeout=30) as response:
        return response.read().decode("utf-8")


def build(login, token, feed_text, today):
    """Write every asset and README section; keep the current drive log when there is no token."""
    ASSETS.mkdir(exist_ok=True)
    for theme in THEMES:
        (ASSETS / f"banner-{theme}.svg").write_text(render_banner(theme, "wide"), encoding="utf-8")
        (ASSETS / f"banner-phone-{theme}.svg").write_text(render_banner(theme, "phone"), encoding="utf-8")
        (ASSETS / f"divider-{theme}.svg").write_text(render_divider(theme), encoding="utf-8")

    readme = README.read_text(encoding="utf-8")
    weeks = weekly_totals(fetch_calendar(login, token)) if token else None
    if weeks or not (ASSETS / "drive-log-day.svg").exists():
        updated = today if weeks else None
        for theme in THEMES:
            (ASSETS / f"drive-log-{theme}.svg").write_text(render_drive_log(theme, "wide", weeks, updated), encoding="utf-8")
            (ASSETS / f"drive-log-phone-{theme}.svg").write_text(render_drive_log(theme, "phone", weeks, updated), encoding="utf-8")
        readme = replace_between(readme, "DRIVE-LOG", drive_log_markdown(drive_log_stats(weeks) if weeks else None, updated))

    items = parse_feed(feed_text) if feed_text else []
    if items:
        readme = replace_between(readme, "WRITING", writing_markdown(items))
    README.write_text(readme, encoding="utf-8")


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--login", default="sidkolapalli", help="GitHub user whose contributions the drive log shows")
    parser.add_argument("--feed-url", default=FEED_URL, help="RSS feed for the writing list")
    parser.add_argument("--offline", action="store_true", help="skip network calls; keep the drive log and writing list")
    args = parser.parse_args(argv)
    token = None if args.offline else os.environ.get("GITHUB_TOKEN")
    feed_text = None if args.offline else fetch_text(args.feed_url)
    build(args.login, token, feed_text, datetime.now(timezone.utc).date().isoformat())


if __name__ == "__main__":
    main()
