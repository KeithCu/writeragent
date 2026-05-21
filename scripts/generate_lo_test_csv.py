#!/usr/bin/env python3
# WriterAgent - AI Writing Assistant for LibreOffice
# Copyright (c) 2026 KeithCu
#
# SPDX-License-Identifier: GPL-3.0-or-later
"""Generate a reproducible CSV for LibreOffice import/Calc testing.

Usage (from repo root):
    python scripts/generate_lo_test_csv.py
    python scripts/generate_lo_test_csv.py --rows 5000 --seed 7 --output /tmp/sample.csv
"""
from __future__ import annotations

import argparse
import csv
import random
from datetime import date, timedelta
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent
DEFAULT_OUTPUT = SCRIPT_DIR / "lo_test_10k.csv"

HEADER = (
    "order_id",
    "order_date",
    "region",
    "country",
    "city",
    "product_category",
    "product_name",
    "quantity",
    "unit_price",
    "discount_pct",
    "revenue",
    "customer_segment",
    "status",
    "currency",
    "notes",
)

REGIONS = ("Americas", "EMEA", "APAC", "LATAM")

COUNTRIES_BY_REGION: dict[str, tuple[str, ...]] = {
    "Americas": ("United States", "Canada", "Mexico", "Brazil", "Chile"),
    "EMEA": ("Germany", "France", "United Kingdom", "Netherlands", "Poland", "South Africa"),
    "APAC": ("Japan", "South Korea", "Australia", "Singapore", "India", "China"),
    "LATAM": ("Mexico", "Brazil", "Argentina", "Colombia", "Peru"),
}

CITIES_ASCII = (
    "Portland", "Austin", "Denver", "Chicago", "Toronto", "Vancouver",
    "Berlin", "Munich", "Paris", "Lyon", "London", "Manchester",
    "Amsterdam", "Warsaw", "Tokyo", "Osaka", "Seoul", "Sydney",
    "Melbourne", "Singapore", "Mumbai", "Shanghai", "São Paulo",
    "Buenos Aires", "Bogotá", "Lima",
)

CITIES_UNICODE = (
    "São Paulo", "Zürich", "München", "Montréal", "北京", "München",
    "Kraków", "Łódź", "İstanbul", "México City",
)

CATEGORIES = ("Electronics", "Home", "Office", "Apparel", "Food")

PRODUCTS_BY_CATEGORY: dict[str, tuple[str, ...]] = {
    "Electronics": (
        "Wireless Mouse Pro", "USB-C Hub 7-Port", "Noise-Cancel Headphones",
        "4K Webcam", "Portable SSD 1TB", "Smart Speaker Mini",
    ),
    "Home": (
        "Ceramic Planter Set", "LED Desk Lamp", "Memory Foam Pillow",
        "Stainless Kettle", "Bamboo Cutting Board",
    ),
    "Office": (
        "Ergonomic Chair", "Standing Desk Mat", "Recycled Notebook Pack",
        "Label Printer", "Whiteboard Markers (12)",
    ),
    "Apparel": (
        "Merino Wool Sweater", "Running Shoes Lite", "Organic Cotton Tee",
        "Rain Jacket Packable", "Leather Belt Classic",
    ),
    "Food": (
        "Organic Coffee Beans", "Dark Chocolate Assortment", "Green Tea Sampler",
        "Granola Bulk 2kg", "Olive Oil Extra Virgin",
    ),
}

# Regional bias: index into CATEGORIES weights per region.
CATEGORY_WEIGHTS_BY_REGION: dict[str, tuple[int, ...]] = {
    "Americas": (3, 2, 2, 2, 1),
    "EMEA": (2, 2, 2, 2, 2),
    "APAC": (4, 1, 2, 1, 2),
    "LATAM": (1, 2, 1, 3, 3),
}

BASE_PRICE_BY_CATEGORY: dict[str, float] = {
    "Electronics": 89.0,
    "Home": 34.0,
    "Office": 45.0,
    "Apparel": 28.0,
    "Food": 12.0,
}

