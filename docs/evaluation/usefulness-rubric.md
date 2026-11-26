# Usefulness rating: rubric and procedure

Rubric version 1.0.0 (`packages/hacore/src/hacore/rules/usefulness-rubric.toml`, the
single source of the wording for the human form and the AI raters). It implements
sections 3 and 6 of the preregistration (`usefulness-prereg.md`); where the two differ,
the preregistration wins.

## What a rater sees

One finding at a time, in plain words, made by `hacore.public.rating_kit.describe`:

- a **relation**: which series rises or falls when the other rises, and the delay;
- an **unusual episode**: the series, the hours, the observed and the usual value;
- a **lasting change**: the series, the day, the level before and after;
- context facts of the home: both series in one room, a series of the whole home,
  unusual outdoor temperature at the same time, a one-hour episode, an earlier change
  of level.

A rater never sees the strength, the evidence, the score, p or q, the rulebook's
category or triviality verdict, the method's structure flags (`indirect`,
`common_factor`), the set, the stratum or any ranking. Every rater and every AI role
gets the findings in an order of its own. A test (`tests/test_rating_kit.py`) checks the
blinding.

## Rubric

The analysis looked at about 90 days of hourly data from the sensors and meters of one
home. Judge each finding from the point of view of the people who live in the home and
look after it; take it as true and judge whether it is worth their attention.

**How useful is this finding to the household?**

| Value | Label | Meaning |
|---|---|---|
| 1 | Nothing to learn | Obvious, impossible to understand, or only about how the data were collected. The household learns nothing. |
| 2 | Interesting, no action | Not obvious and true to life, but there is nothing to check or do about it. |
| 3 | Worth checking | It may point to a fault, a waste, an opportunity or a habit worth a look; someone would check it. |
| 4 | Act on it | A concrete action follows (repair, a setting, an automation, a change of habit) with a likely benefit. |

**Is the finding trivial?** Yes when it follows from how the home or the measurement is
built, whatever its use: one device measured twice or by two of its own sensors, a value
and a helper computed from it, two sensors of the same quantity in one room, known
physics (solar output and sunshine, a heat pump and the outdoor temperature, a battery
and the grid), or series that drop out together because they come from one integration.
Otherwise no.

**Which kind of action does the finding most call for?** `fault` (something may be
broken, wrongly set or measuring wrong), `savings` (energy or money could be saved),
`automation` (something could be automated or an automation adjusted), `behaviour` (a
habit, comfort or routine could change), `none`.

## Languages

The panel rates in Czech and German. `usefulness-rubric.cs.toml` and
`usefulness-rubric.de.toml` hold the same rubric version in those languages: the same
scale, answers, placeholders and context facts as the English source (a test checks the
structure; the meaning is checked by the panel in the pilot). A rater gets the findings
in the same order in every language, so a form can be swapped for another language.
Answers are written in the form's language (`ano`/`ne`, `ja`/`nein`, the category words
of the rubric) and mapped back to the codes by `Rubric.normalise`. Channel names of the
public datasets stay as the dataset has them. The AI raters use the English source only.

## Human panel

Three raters: the author and two beta testers. Codes `r1`–`r3` on the forms; who is
who is recorded outside the repository.

1. **Pilot** (20 calibration findings outside the sample, `pilot.csv`): each rater fills
   `forms-pilot/<lang>/form-<code>.csv` (`cs` or `de`) alone. Afterwards the panel discusses the
   disagreements. The pilot may change the wording of the rubric (a new version of the
   TOML and this file), never the scale. Pilot ratings are not used in any estimate.
2. **Gate sample** (200 findings, `forms-gate/<lang>/`): each rater alone, without talking
   about findings until all three forms are in, in the order of the form, in one or
   more sittings. Fill `usefulness` (1–4), `trivial` (yes/no in the form's language),
   `category` (one of the five words of the rubric) and, optionally, a `note`. A finding you cannot understand is a 1.
3. **Adjudication:**
   put the three filled forms into one folder, then
   `python -m hacore.public.rating_kit adjudicate --ratings <folder> --out adjudication.csv`.
   Spread ≤ 1: median. Spread ≥ 2 (`discuss`): the panel discusses the finding, then
   votes again independently (`revote`); a majority of 2 of 3 wins, else the median of
   the revote. One sentence of reason per discussed finding. No rater has a casting
   vote. The independent first ratings are kept and are what the agreement among
   humans (α ≥ 0.667) is computed from.

## AI raters

Three roles, each the same rubric with its own question: technician (fault or data
error?), owner (would I do something?), energy adviser (savings?). One finding per call,
temperature 0, the exact model id recorded, raw outputs (JSONL) kept with their dates:

    python -m hacore.public.rating_kit requests --sample sample.csv --model <exact id> --out ai-requests.jsonl
    # send as a Message Batch, download the results, then
    python -m hacore.public.rating_kit parse --results ai-results.jsonl --out ai-ratings.csv

An answer outside the rubric is reported as failed, not dropped silently. The AI
consensus is the median of the three roles. The test set is rated by a model other
than the one whose ratings train the ranking model (prereg section 6).

## Files

The manual job `usefulness-sample` of the validation workflow draws the sample and the
pilot and writes the blinded forms in Czech and German (`sample.csv`, `pilot.csv`,
`forms-pilot/{cs,de}/`, `forms-gate/{cs,de}/`, each with its `rubric.txt`, `SOURCE.txt`, an artifact kept 30 days). The findings come
from a public dataset (CC BY 4.0) and are published with the citation; nothing of a
user's home is involved.
