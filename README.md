# LP Terms Monitor (prototype v0.5)

A local prototype that turns private-fund **side letters and amendments** into a verified, versioned register of investor reporting deadlines.

The AI finds and proposes. Code checks that every proposed value is literally written in an exact span of the source document, and does all date arithmetic. A named human approves every change to the register. Each approval is a versioned, audited transaction.

> Not legal advice and not production software. **All funds, investors, clauses and dates in this repository are synthetic (made up).** Do not upload confidential or client documents.

The written brief is in [`docs/LP_Terms_Monitor_Brief.pdf`](docs/LP_Terms_Monitor_Brief.pdf).

## Quick start

Python 3.10 or newer.

```bash
python -m venv .venv
source .venv/bin/activate          # Windows: .venv\Scripts\activate
pip install -r requirements.txt

python -m pytest -q                # 65 unit tests, no API key needed
python -m eval.run_eval            # offline evaluation on the 17 test documents
python -m streamlit run app.py     # UI at http://localhost:8501
```

In the app sidebar, click **Load synthetic demo**. This loads all 17 documents and a seeded register: the LPA baseline, LP-A to LP-D side letter obligations, and two amendments that were already applied. You can then:

- **Obligation register:** view versions (superseded vs approved). The **Possible precedence questions** panel flags LP-A, whose side letter deadline differs from the LPA's for the same deliverable.
- **Documents & extraction:** run LLM extraction on any document (needs an API key, see below).
- **Amendment review:** compare the original clause and the amendment side by side, then approve, reject or escalate. A reviewer ID and a note are required.
- **Lifecycle simulator:** a separate sandbox database for demonstrating approve and reject without touching the main register.
- **Audit history:** every save, review, version change and deletion.

## LLM extraction (optional)

The demo, tests and offline evaluation run without a model. To extract from documents:

```bash
cp .env.example .env
# set OPENROUTER_API_KEY and OPENROUTER_MODEL (any model available to your OpenRouter account)
```

Restart Streamlit after editing `.env`. Document text is sent to OpenRouter only when you click **Extract**. Free-tier models have rate limits. The model identifier used for a live evaluation run is recorded at the top of `eval/results_llm.md`.

## Architecture

```
documents ──► LLM extraction (AI, probabilistic)  ─┐
          └─► regex fast path for explicit         ├─► deterministic gate (code)
              "X deleted and replaced with Y"     ─┘     1 quote grounded to an exact source span
                                                         2 schema and allowed values
                                                         3 every number + unit literally in the quote
                                                         4 investor named in the quote
                                                         5 amendment type auto-applicable?
                                                              │
               blocked / needs_review / pending_review ◄──────┘
                                                              │
     matcher (investor + clause + old value) + LPA-vs-side-letter precedence flags (code)
                                                              │
                         HUMAN reviewer: approve / reject / escalate (ID + note)
                                                              │
     one SQL transaction: new version, old version superseded, audit events (code)
                                                              │
     due dates: calendar / business days + holiday calendar (code)
```

| Part | Must be exact? | Where it lives |
|---|---|---|
| Finding clauses, mapping to fields, in/out of scope | No (AI) | `src/extraction.py` prompts |
| Quote is a real span of the document | Yes | `src/quote_grounding.py` (whitespace and quote/dash normalisation only, no fuzzy matching; the raw span is stored) |
| Extracted numbers are written in the quote | Yes | `src/consistency.py` (digits or words, e.g. "fifteen Business Days") |
| Amendment matching, double-apply and stale-match protection | Yes | `src/amendments.py`, `src/store.py` |
| Due dates | Yes | `src/rules.py` (holiday calendar supplied by caller) |
| Precedence between LPA and side letter | Human | flagged by `src/conflicts.py`, never decided |
| Meaning, legal effect, approval | Human | review screens; recorded in `audit_events` |

## Test examples (17 synthetic documents)

All documents are in `data/synthetic_docs/`. Gold labels, written by hand from the documents, are in `eval/gold.json`.