SEGMENTS = ("Consumer", "SMB", "Enterprise", "VIP")
SEGMENT_WEIGHTS = (50, 28, 17, 5)

STATUSES = ("Shipped", "Processing", "Cancelled", "Returned")
STATUS_WEIGHTS = (78, 14, 5, 3)

CURRENCY_BY_REGION: dict[str, tuple[str, ...]] = {
    "Americas": ("USD", "USD", "USD", "CAD", "MXN"),
    "EMEA": ("EUR", "EUR", "GBP", "EUR", "EUR", "ZAR"),
    "APAC": ("JPY", "KRW", "AUD", "SGD", "INR", "CNY"),
    "LATAM": ("USD", "BRL", "ARS", "COP", "PEN"),
}

DATE_START = date(2023, 1, 1)
DATE_END = date(2025, 12, 31)
DATE_SPAN_DAYS = (DATE_END - DATE_START).days

NOTE_TEMPLATES_SIMPLE = (
    "",
    "",
    "",
    "Rush delivery requested",
    "Gift wrap",
    "Leave at reception",
    "Contact before delivery",
)

NOTE_TEMPLATES_QUOTED = (
    'Customer said: "Please ring twice, leave with neighbor"',
    'Items include: monitor, keyboard, and "spare cable" pack',
    "Refund approved; reason: damaged in transit, box crushed",
    'VIP account — notes: "Always invoice to HQ, attn: Finance"',
    "Split shipment: part A today, part B next week (see email)",
)

RETURNED_NOTE_TEMPLATES = (
    "Returned: wrong size, refund issued",
    "Returned: defective unit, RMA #44291",
    "Partial return; kept accessories",
)


def _weighted_choice(rng: random.Random, items: tuple[str, ...], weights: tuple[int, ...]) -> str:
    return rng.choices(items, weights=weights, k=1)[0]


def _pick_category(rng: random.Random, region: str) -> str:
    weights = CATEGORY_WEIGHTS_BY_REGION[region]
    return _weighted_choice(rng, CATEGORIES, weights)


def _pick_order_date(rng: random.Random) -> date:
    # Mild Q4 seasonality: bias random day toward Oct–Dec.
    for _ in range(8):
        offset = rng.randint(0, DATE_SPAN_DAYS)
        d = DATE_START + timedelta(days=offset)
        if d.month >= 10:
            if rng.random() < 0.55:
                return d
        elif rng.random() < 0.35:
            return d
    offset = rng.randint(0, DATE_SPAN_DAYS)
    return DATE_START + timedelta(days=offset)


def _pick_quantity(rng: random.Random, segment: str, force_zero: bool) -> int:
    if force_zero:
        return 0
    if segment == "VIP":
        return rng.choices(
            range(1, 51),
            weights=[1] * 5 + [2] * 10 + [3] * 15 + [2] * 20,
            k=1,
        )[0]
    return rng.choices(
        range(1, 51),
        weights=[4] * 10 + [2] * 15 + [1] * 25,
        k=1,
    )[0]


def _pick_unit_price(rng: random.Random, category: str, segment: str) -> float:
    base = BASE_PRICE_BY_CATEGORY[category]
    if segment == "VIP":
        base *= rng.uniform(1.15, 1.65)
    elif segment == "Enterprise":
        base *= rng.uniform(1.05, 1.35)
    noise = rng.uniform(0.82, 1.22)
    return round(base * noise, 2)


def _pick_discount(rng: random.Random, segment: str) -> float:
    if segment == "VIP":
        return round(rng.uniform(0.08, 0.35), 2)
    if segment == "Enterprise":
        return round(rng.uniform(0.05, 0.22), 2)
    if segment == "SMB":
        return round(rng.uniform(0.0, 0.15), 2)
    return round(rng.uniform(0.0, 0.08), 2)


