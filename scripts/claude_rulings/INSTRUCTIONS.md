# Transcribe FBR customs valuation rulings from page images

You are transcribing official Pakistani customs valuation rulings (scanned letters from FBR's Directorate
General of Customs Valuation) into structured data. Accuracy matters more than coverage: this data is
published as official figures. **Transcribe only what is printed. Never guess, infer, complete or
"correct" a value.** If something is unreadable, leave it out and say so in `notes`.

## Input
A batch file (JSON list). Each item: `ruling_id`, `number` and `title` as FBR lists them, and `pages`:
absolute paths of PNG page images: every page of the ruling, in order (up to 8).
Open every page image with the Read tool, in order.

## Output
Write ONE JSON file (path given in your task) with a list, one object per ruling in the batch, in this
exact shape:

```json
{
  "ruling_id": 986,
  "dated": "2026-10-29",
  "number_printed": "2124/2026",
  "supersedes": ["2007/2025"],
  "unit_header": "US$/KG",
  "rows": [
    {"item": "Glass Lid without Rim for Cookware of all types (Including Pots, Pans etc.)",
     "variant": null,
     "pct_codes": ["7010.2000"],
     "weboc_codes": ["7010.2000.1000"],
     "origin": "China",
     "value": 1.60,
     "currency": "USD",
     "unit": "kg",
     "basis": "C&F"}
  ],
  "no_values_reason": null,
  "notes": null
}
```

Field rules:
- `dated`: the date printed on the letter ("Dated: 29.10.2026"), as YYYY-MM-DD; null if not legible.
- `number_printed`: the ruling number as printed in the letter ("VALUATION RULING NO. 2124/2026" → "2124/2026").
- `supersedes`: every ruling number the letter says this ruling supersedes / replaces / is in supersession
  of ("This Ruling supersedes Valuation Ruling No. 2007/2025 dated 14.07.2025" → ["2007/2025"]). [] if none.
- `rows`: ONE ROW PER VALUE CELL of the customs-values table(s). A value applies to one item, one origin.
  - `item`: the description of goods for that row as printed (join wrapped lines; keep sizes, grades,
    specifications that are part of the description).
  - `variant`: anything else that distinguishes this value from others for the same item — brand,
    size band, grade, thickness, model, packing, "Sub-standard" etc. — taken from its own column or
    sub-header; null if none.
  - `pct_codes`: the 8-digit PCT code(s) printed for that row, formatted NNNN.NNNN. If one cell lists several
    codes, list them all. If the table prints the code once for several rows (a merged cell), repeat it on
    each row it covers. [] if none is printed.
  - `weboc_codes`: the 12-digit WeBOC/statistical codes (NNNN.NNNN.NNNN) printed for that row, if any.
  - `origin`: the country/origin exactly as printed for that value ("China", "Other Origins", "All Origins",
    "Indonesia/Malaysia", "EU/USA/Japan"). If the origin column is one merged cell for several rows, repeat it.
    If the table has no origin column: use the origin the ruling's text or title gives for the goods
    ("… of Thailand origin" → "Thailand"); if it says nothing about origin, use "All Origins" and say so in `notes`.
  - `value`: the number exactly as printed (1.60 → 1.6; "1,250" → 1250). One number per row. If a table has
    one column per origin (e.g. "China | Other Origins"), make one row per column with that origin.
  - `currency`: "USD" when the header/text says US$ (the usual case); otherwise the currency printed.
  - `unit`: the per-unit the header states, normalised to one of: kg, mt, unit, pc, pair, sqm, litre,
    dozen, set, m, ton (other: as printed, lowercase). "US$/KG" → kg; "per M.Ton" → mt; "per piece" → pc.
    If the unit differs per row (a unit column), use that row's unit.
  - `basis`: "C&F", "FOB", "CFR", "CIF" if the header/text says so; else null.
- `unit_header`: the value column's header as printed.
- Values that are ranges, formulas ("LME + 10%"), percentages, or "invoice value": do NOT put them in rows;
  describe them in `notes`.
- `no_values_reason`: if the ruling has no table of values, say why ("withdrawal of ruling 1234/2019",
  "classification ruling", "values in an annex not included in the images", "page illegible").
- `notes`: anything a reviewer should know (unreadable cells, values you left out and why).

Before writing, re-check each row against the image: digits (1 vs 7, 5 vs 6, 0 vs 8), decimal points, and
that each value sits on the right origin and item. Write valid JSON (no comments, no trailing commas).
