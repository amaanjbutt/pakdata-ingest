"""Pure parts of the data-quality audit (scripts/dq_audit.py)."""
from datetime import date

from scripts.dq_audit import DETECTORS, compare_points, same_value, verdict_for

D1, D2, D3, D4 = date(2026, 1, 31), date(2026, 2, 28), date(2026, 3, 31), date(2026, 4, 30)


def test_same_value_tolerates_float_noise_only():
    assert same_value(278.5795536, 278.57955360000003)
    assert not same_value(9.2, 9.3)
    assert same_value(None, None)
    assert not same_value(None, 0.0)


def test_compare_points_classifies_every_difference():
    ours = {D1: 1.0, D2: 3643.8, D3: 0.02}
    source = {D1: 1.0, D2: 0.018, D4: 0.5}
    matched, diffs = compare_points(ours, source)
    assert matched == 1
    assert [(d.obs_date, d.kind) for d in diffs] == [
        (D2, "value"), (D3, "missing_source"), (D4, "missing_ours")]


def test_compare_points_ignores_source_nulls_we_lack():
    matched, diffs = compare_points({D1: 1.0}, {D1: 1.0, D2: None})
    assert matched == 1 and diffs == []


def test_verdicts():
    src = {D1: 9.2, D2: 3643.796589}
    assert verdict_for(9.2, src, D1) == ("confirmed_as_published", 9.2)
    assert verdict_for(9.3, src, D1) == ("source_differs", 9.2)
    assert verdict_for(1.0, src, D3) == ("not_in_source", None)
    # our nightly unit-slip fix stored 3.643796589; the source still prints the slip
    assert verdict_for(3.643796589, src, D2, scale_fixed_from=3643.796589) == ("scale_corrected", 3643.796589)


def test_detectors_are_parameter_safe():
    # psycopg treats a bare % as a placeholder: LIKE patterns must be written %%
    for name, sql in DETECTORS.items():
        stripped = sql.replace("%%", "")
        assert "%" not in stripped, name


def test_notes_only_for_extreme_confirmed_values():
    from scripts.dq_audit import note_for
    n = note_for("spike_level", 3643.796589, {"prev": 0.017068677, "next": 0.020630961}, "SBP",
                 "easydata:TS_GP_BOP_SCRA_D.X")
    assert n.startswith("As published by SBP (EasyData TS_GP_BOP_SCRA_D.X)") and "210,000 times above" in n
    # no exact 1,000^k puts it between its neighbours: no claim about a unit slip
    assert "unit slip" not in n
    slip = note_for("spike_level", 3375.0, {"prev": 3.2, "next": 3.5}, "SBP", None)
    assert slip.endswith("which suggests a unit slip in the source's figure for this period.")
    # an ordinary lumpy flow (5x) gets no note
    assert note_for("spike_level", 5.0, {"prev": 1.0, "next": 1.1}, "SBP", None) is None
    assert "percentage" in note_for("out_of_range", 1240, {}, "SBP", None)


def test_rename_qualifiers():
    from datetime import date

    from scripts.disambiguate_series_names import complete_clipped, qualify
    assert complete_clipped("Services — GDP at Constant Basic Prices of",
                            "Quarterly GDP at Constant Basic Prices of 2015-16").endswith("Prices of 2015-16")
    base = dict(unit="pkr_mn", first_date=date(2014, 6, 30), last_date=date.today())
    g = [dict(id="a", frequency="quarterly", ds="M2_Q", **base), dict(id="b", frequency="weekly", ds="M2_W", **base),
         dict(id="c", frequency="quarterly", ds="RM_Q", **base)]
    q = qualify(g, {"M2_Q": "Quarterly Broad Money M2", "RM_Q": "Quarterly Reserve Money RM", "M2_W": "Weekly Broad Money M2"})
    assert q == {"a": "quarterly, Broad Money M2", "b": "weekly", "c": "quarterly, Reserve Money RM"}
    same = [dict(id="x", frequency="quarterly", ds="D", **base), dict(id="y", frequency="quarterly", ds="D", **base)]
    assert qualify(same, {}, {}, {"x": "DS00160", "y": "DS00440"}) == {"x": "SBP series DS00160", "y": "SBP series DS00440"}


def test_fund_history_trust_and_rounding():
    from scripts.dq_audit import _rounds_to, history_trusted
    days = [date(2026, 9, d) for d in range(1, 11)]
    ours = {d: 100 + i for i, d in enumerate(days)}
    assert history_trusted(ours, dict(ours))
    # another fund's series served under this id (MUFAP's history endpoint for 12683)
    assert not history_trusted(ours, {d: 532 + i for i, d in enumerate(days)})
    # payout day: history prints the cum-payout NAV, the daily table the ex-NAV
    assert not history_trusted(ours, {**ours, days[-1]: 101.7324})
    # history lags by a day and has extra points we lack: still trusted
    assert history_trusted({**ours, date(2026, 9, 11): 111}, {**ours, date(2026, 8, 30): 99})
    assert _rounds_to(16.466, 16.47) and not _rounds_to(16.466, 16.48) and _rounds_to(16.305, 16.31) and not _rounds_to(16.4661, 16.4662)


def test_write_source_values_survives_a_fully_rejected_batch(monkeypatch):
    """An SBP figure outside our bounds (a 30x source typo) must not stop the whole audit run."""
    from collections import Counter

    import app.db
    from ingestion.framework import ValidationError
    from scripts import dq_audit

    monkeypatch.setattr(app.db, "query", lambda sql, params=None: (
        [] if "obs_scale_corrections" in sql else [{"obs_date": date(2024, 12, 31), "v": 77766.0}]))

    class _Job:
        def validate(self, recs):
            raise ValidationError("all 1 record(s) rejected; first: x > max")

        def upsert(self, recs):  # pragma: no cover - never reached
            raise AssertionError

    dq_audit._ALERTS.clear()
    stats: Counter = Counter()
    dq_audit._write_source_values(_Job(), "money.x", {date(2024, 12, 31): 77766.0,
                                                    date(2025, 1, 31): 2773006.0}, stats)
    assert stats["points_rejected"] == 1
    assert dq_audit._ALERTS and "money.x" in dq_audit._ALERTS[0]
    dq_audit._ALERTS.clear()