def _pick_notes(
    rng: random.Random,
    segment: str,
    status: str,
    force_quoted: bool,
) -> str:
    if status == "Returned":
        return rng.choice(RETURNED_NOTE_TEMPLATES)
    if force_quoted or (segment == "VIP" and rng.random() < 0.25):
        return rng.choice(NOTE_TEMPLATES_QUOTED)
    if rng.random() < 0.04:
        return rng.choice(NOTE_TEMPLATES_QUOTED)
    return rng.choice(NOTE_TEMPLATES_SIMPLE)


def generate_row(rng: random.Random, row_index: int) -> list:
    order_id = 100001 + row_index
    region = rng.choice(REGIONS)
    country = rng.choice(COUNTRIES_BY_REGION[region])
    city = rng.choice(CITIES_UNICODE) if rng.random() < 0.03 else rng.choice(CITIES_ASCII)

    category = _pick_category(rng, region)
    product_name = rng.choice(PRODUCTS_BY_CATEGORY[category])
    segment = _weighted_choice(rng, SEGMENTS, SEGMENT_WEIGHTS)

    force_zero_qty = rng.random() < 0.005
    quantity = _pick_quantity(rng, segment, force_zero_qty)
    unit_price = _pick_unit_price(rng, category, segment)
    discount_pct = _pick_discount(rng, segment)

    status = _weighted_choice(rng, STATUSES, STATUS_WEIGHTS)
    if rng.random() < 0.01:
        status = "Returned"

    if quantity == 0:
        revenue = 0.0
    else:
        revenue = round(quantity * unit_price * (1.0 - discount_pct), 2)

    currency_pool = CURRENCY_BY_REGION[region]
    currency = rng.choice(currency_pool)

    force_quoted_note = rng.random() < 0.04
    notes = _pick_notes(rng, segment, status, force_quoted_note)

    order_date = _pick_order_date(rng)

    return [
        order_id,
        order_date.isoformat(),
        region,
        country,
        city,
        category,
        product_name,
        quantity,
        f"{unit_price:.2f}",
        f"{discount_pct:.2f}",
        f"{revenue:.2f}",
        segment,
        status,
        currency,
        notes,
    ]


def write_csv(output: Path, rows: int, seed: int) -> None:
    rng = random.Random(seed)
    output.parent.mkdir(parents=True, exist_ok=True)
    with output.open("w", encoding="utf-8", newline="") as f:
        writer = csv.writer(f, quoting=csv.QUOTE_MINIMAL)
        writer.writerow(HEADER)
        for i in range(rows):
            writer.writerow(generate_row(rng, i))


def count_lines(path: Path) -> int:
    with path.open(encoding="utf-8") as f:
        return sum(1 for _ in f)


def main() -> int:
    parser = argparse.ArgumentParser(description="Generate a LibreOffice test CSV.")
    parser.add_argument("--rows", type=int, default=10000, help="Number of data rows (default: 10000)")
    parser.add_argument("--seed", type=int, default=42, help="RNG seed for reproducibility (default: 42)")
    parser.add_argument(
        "--output",
        type=Path,
        default=DEFAULT_OUTPUT,
        help=f"Output CSV path (default: {DEFAULT_OUTPUT})",
    )
    parser.add_argument("--quiet", action="store_true", help="Only print path and row count")
    args = parser.parse_args()

    if args.rows < 1:
        parser.error("--rows must be at least 1")

    write_csv(args.output, args.rows, args.seed)
    line_count = count_lines(args.output)
    expected = args.rows + 1
    if line_count != expected:
        raise SystemExit(f"Line count mismatch: expected {expected}, got {line_count}")

    size_bytes = args.output.stat().st_size
    if args.quiet:
        print(f"{args.output}\t{args.rows} data rows\t{size_bytes} bytes")
    else:
        print(f"Wrote {args.output}")
        print(f"  Data rows: {args.rows}")
        print(f"  Total lines (incl. header): {line_count}")
        print(f"  File size: {size_bytes:,} bytes ({size_bytes / 1024 / 1024:.2f} MiB)")
        print(f"  Seed: {args.seed}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
