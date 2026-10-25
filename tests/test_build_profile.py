"""Tests for scripts/build_profile.py and the README and assets it maintains."""

import re
import sys
import unittest
from datetime import date, timedelta
from unittest import mock
from pathlib import Path
from xml.etree import ElementTree as ET

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))

import build_profile as bp  # noqa: E402

SVG_NS = "{http://www.w3.org/2000/svg}"
FORBIDDEN_ELEMENTS = {"script", "foreignObject", "iframe", "object", "embed"}
PLACEHOLDER = re.compile(r"\[(?:[A-Z0-9][A-Z0-9 \-]{2,}|N)\]")


def calendar(counts, start="2025-10-26"):
    """A contributionCalendar-shaped dict holding one day per count."""
    first = date.fromisoformat(start)
    weeks = []
    for i in range(0, len(counts), 7):
        days = [
            {"date": (first + timedelta(days=i + j)).isoformat(), "contributionCount": n}
            for j, n in enumerate(counts[i : i + 7])
        ]
        weeks.append({"contributionDays": days})
    return {"weeks": weeks}


def sample_weeks():
    counts = [(i * 7) % 11 if i % 5 else 0 for i in range(53 * 7)]
    return bp.weekly_totals(calendar(counts))


def assert_safe_svg(case, text, name):
    """The SVG renders as a standalone image on GitHub and stays accessible."""
    root = ET.fromstring(text)
    case.assertEqual(root.tag, SVG_NS + "svg", name)
    case.assertTrue(root.get("viewBox"), f"{name}: viewBox")
    title = root.find(SVG_NS + "title")
    case.assertIsNotNone(title, f"{name}: <title>")
    case.assertTrue((title.text or "").strip(), f"{name}: empty <title>")
    for el in root.iter():
        local = el.tag.replace(SVG_NS, "")
        case.assertNotIn(local, FORBIDDEN_ELEMENTS, f"{name}: <{local}>")
        for key, value in el.attrib.items():
            if key.endswith("href"):
                case.assertFalse(value.startswith(("http:", "https:", "//")), f"{name}: external {key}")
    case.assertNotIn("http", text.replace("http://www.w3.org/2000/svg", ""), f"{name}: external URL")
    if "@keyframes" in text:
        case.assertIn("prefers-reduced-motion", text, f"{name}: reduced-motion rule")
    if root.find(f".//{SVG_NS}text") is not None:
        case.assertIn("data:font/woff2;base64,", text, f"{name}: embedded font")
    case.assertLess(len(text.encode()), 160_000, f"{name}: size")


class WeeklyTotalsTest(unittest.TestCase):
    def test_sums_each_week_and_keeps_its_start_date(self):
        weeks = bp.weekly_totals(calendar([1] * 7 + [0] * 6 + [5]))
        self.assertEqual(weeks, [{"start": "2025-10-26", "total": 7}, {"start": "2025-11-02", "total": 5}])

    def test_partial_last_week_counts_only_its_days(self):
        weeks = bp.weekly_totals(calendar([0] * 7 + [3, 4]))
        self.assertEqual([w["total"] for w in weeks], [0, 7])


class StatsTest(unittest.TestCase):
    def test_summarises_weekly_totals(self):
        stats = bp.drive_log_stats([{"start": "s", "total": t} for t in (0, 4, 10, 0)])
        self.assertEqual(stats, {"total": 14, "peak": 10, "peak_index": 2, "average": 3.5, "quiet": 2, "weeks": 4})

    def test_no_weeks_gives_zeros(self):
        stats = bp.drive_log_stats([])
        self.assertEqual((stats["total"], stats["peak"], stats["average"], stats["quiet"]), (0, 0, 0.0, 0))


