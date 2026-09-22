# Scenario enumeration overlay — work-side runbook

**Goal:** list the DIS entity types your scenario emits, check the list, and
send back four numbers. **The list itself stays with you.** Nothing in this
procedure needs it to leave your side.

## What you have

| File | What it is |
|---|---|
| `overlay.empty.json` | The template: an empty JSON list. Fill in a copy. |
| `overlay.example.json` | 12 stock SISO-REF-010 entries, filled in, showing the format. |
| `validate_overlay.py` | Checks a filled overlay. Python 3.9+, standard library only. |

## You also need

The SISO-REF-010 enumerations XML (`SISO-REF-010.xml`), from SISO's document
library or from your simulator's install. The example was checked against
**SISO-REF-010-v37 (2026-05-25)**. A newer release is fine. The validator
prints the title it used.

## Steps

**1. Copy the template.** Never edit `overlay.empty.json` in place.

```
cp overlay.empty.json scenario-overlay.json
```

**2. Add one row per entity type the scenario emits,** platforms and munitions
both:

```json
{"type": [kind, domain, country, category, subcategory, specific, extra], "variant": "<your label>"}
```

- `kind` must be **1** (platform) or **2** (munition). List munitions as well
  as platforms. They are the part we most need counted.
- `variant` is your label for the type. Printable ASCII, at most 64 characters.
- Take the tuples from the scenario or the simulator's entity list. Don't
  retype them from a document.
- Each type goes in once, however many entities of that type the scenario
  uses.

**3. Validate against SISO:**

```
python validate_overlay.py scenario-overlay.json --siso /path/to/SISO-REF-010.xml
```

- `RESULT: REFUSED` means there is at least one error. Fix each row it names
  and run step 3 again.
- A `WARN` line does not stop you. The usual one, `type not defined in
  SISO-REF-010-...`, means that row is a locally defined type. That can be
  legitimate, but check it for a typo first.
- The output holds only counts and row numbers, never a tuple or a label, so
  you can copy it as it stands.

**4. Optional smoke test.** Point dis-sim at the file. The **first line**
should read `loaded N entity type(s) ... kinds present: [...]`, with N equal to
`ROWS`. Ignore the list printed after it: that is the lab's built-in default,
not your file.

```
DIS_ENTITY_TYPES_PATH=scenario-overlay.json python ../dis_sim.py --list-types
```

## What the check does not catch

A tuple that exists in SISO but names a **different** platform than you meant
still passes. The validator checks that a type exists, not that it is the one
you intended. Take the tuples from the simulator itself, which is what step 2
asks.

## Report back — four numbers

Copy them from the `REPORT BACK` block of a `RESULT: VALID` run, and name the
SISO release the run was checked against:

| # | Number | Meaning |
|---|---|---|
| 1 | `ROWS` | distinct entity types in the scenario |
| 2 | `KIND_1` | how many are platforms |
| 3 | `KIND_2` | how many are munitions |
| 4 | `NOT_IN_SISO` | how many are not in the SISO release you checked against |

Report `NOT_IN_SISO` as a number. If it reads `NOT RUN`, go back to step 3 and
run it with `--siso`.
