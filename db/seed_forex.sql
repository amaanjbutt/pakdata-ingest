-- Forex catalog.
--
-- The daily interbank/M2M scrape (fx.interbank.*) is RETIRED and seeded INACTIVE.
-- SBP removed the M2M-Current.asp page in its July 2026 site redesign (every
-- /ecodata/*.asp URL now returns the KIBOR catch-all with no currency table), and
-- EasyData carries no daily FX. FX is therefore sourced from EasyData's monthly
-- exchange-rate datasets instead — fx.avg.* / fx.monthend.* / fx.reer / fx.neer,
-- seeded programmatically from ingestion/easydata_series.json by db.init's
-- _seed_easydata_catalog(). These legacy rows are kept for audit but marked
-- is_active=false so they stay out of the catalog, /v1/status, and query surface
-- (they never carried data). Idempotent.

INSERT INTO series (id, module, name, description, unit, frequency, source, source_url, dimensions, min_value, max_value, max_step, tier, is_active)
VALUES
 ('fx.interbank.usd', 'forex', 'Interbank USD/PKR (retired)', 'Retired: SBP daily M2M page removed in 2026 redesign; use fx.avg.usd / fx.monthend.usd', 'pkr', 'daily', 'SBP', 'https://www.sbp.org.pk/ecodata/rates/m2m/M2M-Current.asp', '{}', 0, 2000, 30, 'basic', false),
 ('fx.interbank.eur', 'forex', 'Interbank EUR/PKR (retired)', 'Retired: use fx.avg.eur / fx.monthend.eur', 'pkr', 'daily', 'SBP', 'https://www.sbp.org.pk/ecodata/rates/m2m/M2M-Current.asp', '{}', 0, 2000, 30, 'basic', false),
 ('fx.interbank.gbp', 'forex', 'Interbank GBP/PKR (retired)', 'Retired: use fx.avg.gbp / fx.monthend.gbp', 'pkr', 'daily', 'SBP', 'https://www.sbp.org.pk/ecodata/rates/m2m/M2M-Current.asp', '{}', 0, 2000, 30, 'basic', false),
 ('fx.interbank.jpy', 'forex', 'Interbank JPY/PKR (retired)', 'Retired: use fx.avg.jpy / fx.monthend.jpy', 'pkr', 'daily', 'SBP', 'https://www.sbp.org.pk/ecodata/rates/m2m/M2M-Current.asp', '{}', 0, 100, 5, 'basic', false),
 ('fx.interbank.sar', 'forex', 'Interbank SAR/PKR (retired)', 'Retired: use fx.avg.sar / fx.monthend.sar', 'pkr', 'daily', 'SBP', 'https://www.sbp.org.pk/ecodata/rates/m2m/M2M-Current.asp', '{}', 0, 2000, 30, 'basic', false),
 ('fx.interbank.aed', 'forex', 'Interbank AED/PKR (retired)', 'Retired: use fx.avg.aed / fx.monthend.aed', 'pkr', 'daily', 'SBP', 'https://www.sbp.org.pk/ecodata/rates/m2m/M2M-Current.asp', '{}', 0, 2000, 30, 'basic', false),
 ('fx.interbank.cny', 'forex', 'Interbank CNY/PKR (retired)', 'Retired: use fx.avg.cny / fx.monthend.cny', 'pkr', 'daily', 'SBP', 'https://www.sbp.org.pk/ecodata/rates/m2m/M2M-Current.asp', '{}', 0, 2000, 30, 'basic', false)
ON CONFLICT (id) DO UPDATE SET
    name=EXCLUDED.name, description=EXCLUDED.description, is_active=false;