class TracePointsTest(unittest.TestCase):
    def test_spans_the_plot_and_maps_peak_to_the_top(self):
        points, scale = bp.trace_points([0, 5, 10], x0=36, x1=1104, top=8, base=156)
        self.assertEqual(scale, 10)
        self.assertEqual(points[0], (36.0, 156.0))
        self.assertEqual(points[1], (570.0, 82.0))
        self.assertEqual(points[2], (1104.0, 8.0))

    def test_all_zero_weeks_do_not_divide_by_zero(self):
        points, scale = bp.trace_points([0, 0], x0=0, x1=10, top=0, base=100)
        self.assertEqual(scale, 10)
        self.assertEqual([y for _, y in points], [100.0, 100.0])

    def test_scale_rounds_up_to_a_multiple_of_ten(self):
        _, scale = bp.trace_points([3, 37], x0=0, x1=10, top=0, base=100)
        self.assertEqual(scale, 40)


class FeedTest(unittest.TestCase):
    FEED = """<?xml version="1.0"?><rss version="2.0"><channel><title>Site</title>
      <item><title>Rules &amp; Models</title><link>https://example.com/#/post/a</link><category>ADAS</category></item>
      <item><title>No category</title><link>https://example.com/#/post/b</link></item>
      <item><title>Bad link</title><link>javascript:alert(1)</link></item>
    </channel></rss>"""

    def test_parses_items_in_feed_order_and_drops_unsafe_links(self):
        self.assertEqual(
            bp.parse_feed(self.FEED),
            [
                {"title": "Rules & Models", "link": "https://example.com/#/post/a", "category": "ADAS"},
                {"title": "No category", "link": "https://example.com/#/post/b", "category": ""},
            ],
        )


class MarkersTest(unittest.TestCase):
    def test_replaces_only_the_marked_section(self):
        text = "a\n<!-- X:START -->\nold\n<!-- X:END -->\nb"
        self.assertEqual(bp.replace_between(text, "X", "new"), "a\n<!-- X:START -->\nnew\n<!-- X:END -->\nb")

    def test_is_idempotent(self):
        once = bp.replace_between("<!-- X:START --><!-- X:END -->", "X", "body")
        self.assertEqual(bp.replace_between(once, "X", "body"), once)

    def test_missing_markers_raise(self):
        with self.assertRaises(ValueError):
            bp.replace_between("nothing here", "X", "body")


class WritingMarkdownTest(unittest.TestCase):
    ITEMS = [{"title": f"Post {i} [draft]", "link": f"https://example.com/{i}", "category": "Sim & AI"} for i in range(7)]

    def test_lists_category_and_linked_title(self):
        lines = bp.writing_markdown(self.ITEMS, limit=5).splitlines()
        self.assertEqual(len(lines), 5)
        self.assertEqual(lines[0], r"- <sub>Sim &amp; AI</sub> · [Post 0 \[draft\]](https://example.com/0)")


class DriveLogMarkdownTest(unittest.TestCase):
    def test_picture_switches_theme_and_width_and_describes_the_numbers(self):
        stats = {"total": 412, "peak": 38, "peak_index": 3, "average": 7.9, "quiet": 6, "weeks": 53}
        md = bp.drive_log_markdown(stats, updated="2026-10-25")
        sources = re.findall(r'<source media="([^"]+)" srcset="([^"]+)"', md)
        self.assertEqual(
            sources,
            [
                ("(max-width: 600px) and (prefers-color-scheme: dark)", "assets/drive-log-phone-night.svg"),
                ("(max-width: 600px)", "assets/drive-log-phone-day.svg"),
                ("(prefers-color-scheme: dark)", "assets/drive-log-night.svg"),
            ],
        )
        self.assertIn('src="assets/drive-log-day.svg"', md)
        self.assertIn("412 contributions in the last 12 months", md)
        self.assertIn("Updated 2026-10-25", md)

    def test_pending_alt_text_says_so(self):
        self.assertIn("waiting for the first daily run", bp.drive_log_markdown(None, updated=None))


