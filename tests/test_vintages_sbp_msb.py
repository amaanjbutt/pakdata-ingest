"""SBP Monthly Statistical Bulletin vintage backfill — pure matching/chain logic."""
from datetime import date

from scripts.vintages_sbp_msb import _header_months, _rows, build_index, build_vintages, map_row

TRACK = date(2026, 8, 17)
MONTHS = ["2025-01-31", "2025-02-28", "2025-03-31", "2025-04-30", "2025-05-31", "2025-06-30"]


def _obs(sid, vals):
    return [{"series_id": sid, "obs_date": date.fromisoformat(d), "value": v} for d, v in zip(MONTHS, vals)]


def test_header_rolls_year_over_and_flags_corresponding_column():
    cols, corr = _header_months("I T E M S\nUnit / Base 2022 2023\nDec Jan Feb Mar\n1 Currency 1 2 3 4")
    assert cols == [(2022, 12), (2023, 1), (2023, 2), (2023, 3)] and not corr
    cols, corr = _header_months("Unit / Base Corresponding Month a year ago 2011\nItems May Jun. Jul.\n1 X 1 2 3 4")
    assert cols == [(2011, 5), (2011, 6), (2011, 7)] and corr


def test_rows_fold_wrapped_units_and_keep_negatives():
    text = "11 Exports (BOP) Million US\nDollars\n2,308 2,222\n14 Portfolio (Net) \" (1,001.9) 7.8"
    rows = dict(_rows(text))
    assert rows["Exports (BOP) Million US Dollars"] == ["2,308", "2,222"]
    assert rows['Portfolio (Net) "'] == ["(1,001.9)", "7.8"]


def test_row_maps_by_value_and_unit_scale():
    # ours in pkr_mn; the bulletin prints Billion Rs to 2 dp
    idx = build_index(_obs("banking.m2", [27942840.4, 27674070.1, 27918290.0, 28814140.2, 29328550.0, 29499700.0])
                      + _obs("noise.series", [1, 2, 3, 4, 5, 6]))
    row = {"label": "Broad Money (M2)", "values": {d: [v, 2] for d, v in zip(
        MONTHS, [27942.84, 27674.07, 27918.29, 28814.14, 29328.55, 29499.70])}}
    assert [(s, sc) for s, sc, _ in map_row(row, idx)] == [("banking.m2", 1e3)]


def test_unrelated_row_does_not_map():
    idx = build_index(_obs("a.b", [10.0, 11.0, 12.0, 13.0, 14.0, 15.0]))
    row = {"label": "x", "values": {d: [v, 1] for d, v in zip(MONTHS, [99.1, 98.2, 97.3, 96.4, 95.5, 94.6])}}
    assert map_row(row, idx) == []


def test_revision_chain_and_first_print_dates():
    # CPI YoY for Jan-2025 printed 24.1 (Feb issue), revised to 24.3 (Mar issue);
    # every other month never revised. Today's value is 24.3 -> one revision.
    ours = [24.3, 23.0, 21.5, 20.1, 19.2, 18.0]
    idx = build_index(_obs("cpi.yoy", ours))
    current = {("cpi.yoy", d): v for d, v in zip(MONTHS, ours)}
    feb = {"issue": "2025-02", "published": "2025-02-28",
           "rows": [{"label": "CPI", "values": {MONTHS[0]: [24.1, 1], **{d: [v, 1] for d, v in zip(MONTHS[1:], ours[1:])}}}]}
    mar = {"issue": "2025-03", "published": "2025-03-31",
           "rows": [{"label": "CPI", "values": {d: [v, 1] for d, v in zip(MONTHS, ours)}}]}
    mapping, first_seen, revisions = build_vintages([feb, mar], idx, current, TRACK)
    assert "cpi.yoy" in mapping
    assert first_seen[("cpi.yoy", MONTHS[0])] == "2025-02-28"
    assert revisions == [("cpi.yoy", MONTHS[0], 24.1, "2025-03-31")]


