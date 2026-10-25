-- Seed the series catalog. Idempotent (ON CONFLICT).
-- KIBOR tenors: bid/offer carried in the `side` dimension on observations.
-- The live SBP page publishes the 3-M, 6-M and 12-M (=1Y) benchmark tenors; the
-- older 1W/2W/1M/9M rows and the archive pages were dropped in the 2026 site
-- redesign. Deep KIBOR history is ingested separately from EasyData
-- (gp_bam_sirkibor_d.*); this scraper provides same-day freshness.

INSERT INTO series (id, module, name, description, unit, frequency, source, source_url, dimensions, min_value, max_value, max_step, tier)
VALUES
 ('rates.kibor.3m',  'fixed-income', 'KIBOR 3-Month',  'Karachi Interbank Offered Rate, 3-month tenor (bid/offer)', 'percent', 'daily', 'SBP', 'https://www.sbp.org.pk/ecodata/kibor/kibor.asp', '{"side":["bid","offer"]}', 0, 40, 5, 'basic'),
 ('rates.kibor.6m',  'fixed-income', 'KIBOR 6-Month',  'Karachi Interbank Offered Rate, 6-month tenor (bid/offer)', 'percent', 'daily', 'SBP', 'https://www.sbp.org.pk/ecodata/kibor/kibor.asp', '{"side":["bid","offer"]}', 0, 40, 5, 'basic'),
 ('rates.kibor.1y',  'fixed-income', 'KIBOR 1-Year',   'Karachi Interbank Offered Rate, 12-month tenor (bid/offer)', 'percent', 'daily', 'SBP', 'https://www.sbp.org.pk/ecodata/kibor/kibor.asp', '{"side":["bid","offer"]}', 0, 40, 5, 'basic')
ON CONFLICT (id) DO UPDATE SET
    module=EXCLUDED.module, name=EXCLUDED.name, description=EXCLUDED.description,
    unit=EXCLUDED.unit, frequency=EXCLUDED.frequency, source=EXCLUDED.source,
    source_url=EXCLUDED.source_url, dimensions=EXCLUDED.dimensions,
    min_value=EXCLUDED.min_value, max_value=EXCLUDED.max_value,
    max_step=EXCLUDED.max_step, tier=EXCLUDED.tier, is_active=true;

-- Retire tenors no longer published on the live page (keep any past data, hide
-- from the active catalog).
UPDATE series SET is_active = false
 WHERE id IN ('rates.kibor.1w', 'rates.kibor.2w', 'rates.kibor.1m', 'rates.kibor.9m');

-- SBP daily market snapshot, parsed from the same kibor.asp page by sbp_kibor
-- (2026-10-04). Daily interbank FX was the long-requested gap: EasyData only has
-- monthly averages. Accrues from 2026-10-04 (the live page shows today only).
INSERT INTO series (id, module, name, description, unit, frequency, source, source_url, dimensions, min_value, max_value, max_step, tier)
VALUES
 ('fx.rate.m2m.usd', 'forex', 'USD/PKR M2M revaluation rate (daily)', 'State Bank of Pakistan''s daily mark-to-market (M2M) revaluation rate for the US dollar in rupees — the official daily reference rate banks use to revalue FX positions.', 'pkr', 'daily', 'SBP', 'https://www.sbp.org.pk/ecodata/kibor/kibor.asp', '{}', 100, 1000, 30, 'basic'),
 ('fx.rate.interbank.usd', 'forex', 'USD/PKR interbank rate (daily, bid/offer)', 'Weighted-average interbank USD/PKR exchange rate published daily by the State Bank of Pakistan, bid and offer (side dimension).', 'pkr', 'daily', 'SBP', 'https://www.sbp.org.pk/ecodata/kibor/kibor.asp', '{"side":["bid","offer"]}', 100, 1000, 30, 'basic'),
 ('reserves.liquid.sbp', 'external', 'Liquid FX reserves held by SBP (weekly)', 'Liquid foreign exchange reserves held by the State Bank of Pakistan, USD million, weekly as published by SBP.', 'usd_mn', 'weekly', 'SBP', 'https://www.sbp.org.pk/ecodata/kibor/kibor.asp', '{}', 0, 100000, 5000, 'basic'),
 ('reserves.liquid.banks', 'external', 'Liquid FX reserves held by commercial banks (weekly)', 'Net liquid foreign exchange reserves held by Pakistan''s commercial banks, USD million, weekly as published by SBP.', 'usd_mn', 'weekly', 'SBP', 'https://www.sbp.org.pk/ecodata/kibor/kibor.asp', '{}', 0, 100000, 5000, 'basic'),
 ('reserves.liquid.total', 'external', 'Total liquid FX reserves of Pakistan (weekly)', 'Total liquid foreign exchange reserves of Pakistan (SBP plus commercial banks), USD million, weekly as published by SBP.', 'usd_mn', 'weekly', 'SBP', 'https://www.sbp.org.pk/ecodata/kibor/kibor.asp', '{}', 0, 100000, 5000, 'basic'),
 ('rates.repo.overnight', 'fixed-income', 'Weighted-average overnight repo rate (daily)', 'Weighted-average overnight repo rate in Pakistan''s money market, percent per annum, as published daily by the State Bank of Pakistan.', 'percent', 'daily', 'SBP', 'https://www.sbp.org.pk/ecodata/kibor/kibor.asp', '{}', 0, 40, 5, 'basic')
ON CONFLICT (id) DO UPDATE SET
    module=EXCLUDED.module, name=EXCLUDED.name, description=EXCLUDED.description,
    unit=EXCLUDED.unit, frequency=EXCLUDED.frequency, source=EXCLUDED.source,
    source_url=EXCLUDED.source_url, dimensions=EXCLUDED.dimensions,
    min_value=EXCLUDED.min_value, max_value=EXCLUDED.max_value,
    max_step=EXCLUDED.max_step, tier=EXCLUDED.tier, is_active=true;
