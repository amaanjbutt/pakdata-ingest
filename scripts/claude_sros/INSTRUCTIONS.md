# Transcribe an FBR customs SRO from page images

You are transcribing a Pakistani customs notification (an SRO of the Federal Board of Revenue: regulatory
duty, additional customs duty, or an amendment to one) into structured data. The data is published as
official figures. **Transcribe only what is printed. Never guess, infer, complete or "correct" a value.**
If something is unreadable, leave it out and say so in `notes`.

## Input
Your task names the SRO and gives the absolute paths of PNG page images, in order. Open every page image
with the Read tool, in order.

## Output
Write ONE JSON file (path given in your task):

```json
{
  "sro": "1152(I)/2025",
  "pages": [1, 2, 3],
  "rows": [
    {"page": 2, "sno": "11", "codes": ["03.04"],
     "description": "Fish fillets and other fish meat (whether or not minced), fresh, chilled or frozen (PCT code except 0304.6200).",
     "rate": "17.5"}
  ],
  "text": [{"page": 1, "text": "S.R.O. 1152(I)/2025.- In exercise of the powers conferred by sub-section (3) of section 18 ..."}],
  "notes": null
}
```

### `rows`: the table(s)
ONE ROW PER TABLE ROW, in the order printed.
- `page`: the page number (1 = the first image you were given for this SRO, counting all of its pages, even
  if your task gives you only some of them — your task says which page number each image is).
- `sno`: the serial number as printed, without the trailing dot ("11.", "11" → "11"; "45A" stays "45A").
  null if the row has none (e.g. a row continued from the previous page — then set `"continued": true`).
- `codes`: every PCT code printed in the code cell, exactly as printed, digits and dots only
  ("0304.6200", "03.04", "0305.31", "8703"). Several codes in one cell → all of them, in order. [] if none.
- `description`: the description cell as printed (join wrapped lines with spaces; keep "except …",
  "CBU", "of value exceeding …" and every condition; italics don't matter).
- `rate`: the rate cell exactly as printed, as a string ("17.5", "10", "35%", "Rs.300 per kg",
  "50% or US$ 5 per kg whichever is higher"). Read the digits and the DECIMAL POINT with care: "17.5",
  "8.0" and "12.5" are common, and a decimal point is easy to miss. If a table has more than one rate
  column (e.g. columns for different conditions or dates), put each in `rates` as {"header": rate} instead
  of `rate`.
- If a table has other columns (e.g. "Condition"), add them as extra keys named after the header.

### `text`: everything else
The preamble, any headings between tables, conditions, provisos, explanations and the closing lines, as
printed (one entry per block, with its page). For an AMENDMENT SRO the clauses are the whole content:
transcribe each clause in full, e.g.
"(a) against S. No. 23, in column (4), for the figure \"10\" the figure \"15\" shall be substituted;"
and also put any table the amendment inserts ("after S. No. 45 and the entries relating thereto, the
following new S. Nos. and entries shall be inserted, namely:-") in `rows`, with `"inserted_after": "45"`
on each of those rows.

Before writing, re-check every row against the image: digits (1 vs 7, 5 vs 6, 0 vs 8, 3 vs 8), decimal
points, and that each rate sits on the right serial number and code. Write valid JSON (no comments, no
trailing commas).
