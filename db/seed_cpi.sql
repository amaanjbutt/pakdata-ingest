-- PBS headline price indices + YoY inflation (Phase 5.3). Idempotent.
INSERT INTO series (id, module, name, description, unit, frequency, source, source_url, dimensions, min_value, max_value, max_step, tier)
VALUES
 ('inflation.cpi.national',     'economic', 'CPI National',        'Consumer Price Index, national (Base 2015-16)', 'index',   'monthly', 'PBS', 'https://www.pbs.gov.pk/price-statistics/', '{}', 0, 2000, 40, 'basic'),
 ('inflation.cpi.urban',        'economic', 'CPI Urban',           'Urban Consumer Price Index (Base 2015-16)',     'index',   'monthly', 'PBS', 'https://www.pbs.gov.pk/price-statistics/', '{}', 0, 2000, 40, 'basic'),
 ('inflation.cpi.rural',        'economic', 'CPI Rural',           'Rural Consumer Price Index (Base 2015-16)',     'index',   'monthly', 'PBS', 'https://www.pbs.gov.pk/price-statistics/', '{}', 0, 2000, 40, 'basic'),
 ('inflation.wpi',              'economic', 'WPI',                 'Wholesale Price Index (Base 2015-16)',          'index',   'monthly', 'PBS', 'https://www.pbs.gov.pk/price-statistics/', '{}', 0, 2000, 40, 'basic'),
 ('inflation.cpi.national.yoy', 'economic', 'CPI National YoY',    'Headline inflation, year-on-year percent',      'percent', 'monthly', 'PBS', 'https://www.pbs.gov.pk/price-statistics/', '{}', -50, 100, 30, 'basic'),
 ('inflation.cpi.urban.yoy',    'economic', 'CPI Urban YoY',       'Urban inflation, year-on-year percent',         'percent', 'monthly', 'PBS', 'https://www.pbs.gov.pk/price-statistics/', '{}', -50, 100, 30, 'basic'),
 ('inflation.cpi.rural.yoy',    'economic', 'CPI Rural YoY',       'Rural inflation, year-on-year percent',          'percent', 'monthly', 'PBS', 'https://www.pbs.gov.pk/price-statistics/', '{}', -50, 100, 30, 'basic'),
 ('inflation.wpi.yoy',          'economic', 'WPI YoY',             'Wholesale inflation, year-on-year percent',      'percent', 'monthly', 'PBS', 'https://www.pbs.gov.pk/price-statistics/', '{}', -50, 100, 40, 'basic')
ON CONFLICT (id) DO UPDATE SET
    module=EXCLUDED.module, name=EXCLUDED.name, description=EXCLUDED.description,
    unit=EXCLUDED.unit, frequency=EXCLUDED.frequency, source=EXCLUDED.source,
    source_url=EXCLUDED.source_url, min_value=EXCLUDED.min_value,
    max_value=EXCLUDED.max_value, max_step=EXCLUDED.max_step, tier=EXCLUDED.tier;