def test_later_undated_revision_is_pinned_to_tracking_start():
    ours = [5.0, 6.0, 7.0, 8.0, 9.0, 10.0]
    idx = build_index(_obs("s", ours))
    current = {("s", d): v for d, v in zip(MONTHS, ours)}
    current[("s", MONTHS[0])] = 5.4  # revised after the bulletin stopped printing it
    iss = {"issue": "2025-06", "published": "2025-06-30",
           "rows": [{"label": "S", "values": {d: [v, 1] for d, v in zip(MONTHS, ours)}}]}
    _, _, revisions = build_vintages([iss], idx, current, TRACK)
    assert revisions == [("s", MONTHS[0], 5.0, TRACK.isoformat())]


def test_label_family_ignores_cosmetics_but_not_concepts():
    from scripts.vintages_sbp_msb import label_family

    assert label_family('CPI Inflation YoY Urban (Non-Food) "') == label_family('CPI Inflation YoY Urban (Non Food) "')
    assert label_family("Exchange Rate (end month) Rs./US $") == label_family("Exchange Rate (Month end) Rs./US $")
    assert label_family("Real Effective Exchange Rate (REER)") != label_family("Nominal Effective Exchange Rate (NEER)")
    assert label_family('Gold and Foreign Exchange Reserves "') != label_family('Foreign Exchange Reserves "')


def test_off_family_row_is_dropped():
    ours = [100.0, 101.0, 102.0, 103.0, 104.0, 105.0]
    idx = build_index(_obs("fx.reer", ours))
    current = {("fx.reer", d): v for d, v in zip(MONTHS, ours)}
    good = {"label": "Real Effective Exchange Rate (REER)", "values": {d: [v, 1] for d, v in zip(MONTHS, ours)}}
    wrong = {"label": "Nominal Effective Exchange Rate (NEER)", "values": {d: [v, 1] for d, v in zip(MONTHS, ours)}}
    issues = [{"issue": f"2025-0{k}", "published": f"2025-0{k}-28", "rows": [good]} for k in (6, 7, 8)]
    issues.append({"issue": "2025-09", "published": "2025-09-30", "rows": [wrong]})
    mapping, _, _ = build_vintages(issues, idx, current, TRACK)
    assert mapping["fx.reer"]["issues"] == 3


def test_one_off_blip_is_dropped_but_persistent_misprint_kept():
    from scripts.vintages_sbp_msb import drop_blips

    blip = [("2025-01-31", 10.0, 1), ("2025-02-28", 99.0, 1), ("2025-03-31", 10.0, 1)]
    assert [p[1] for p in drop_blips(blip, 1.0)] == [10.0, 10.0]
    misprint = [("2025-01-31", 36.36, 2), ("2025-02-28", 36.36, 2), ("2025-03-31", 13.22, 2)]
    assert drop_blips(misprint, 1.0) == misprint


def test_small_integers_do_not_vote():
    idx = build_index(_obs("tiny", [1.0, 2.0, 3.0, 4.0, 5.0, 6.0]))
    row = {"label": "x", "values": {d: [v, 0] for d, v in zip(MONTHS, [1, 2, 3, 4, 5, 6])}}
    assert map_row(row, idx) == []


def test_unconfirmed_outliers_dropped_confirmed_kept():
    from scripts.vintages_sbp_msb import drop_unconfirmed_outliers

    one_off = [("2012-04-30", 11156.81, 2)] + [(f"2012-0{m}-28", 1156.81, 2) for m in (5, 6, 7)]
    assert [p[1] for p in drop_unconfirmed_outliers(one_off, 1156.81, 1.0)] == [1156.81] * 3
    confirmed = [(f"2011-0{m}-28", 36.36, 2) for m in (2, 3, 4)]
    assert drop_unconfirmed_outliers(confirmed, 13.36, 1.0) == confirmed


