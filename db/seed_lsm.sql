-- PBS Large Scale Manufacturing (Quantum Index, QIM). Idempotent.
INSERT INTO series (id, module, name, description, unit, frequency, source, source_url, dimensions, min_value, max_value, max_step, tier)
VALUES
 ('industry.lsm.qim',     'economic', 'LSM Quantum Index (QIM)',   'Quantum Index of Large Scale Manufacturing output (Base 2015-16)', 'index',   'monthly', 'PBS', 'https://www.pbs.gov.pk/qim', '{}', 0, 1000, 60, 'basic'),
 ('industry.lsm.qim.mom', 'economic', 'LSM QIM MoM Change',        'Month-on-month percent change in LSM output',                     'percent', 'monthly', 'PBS', 'https://www.pbs.gov.pk/qim', '{}', -100, 100, 60, 'basic'),
 ('industry.lsm.qim.yoy', 'economic', 'LSM QIM YoY Change',        'Year-on-year percent change in LSM output',                       'percent', 'monthly', 'PBS', 'https://www.pbs.gov.pk/qim', '{}', -100, 100, 60, 'basic')
ON CONFLICT (id) DO UPDATE SET
    module=EXCLUDED.module, name=EXCLUDED.name, description=EXCLUDED.description,
    unit=EXCLUDED.unit, frequency=EXCLUDED.frequency, source=EXCLUDED.source,
    source_url=EXCLUDED.source_url, min_value=EXCLUDED.min_value,
    max_value=EXCLUDED.max_value, max_step=EXCLUDED.max_step, tier=EXCLUDED.tier;
