"""Parser for the JSON payload embedded in a Google Flights results page.

The third provider in this pack has no API. Its results arrive as an obfuscated
nested-array blob inside a `<script class="ds:1">` tag, addressed entirely by
integer index — `segment[22][0]` is the carrier code, and nothing in the page
says so. Those indexes were established empirically against live pages
(vacation-price-tracker, 2026-07) and Google is free to renumber them tomorrow.

Two consequences shape this module.

**Every read is guarded.** `_at` returns `None` on any shape mismatch, so a
renumbered payload yields missing fields rather than an `IndexError` from four
frames down. A scraper that crashes on drift is a scraper that pages you at 3am.

**Drift is measured, not merely survived.** Degrading quietly is its own
failure: the caller gets offers with no flight numbers, or half the page, and
no reason to doubt them. :class:`PayloadHealth` records which sections parsed
and how many segments yielded an identity, which is what lets the adapter
return `DEGRADED` and the router keep looking.

Deliberately dependency-free — no HTML parser, no `fast-flights` import. The
fetching half needs a browser-impersonating HTTP client; reading the payload
does not, and keeping them apart is what makes this testable from a fixture.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

__all__ = [
    "GooglePayloadError",
    "NoFlightsFound",
    "ParsedItinerary",
    "ParsedPage",
    "ParsedSegment",
    "PayloadHealth",
    "parse_flights_page",
]

# Section indexes: "best flights" first, then the additional departing options.
# The upstream fast-flights library reads only the first; the cheapest fare
# regularly sits in the second, so both are read here.
BEST_SECTION = 3
OTHER_SECTION = 2

# Matched in two steps rather than one attribute-shaped pattern: quoting style
# and attribute order are exactly the sort of incidental markup detail that
# changes without warning, and a parser that reads "block missing" from a
# swapped quote character reports a bot challenge that never happened.
_SCRIPT_RE = re.compile(r"<script\b([^>]*)>(.*?)</script>", re.DOTALL | re.IGNORECASE)
_DS1_RE = re.compile(r"\bds:1\b")


class GooglePayloadError(Exception):
    """The page carried no readable payload — blocked, challenged, or changed."""


class NoFlightsFound(Exception):
    """Google's explicit "no flights for this route/date" answer.

    A legitimate empty result, not a failure: retrying or failing over to
    another provider will not conjure a flight that does not exist.
    """


@dataclass(frozen=True, slots=True)
class ParsedSegment:
    """One flight segment, with the identity the upstream library discards."""

    from_code: str | None = None
    from_name: str | None = None
    to_code: str | None = None
    to_name: str | None = None
    departure: datetime | None = None
    arrival: datetime | None = None
    duration_minutes: int | None = None
    plane_type: str | None = None
    carrier: str | None = None
    flight_number: str | None = None
    airline_name: str | None = None

    @property
    def designator(self) -> str | None:
        """Full carrier-prefixed designator, e.g. ``"AS943"``."""
        if self.carrier and self.flight_number:
            return f"{self.carrier}{self.flight_number}"
        return None


@dataclass(frozen=True, slots=True)
class ParsedItinerary:
    """One priced itinerary from either page section."""

    price: int | None = None
    airline_names: tuple[str, ...] = ()
    segments: tuple[ParsedSegment, ...] = ()
    is_best: bool = False


@dataclass(frozen=True, slots=True)
class PayloadHealth:
    """What the parser found, so drift can be reported rather than absorbed.

    The distinction that matters: a page with no flights is *healthy* (Google
    answered, the answer was "none"), while a page whose segments carry no
    carrier identity is *damaged* — it looks like a result and is missing a
    field this pack promises on every offer.
    """

    sections_found: tuple[int, ...] = ()
    itineraries: int = 0
    segments: int = 0
    segments_with_identity: int = 0

    @property
    def missing_sections(self) -> tuple[int, ...]:
        return tuple(s for s in (BEST_SECTION, OTHER_SECTION) if s not in self.sections_found)

    @property
    def degraded(self) -> bool:
        if not self.itineraries:
            # No itineraries at all: an empty page is a valid answer, and the
            # section indexes are unfalsifiable when there is nothing in them.
            return False
        return bool(self.missing_sections) or self.segments_with_identity < self.segments

    @property
    def reason(self) -> str | None:
        """A drift report naming the index, so the fix is a one-line search."""
        if not self.degraded:
            return None
        problems: list[str] = []
        if self.missing_sections:
            names = ", ".join(f"payload[{s}][0]" for s in self.missing_sections)
            problems.append(f"itinerary section(s) absent or unreadable: {names}")
        if self.segments_with_identity < self.segments:
            missing = self.segments - self.segments_with_identity
            problems.append(
                f"{missing}/{self.segments} segments carry no carrier identity at "
                "segment[22] — flight numbers are missing from those offers"
            )
        return "Google Flights payload drift: " + "; ".join(problems)


@dataclass(frozen=True, slots=True)
class ParsedPage:
    itineraries: tuple[ParsedItinerary, ...] = ()
    airline_code_to_name: dict[str, str] = field(default_factory=dict)
    health: PayloadHealth = field(default_factory=PayloadHealth)


def _at(node: Any, *indexes: int) -> Any:
    """Nested list access returning ``None`` on any shape mismatch."""
    for i in indexes:
        if not isinstance(node, list) or i >= len(node):
            return None
        node = node[i]
    return node


def _extract_payload(html: str) -> Any:
    body = next(
        (m.group(2) for m in _SCRIPT_RE.finditer(html) if _DS1_RE.search(m.group(1))),
        None,
    )
    if body is None:
        raise GooglePayloadError(
            "no `script.ds:1` block in the page — typically a consent wall, a bot "
            "challenge, or a markup change rather than a missing result"
        )
    if "data:" not in body:
        raise GooglePayloadError("`script.ds:1` block carried no `data:` assignment")
    data = body.split("data:", 1)[1].rsplit(",", 1)[0].strip()
    if data.endswith("errorHasStatus: true"):
        raise NoFlightsFound("Google reported no flights for this route and date")
    try:
        return json.loads(data)
    except ValueError as exc:
        raise GooglePayloadError(f"payload was not JSON: {data[:200]}") from exc


def _to_datetime(date_part: Any, time_part: Any) -> datetime | None:
    """Combine ``[y, m, d]`` with ``[h]`` / ``[h, m]``, both local.

    Google omits trailing zero components: 14:00 arrives as ``[14]`` and
    midnight as ``None``. Reading these positionally without that allowance
    turns every on-the-hour departure into a parse failure.
    """
    if not isinstance(date_part, list) or len(date_part) != 3:
        return None
    parts = time_part if isinstance(time_part, list) else []
    hour = parts[0] if len(parts) >= 1 and isinstance(parts[0], int) else 0
    minute = parts[1] if len(parts) >= 2 and isinstance(parts[1], int) else 0
    try:
        return datetime(date_part[0], date_part[1], date_part[2], hour, minute)
    except (TypeError, ValueError):
        return None


def _str_at(node: Any, *indexes: int) -> str | None:
    value = _at(node, *indexes)
    return value if isinstance(value, str) and value else None


def _parse_segment(seg: Any) -> ParsedSegment | None:
    if not isinstance(seg, list):
        return None
    identity = _at(seg, 22)
    carrier = _at(identity, 0)
    number = _at(identity, 1)
    duration = _at(seg, 11)
    return ParsedSegment(
        from_code=_str_at(seg, 3),
        from_name=_str_at(seg, 4),
        to_code=_str_at(seg, 6),
        # Not a typo: the payload stores the arrival airport's name before its
        # code, the opposite order from departure.
        to_name=_str_at(seg, 5),
        departure=_to_datetime(_at(seg, 20), _at(seg, 8)),
        arrival=_to_datetime(_at(seg, 21), _at(seg, 10)),
        duration_minutes=duration if isinstance(duration, int) else None,
        plane_type=_str_at(seg, 17),
        carrier=str(carrier) if isinstance(carrier, str) and carrier else None,
        flight_number=str(number) if isinstance(number, str | int) and number != "" else None,
        airline_name=_str_at(identity, 3),
    )


def _parse_itinerary(item: Any, is_best: bool) -> ParsedItinerary | None:
    flight = _at(item, 0)
    if not isinstance(flight, list):
        return None
    segments = tuple(
        parsed for seg in _at(flight, 2) or [] if (parsed := _parse_segment(seg)) is not None
    )
    if not segments:
        return None
    price = _at(item, 1, 0, 1)
    names = _at(flight, 1)
    return ParsedItinerary(
        price=price if isinstance(price, int) else None,
        airline_names=tuple(str(n) for n in names if isinstance(n, str))
        if isinstance(names, list)
        else (),
        segments=segments,
        is_best=is_best,
    )


def parse_flights_page(html: str) -> ParsedPage:
    """Parse a Google Flights results page.

    Best-section itineraries come first (Google's own ranking), then the
    additional departing options.

    Raises :class:`NoFlightsFound` on Google's explicit empty answer and
    :class:`GooglePayloadError` when the payload cannot be located or decoded —
    two cases the adapter must treat differently, which is why they are two
    exception types and not one.
    """
    payload = _extract_payload(html)

    code_to_name: dict[str, str] = {}
    for entry in _at(payload, 7, 1, 1) or []:
        code, name = _at(entry, 0), _at(entry, 1)
        if isinstance(code, str) and isinstance(name, str) and code:
            code_to_name[code.upper()] = name

    itineraries: list[ParsedItinerary] = []
    sections_found: list[int] = []
    for section, is_best in ((BEST_SECTION, True), (OTHER_SECTION, False)):
        parsed_section = [
            parsed
            for item in _at(payload, section, 0) or []
            if (parsed := _parse_itinerary(item, is_best)) is not None
        ]
        if parsed_section:
            sections_found.append(section)
        itineraries.extend(parsed_section)

    all_segments = [seg for it in itineraries for seg in it.segments]
    return ParsedPage(
        itineraries=tuple(itineraries),
        airline_code_to_name=code_to_name,
        health=PayloadHealth(
            sections_found=tuple(sections_found),
            itineraries=len(itineraries),
            segments=len(all_segments),
            segments_with_identity=sum(1 for s in all_segments if s.designator),
        ),
    )