def test_revised_old_issue_joins_via_family_near_match():
    # Latest issue matches exactly; an older issue printed first prints ~5% lower.
    ours = [2000.0, 2100.0, 2200.0, 2300.0, 2400.0, 2500.0]
    idx = build_index(_obs("bop.exports", ours))
    current = {("bop.exports", d): v for d, v in zip(MONTHS, ours)}
    exact = {"label": "Exports (BOP) Million US$", "values": {d: [v, 0] for d, v in zip(MONTHS, ours)}}
    first = {"label": "Exports (BOP) Million US$", "values": {d: [round(v * 0.95), 0] for d, v in zip(MONTHS, ours)}}
    wrong_unit = {"label": "Exports (BOP) Million US$", "values": {d: [v * 1000, 0] for d, v in zip(MONTHS, ours)}}
    issues = [{"issue": "2025-07", "published": "2025-07-31", "rows": [first]},
              {"issue": "2025-08", "published": "2025-08-31", "rows": [wrong_unit]},
              {"issue": "2025-09", "published": "2025-09-30", "rows": [exact]},
              {"issue": "2025-10", "published": "2025-10-31", "rows": [exact]}]
    mapping, first_seen, revisions = build_vintages(issues, idx, current, TRACK)
    # two exact + one near; the row printed in another unit (x1000) is not mixed in
    assert mapping["bop.exports"]["issues"] == 3
    assert first_seen[("bop.exports", MONTHS[0])] == "2025-07-31"
    assert ("bop.exports", MONTHS[0], 1900.0, "2025-09-30") in revisions


def test_definitional_mismatch_series_is_excluded():
    # The bulletin's total portfolio investment equals our private-only series in
    # most months but not in Eurobond months; the final print never moves towards ours.
    ours = [100.0, 110.0, 120.0, 130.0, 140.0, 150.0]
    printed = [100.0, 110.0, 2115.2, 130.0, 140.0, 150.0]
    idx = build_index(_obs("fpi.private", ours))
    current = {("fpi.private", d): v for d, v in zip(MONTHS, ours)}
    row = {"label": "Foreign Portfolio Investment (Net)", "values": {d: [v, 1] for d, v in zip(MONTHS, printed)}}
    issues = [{"issue": f"2025-0{k}", "published": f"2025-0{k}-28", "rows": [row]} for k in (6, 7, 8)]
    mapping, first_seen, revisions = build_vintages(issues, idx, current, TRACK)
    assert mapping["fpi.private"].get("excluded")
    assert not revisions and not first_seen


def test_seeded_series_take_rows_by_label_across_families():
    """Series seeded from the bulletin are mapped by label family (explicit), across
    the label's spellings over the years; the latest issue's print is the value and
    earlier prints become revisions."""
    from scripts.vintages_core import build_vintages_detailed, explicit_map, label_family, latest_prints

    spec = {"id": "inflation.cpi.national.food.yoy", "doc_source": "sbp_msb",
            "labels": ['CPI Inflation (Foods) "', 'CPI Inflation (Food) "']}
    old = {"issue": "2015-02", "published": "2015-02-28", "source": "sbp_msb",
           "rows": [{"label": 'CPI Inflation (Foods) "', "values": {"2015-01-31": [3.0, 1]}}]}
    new = {"issue": "2015-05", "published": "2015-05-31", "source": "sbp_msb",
           "rows": [{"label": 'CPI Inflation (Food) "', "values": {"2015-01-31": [3.2, 1], "2015-04-30": [2.1, 1]}}]}
    assert label_family('CPI Inflation (Foods) "') != label_family('CPI Inflation (Food) "')
    pts = latest_prints([new, old], spec)
    assert pts == {"2015-01-31": (3.2, "2015-05"), "2015-04-30": (2.1, "2015-05")}
    current = {(spec["id"], d): v for d, (v, _i) in pts.items()}
    mapping, first_seen, revisions, unmatched = build_vintages_detailed(
        [old, new], idx={}, current=current, track=TRACK, explicit=explicit_map([spec]))
    assert spec["id"] in mapping and not unmatched
    assert first_seen[(spec["id"], "2015-01-31")] == "2015-02-28"
    assert revisions == [(spec["id"], "2015-01-31", 3.0, "2015-05-31", "sbp_msb")]


def test_explicit_map_rejects_a_label_claimed_twice():
    import pytest

    from scripts.vintages_core import explicit_map

    with pytest.raises(ValueError):
        explicit_map([{"id": "a", "labels": ["X Rate"]}, {"id": "b", "labels": ["x rate"]}])


def test_header_naming_first_column_year_rolls_forward():
    # 2026 layout: only the first column's year is printed ("2024 / Dec Jan ... May")
    cols, _ = _header_months("2024\nDec Jan Feb Mar Apr May\n1 Currency in Circulation 1 2 3 4 5 6",
                             None, (2026, 1))
    assert cols[0] == (2024, 12) and cols[-1] == (2025, 5)
