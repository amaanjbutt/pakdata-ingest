"""
harvest_dataset_codes.py — one-time harvest of EasyData dataset codes.

Run this ONCE on your laptop (not Docker, not Colab). It opens the SBP EasyData
portal, walks the dataset tree, and writes every dataset code to
ingestion/dataset_codes.json — which discover_easydata.py then consumes.

WHY A BROWSER: EasyData has no "list datasets" API. The dataset codes
(TS_GP_BOP_WR_M, ...) live only in the portal's navigation. We collect them once.

SETUP (in a terminal, in your project folder):
    python -m venv .venv
    # mac/linux:
    source .venv/bin/activate
    # windows:
    .venv\Scripts\activate
    pip install playwright
    playwright install chromium

RUN:
    python harvest_dataset_codes.py            # visible browser (recommended first run)
    python harvest_dataset_codes.py --headless # no window, once you trust it

If it finds few/zero codes, run WITHOUT --headless, watch how the tree behaves,
then tell Claude which elements you have to click to expand it.
"""
from __future__ import annotations

import argparse
import json
import re
import time
import urllib.parse
from pathlib import Path

from playwright.sync_api import sync_playwright

START = "https://easydata.sbp.org.pk"
OUT = Path("ingestion/dataset_codes.json")
CODE_RE = re.compile(r"(TS_[A-Z0-9_]+)")


def codes_from_text(text: str) -> set[str]:
    """Pull TS_... dataset codes out of any URL/text blob (URL-decoded)."""
    decoded = urllib.parse.unquote(text or "")
    found = set()
    for m in CODE_RE.finditer(decoded):
        code = m.group(1).split(".")[0]  # dataset code = part before .<suffix>
        found.add(code)
    return found


def harvest(headless: bool) -> list[str]:
    found: set[str] = set()
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=headless)
        page = browser.new_context(
            user_agent="pakdata-harvester/1.0 (one-time dataset discovery)"
        ).new_page()

        print(f"Opening {START} ...")
        page.goto(START, wait_until="networkidle", timeout=90_000)
        time.sleep(3)

        print("Clicking 'Datasets' tab...")
        datasets_link = page.get_by_role("link", name="Datasets", exact=True)
        if datasets_link.count() > 0:
            datasets_link.first.click()
        else:
            page.locator("a:has-text('Datasets')").first.click()
        
        page.wait_for_load_state("networkidle")
        time.sleep(3)

        # Select "All" from the rows dropdown to load all datasets
        print("Selecting 'All' in rows dropdown...")
        selects = page.locator("select").all()
        rows_select = None
        for sel in selects:
            options = sel.locator("option").all_inner_texts()
            if "All" in options:
                rows_select = sel
                break
        
        if rows_select:
            rows_select.select_option(label="All")
            page.wait_for_load_state("networkidle")
            time.sleep(5)
        else:
            print("[Warning] Could not find 'All' rows dropdown option. Proceeding with visible links.")

        print("Extracting dataset codes...")
        for h in page.eval_on_selector_all("a", "els => els.map(e => e.href).filter(Boolean)"):
            found |= codes_from_text(h)
        found |= codes_from_text(page.content())

        browser.close()
    return sorted(found)


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--headless", action="store_true")
    args = ap.parse_args()

    codes = harvest(headless=args.headless)

    OUT.parent.mkdir(parents=True, exist_ok=True)
    OUT.write_text(json.dumps({
        "_comment": "Harvested from the EasyData APEX portal by harvest_dataset_codes.py. "
                    "Consumed by discover_easydata.py.",
        "dataset_codes": codes,
    }, indent=2) + "\n", encoding="utf-8")

    print(f"\nHarvested {len(codes)} dataset codes -> {OUT}")
    for c in codes[:25]:
        print("  ", c)
    if len(codes) > 25:
        print(f"   ... and {len(codes) - 25} more")
    if len(codes) < 5:
        print("\n[!] Very few codes found. Re-run WITHOUT --headless, watch how the")
        print("    dataset menu expands, and report back which elements to click.")


if __name__ == "__main__":
    main()
