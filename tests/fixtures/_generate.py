"""Regenerate the Google Flights HTML fixtures.

    python tests/fixtures/_generate.py

The fixtures are **synthetic**: they reproduce the payload's documented index
layout rather than being a captured live page. That is a deliberate trade, and
its limits should be understood.

What the canary test can catch with these: our own parser drifting away from the
layout it claims to read — someone "tidying" ``segment[22]`` into ``segment[21]``,
or dropping the second itinerary section. What it cannot catch: *Google* moving
an index, since a synthetic fixture moves with whatever this file says.

Closing that gap needs a captured page. To do it: fetch a real results page
(``fast_flights.fetcher.fetch_flights_html``), save the HTML beside these files,
and point the canary test at it. The test asserts on parsed *values*, so it will
work unchanged — and the day Google renumbers, it fails with the index named.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

HERE = Path(__file__).parent

SEGMENT_LENGTH = 23  # the identity we need lives at index 22


def segment(
    *,
    from_code: str,
    from_name: str,
    to_code: str,
    to_name: str,
    dep_date: list[int],
    dep_time: list[int],
    arr_date: list[int],
    arr_time: list[int],
    duration: int,
    plane: str,
    carrier: str | None,
    number: str | None,
    airline: str | None,
) -> list[Any]:
    """One segment, positioned exactly where the parser reads each field."""
    seg: list[Any] = [None] * SEGMENT_LENGTH
    seg[3] = from_code
    seg[4] = from_name
    seg[5] = to_name  # not a typo — name before code on the arrival side
    seg[6] = to_code
    seg[8] = dep_time
    seg[10] = arr_time
    seg[11] = duration
    seg[17] = plane
    seg[20] = dep_date
    seg[21] = arr_date
    # `identity=None` models the drift case: a segment with no carrier fields.
    seg[22] = [carrier, number, None, airline] if carrier else None
    return seg


def itinerary(price: int, names: list[str], segments: list[list[Any]]) -> list[Any]:
    return [[None, names, segments], [[None, price]]]


NONSTOP = segment(
    from_code="SFO",
    from_name="San Francisco International Airport",
    to_code="JFK",
    to_name="John F. Kennedy International Airport",
    dep_date=[2026, 9, 15],
    dep_time=[8],  # 08:00 — Google omits the trailing zero minute
    arr_date=[2026, 9, 15],
    arr_time=[16, 30],
    duration=330,
    plane="Airbus A321neo",
    carrier="AS",
    number="3361",
    airline="Alaska Airlines",
)

CONNECTING = [
    segment(
        from_code="SFO",
        from_name="San Francisco International Airport",
        to_code="ORD",
        to_name="O'Hare International Airport",
        dep_date=[2026, 9, 15],
        dep_time=[6, 15],
        arr_date=[2026, 9, 15],
        arr_time=[12, 25],
        duration=250,
        plane="Boeing 737 MAX 8",
        carrier="UA",
        number="742",
        airline="United",
    ),
    segment(
        from_code="ORD",
        from_name="O'Hare International Airport",
        to_code="JFK",
        to_name="John F. Kennedy International Airport",
        dep_date=[2026, 9, 15],
        dep_time=[14],
        arr_date=[2026, 9, 15],
        arr_time=[17, 10],
        duration=130,
        plane="Boeing 757-200",
        carrier="UA",
        number="1607",
        airline="United",
    ),
]


def build_payload(*, best: list[Any], other: list[Any]) -> list[Any]:
    payload: list[Any] = [None] * 8
    payload[2] = [other]
    payload[3] = [best]
    payload[7] = [None, [None, [["AS", "Alaska Airlines"], ["UA", "United"], ["B6", "JetBlue"]]]]
    return payload


def wrap(payload: Any) -> str:
    """Embed a payload the way a real results page carries it."""
    blob = json.dumps(payload)
    return (
        "<!doctype html><html><head><title>Flights</title></head><body>"
        '<div class="results"></div>'
        '<script class="ds:1" nonce="abc123">AF_initDataCallback({key: \'ds:1\', '
        f"hash: '2', data:{blob}, sideChannel: {{}}}});</script>"
        "</body></html>"
    )


def main() -> None:
    healthy = build_payload(
        best=[itinerary(298, ["Alaska"], [NONSTOP])],
        # The cheaper fare sits in the *second* section, which the upstream
        # library never reads — the reason this parser exists.
        other=[itinerary(243, ["United"], CONNECTING)],
    )
    (HERE / "google_flights_healthy.html").write_text(wrap(healthy))

    # Drift: the "other" section is gone and the surviving segment carries no
    # identity at index 22 — offers arrive with no flight number.
    anonymous = segment(
        from_code="SFO",
        from_name="San Francisco International Airport",
        to_code="JFK",
        to_name="John F. Kennedy International Airport",
        dep_date=[2026, 9, 15],
        dep_time=[8],
        arr_date=[2026, 9, 15],
        arr_time=[16, 30],
        duration=330,
        plane="Airbus A321neo",
        carrier=None,
        number=None,
        airline=None,
    )
    drifted = build_payload(best=[itinerary(298, ["Alaska"], [anonymous])], other=[])
    (HERE / "google_flights_drifted.html").write_text(wrap(drifted))

    (HERE / "google_flights_no_results.html").write_text(
        "<!doctype html><html><body>"
        "<script class=\"ds:1\">AF_initDataCallback({key: 'ds:1', data:[], "
        "errorHasStatus: true,});</script></body></html>"
    )

    print(f"wrote fixtures to {HERE}")


if __name__ == "__main__":
    main()
