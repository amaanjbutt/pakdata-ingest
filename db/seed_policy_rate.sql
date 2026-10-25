-- SBP policy rate + interest-rate corridor (Phase 4.2). Idempotent.
INSERT INTO series (id, module, name, description, unit, frequency, source, source_url, dimensions, min_value, max_value, max_step, tier)
VALUES
 ('rates.policy',         'fixed-income', 'SBP Policy Rate',            'SBP policy (target) rate, set by the Monetary Policy Committee', 'percent', 'irregular', 'SBP', 'https://www.sbp.org.pk/our-operations/monetary-policy', '{}', 0, 40, 5, 'basic'),
 ('rates.policy.floor',   'fixed-income', 'SBP Overnight Repo (Floor)', 'SBP overnight repo rate — floor of the interest-rate corridor',  'percent', 'irregular', 'SBP', 'https://www.sbp.org.pk/our-operations/monetary-policy', '{}', 0, 40, 5, 'basic'),
 ('rates.policy.ceiling', 'fixed-income', 'SBP Reverse Repo (Ceiling)', 'SBP overnight reverse repo rate — ceiling of the corridor',      'percent', 'irregular', 'SBP', 'https://www.sbp.org.pk/our-operations/monetary-policy', '{}', 0, 40, 5, 'basic')
ON CONFLICT (id) DO UPDATE SET
    module=EXCLUDED.module, name=EXCLUDED.name, description=EXCLUDED.description,
    unit=EXCLUDED.unit, frequency=EXCLUDED.frequency, source=EXCLUDED.source,
    source_url=EXCLUDED.source_url, min_value=EXCLUDED.min_value,
    max_value=EXCLUDED.max_value, max_step=EXCLUDED.max_step, tier=EXCLUDED.tier;