class SubsetTest(unittest.TestCase):
    def test_subset_is_small_woff2(self):
        data = bp.subset_woff2(bp.FONT_DIR / bp.FONTS["serif"][1], "Hi")
        self.assertTrue(data.startswith(b"wOF2"))
        self.assertLess(len(data), 15_000)

    def test_subset_does_not_depend_on_the_clock(self):
        # A daily run must not rewrite unchanged images just because time passed.
        path = bp.FONT_DIR / bp.FONTS["mono"][1]
        with mock.patch("fontTools.ttLib.tables._h_e_a_d.timestampNow", return_value=0):
            first = bp.subset_woff2.__wrapped__(path, "Clock")
        with mock.patch("fontTools.ttLib.tables._h_e_a_d.timestampNow", return_value=10**9):
            second = bp.subset_woff2.__wrapped__(path, "Clock")
        self.assertEqual(first, second)


class RenderTest(unittest.TestCase):
    def test_banners_are_safe_in_every_theme_and_layout(self):
        for theme in bp.THEMES:
            for layout in ("wide", "phone"):
                svg = bp.render_banner(theme, layout)
                assert_safe_svg(self, svg, f"banner {theme} {layout}")
                self.assertIn("Hi, I’m Sid.", svg)

    def test_divider_stretches_and_is_safe(self):
        for theme in bp.THEMES:
            svg = bp.render_divider(theme)
            assert_safe_svg(self, svg, f"divider {theme}")
            self.assertIn('preserveAspectRatio="none"', svg)

    def test_drive_log_shows_real_numbers(self):
        weeks = sample_weeks()
        stats = bp.drive_log_stats(weeks)
        for theme in bp.THEMES:
            for layout in ("wide", "phone"):
                svg = bp.render_drive_log(theme, layout, weeks, updated="2026-10-25")
                assert_safe_svg(self, svg, f"drive log {theme} {layout}")
                self.assertIn(f"{stats['total']:,}", svg)
                self.assertIn("Updated 2026-10-25", svg)

    def test_pending_drive_log_shows_no_numbers(self):
        for layout in ("wide", "phone"):
            svg = bp.render_drive_log("day", layout, None, updated=None)
            assert_safe_svg(self, svg, f"pending drive log {layout}")
            self.assertIn("Waiting for the first daily run", svg)
            self.assertNotIn("contributions in 12 months", svg)


class RepoReadmeTest(unittest.TestCase):
    def setUp(self):
        self.readme = (ROOT / "README.md").read_text(encoding="utf-8")

    def test_has_the_sections_the_workflow_rewrites(self):
        for name in ("DRIVE-LOG", "WRITING"):
            self.assertIn(f"<!-- {name}:START -->", self.readme)
            self.assertIn(f"<!-- {name}:END -->", self.readme)

    def test_every_referenced_asset_exists(self):
        paths = re.findall(r'(?:src|srcset)="(assets/[^"]+)"', self.readme)
        self.assertGreaterEqual(len(paths), 10)
        for path in paths:
            self.assertTrue((ROOT / path).is_file(), path)

    def test_links_to_the_site_and_projects(self):
        for url in (
            "https://sidkolapalli.com/",
            "https://sidkolapalli.com/feed.xml",
            "https://github.com/sidkolapalli/carla-agentic-toolkit",
            "https://github.com/sidkolapalli/TracePad",
        ):
            self.assertIn(url, self.readme)

    def test_uses_only_markup_github_keeps(self):
        self.assertNotIn("<script", self.readme)
        self.assertNotIn("style=", self.readme)

    def test_has_no_placeholders(self):
        self.assertEqual(PLACEHOLDER.findall(self.readme), [])


class RepoAssetsTest(unittest.TestCase):
    def test_every_svg_is_safe(self):
        svgs = sorted((ROOT / "assets").glob("*.svg"))
        self.assertGreaterEqual(len(svgs), 10)
        for path in svgs:
            assert_safe_svg(self, path.read_text(encoding="utf-8"), path.name)


if __name__ == "__main__":
    unittest.main()
