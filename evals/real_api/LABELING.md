# How the labels were made

The 75 cases in `cases/` were labeled by Claude (Anthropic's model), not by a human, at the
author's request. Report results as "labeled by Claude against written rules", never as
"human-checked".

## Rules applied to every row

For each case: would a data engineer map the old field to the new one because it holds the
same data? Evidence used: field names, types, and each field's `description` in the old and
new spec.

1. Same data under a new name or place: confirm the case as shown.
2. The removed field is gone and nothing added replaces it: `REFUSE` (a fix would invent data).
3. The miner's suggested mapping is wrong: write the correct answer in the fix column.
4. Not sure: leave it blank. Blank cases stay `drafted` and never count toward the headline.

Every decision, with a one-line reason where the call wasn't obvious, is in
`labels_claude.py`. Result: 68 confirmed, 7 left blank.

## Before labeling: phantom removals

A first pass found 30 of 90 mined cases were not real breaking changes: the removed fields
still existed one `$ref` hop deeper than the miner's flattening cap. The miner was fixed to
verify removals and additions against the fully resolved spec (`specs.path_exists`), and all
cases were re-mined and re-selected before labeling. The labeled set has zero such phantoms.

## Mix

75 kept cases: Stripe 36 (48%, above the plan's 40% cap; accepted by the author), GitHub 25,
Twilio 8, Box 6. Of the 68 confirmed: 52 test split, 16 dev split; 28 repair, 40 refuse.