| ID | Document | What it tests |
|---|---|---|
| C01 | `00_FUND_IV_LPA.txt` | Fund-wide baseline: quarterly statements, 60 calendar days; defines "Business Day" (London) |
| C02 | `01_LP_A_SIDE_LETTER.txt` | Two deliverables in one sentence, 45 calendar days |
| C03 | `02_LP_B_SIDE_LETTER.txt` | ESG report with a soft condition ("where reasonably available") |
| C04 | `03_LP_C_SIDE_LETTER.txt` | "As soon as practicable": no numeric deadline, the system must not invent one |
| C05 | `04_LP_D_SIDE_LETTER.txt` | 10 business days, across the year-end bank holiday |
| C06 | `05_LP_E_SIDE_LETTER.txt` | Event notification: out of scope, must be excluded |
| C07 | `09_LP_F_SIDE_LETTER.txt` | Deadline in words only ("fifteen Business Days"), crosses Easter |
| C08 | `10_LP_G_SIDE_LETTER.txt` | One clause, two obligations (quarterly 45 days, annual 120 days) |
| C09 | `11_LP_H_SIDE_LETTER.txt` | MFN and fee distractors; deadline runs from an event, not a period end |
| C10 | `12_LP_I_SIDE_LETTER.txt` | "Notwithstanding Section 7.1": side letter overrides the LPA (90 vs 60 days) |
| A01 | `06_LP_A_AMENDMENT_01.txt` | "deleted and replaced with" 45 to 30; already applied in the demo register |
| A02 | `07_LP_B_AMENDMENT_01.txt` | "changed from ... to" 60 to 75; already applied |
| A03 | `08_LP_A_AMENDMENT_02.txt` | Same pattern, but hard line breaks inside the sentence (PDF-style) |
| A04 | `13_LP_D_AMENDMENT_01.txt` | Unit change: 10 business days to 15 calendar days |
| A05 | `14_LP_B_AMENDMENT_02.txt` | "Extended by a further 15 days": new value not written, must not be auto-applied |
| A06 | `15_LP_A_AMENDMENT_03.txt` | Frequency change (not a deadline): manual handling only |
| A07 | `16_LP_J_AMENDMENT_01.txt` | US date format; investor with no record in the register |

## Results

### Offline (deterministic layer): 102 / 106 checks pass

`python -m eval.run_eval` feeds each gold record through the same post-processing the app uses, plus deliberately broken versions of it (invented deadline, paraphrased quote, re-wrapped quote, calendar/business unit swap, wrong investor, tampered amendment value). All 53 perturbations are handled correctly: 43 injected errors are caught, and 10 harmlessly re-wrapped quotes are restored to the exact source span. Full table: [`eval/results_offline.md`](eval/results_offline.md).

**Where it fails (reported, not tuned away):**

| Case | Failure | Explanation |
|---|---|---|
| C05 | Due date 2027-01-14, expected 2027-01-15 | Default business-day count skips weekends only and misses 1 Jan. Correct when the LPA holiday calendar is supplied (also checked). |
| C07 | Due date 2026-04-21, expected 2026-04-23 | Same cause: Good Friday and Easter Monday. Correct with the calendar. |
| C07 | Evidence helper cannot locate "fifteen Business Days" | The evidence lookup expects digits. The consistency check handles words, so the safety gate still works. |
| A03 | Regex fast path declines the line-broken amendment | It falls back to the LLM. The LLM tends to flatten line breaks in its quote, and strict grounding maps it back to the raw span. |

Known limitations that are not scored as failures: LP-G's sub-paragraph obligations are escalated because the investor is named only in the clause lead-in (an accepted false positive). The precedence flag matches deliverables exactly, so paraphrased deliverables are missed.

### Live LLM

```bash
python -m eval.run_eval --llm      # needs .env; writes eval/results_llm.md and .json (includes raw model outputs)
```

This scores reporting-obligation count, investor, frequency, deadline value and unit, clause, quote grounding and escalation for each side-letter case, and old/new value, effective date, blocking and matching for each amendment. Set `EVAL_SLEEP_SECONDS` to slow down for rate limits. Free-tier results vary between runs; treat one run as a sample, not an accuracy claim.

## Limitations

- **Recall is the weak spot.** The gate checks what the model found, not what it missed. A skipped clause produces no warning.
- Only deadline replacements can be applied automatically. Other amendment types are kept as `needs_review` for manual handling.
- Business-day dates are wrong unless a holiday calendar is supplied. Cut-off times and time zones are not modelled.
- No OCR, tables or cross-referenced schedules.
- No authentication or role-based access. SQLite is not encrypted. The audit log is append-only through the app but is not tamper-proof.
- Free OpenRouter models must only be used with synthetic data. Real side letters are confidential.

## Repository layout

```
app.py                     Streamlit UI
src/extraction.py          LLM prompts and post-processing (grounding, validation, consistency)
src/quote_grounding.py     strict quote-to-source-span grounding
src/consistency.py         numbers/units/investor must be readable from the quote
src/amendments.py          regex fast path, role detection, amendment matching
src/conflicts.py           LPA vs side letter precedence flags
src/rules.py               deadline calculator (calendar / business days + holidays)
src/store.py               SQLite store, versioning transaction, audit events
src/evidence.py            field-level evidence for reviewers
src/amendment_lab.py       isolated lifecycle simulator
data/synthetic_docs/       17 synthetic documents
data/demo_manifest.json    seeded demo register
eval/gold.json             hand-written gold labels
eval/run_eval.py           offline and live evaluation harness
tests/                     unit tests
docs/                      written brief (PDF)
```
