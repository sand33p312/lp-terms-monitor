# Changelog

## v0.5 (submission build)

- Added value-to-quote consistency checks (`src/consistency.py`): extracted deadline numbers and units, amendment old/new values and the investor must be literally readable from the grounded quote (digits or number words). Obligations that fail are saved only as `needs_review`; amendments that fail cannot be auto-applied.
- Amendment candidates with validation errors (computed values, non-deadline changes) can now be saved for manual handling as `needs_review` instead of being lost; approval still refuses them.
- Added LPA vs side letter precedence flags (`src/conflicts.py`) and a register panel. The app flags; a human decides.
- Business-day calculation accepts a holiday calendar.
- Added 8 synthetic test documents (17 total), a "Business Day" definition in the synthetic LPA, and an LPA baseline record in the demo.
- Added the evaluation harness (`eval/`): hand-written gold labels, offline perturbation tests, and a live `--llm` mode.
- Refactored post-LLM checks into `postprocess_obligation_item` / `postprocess_amendment_item` so the app and the eval share one code path.
- 65 unit tests.

## v0.4.1: Quote grounding and validation safety

- Added strict source-quote grounding that maps model-generated, whitespace-normalized quotations back to exact raw document spans.
- Added Unicode NFKC and common quote/dash typography normalization strictly for locating source spans; stored evidence always comes from original source text.
- Applied grounding to both reporting obligation extraction and amendment extraction.
- Added `source_quote_grounding` provenance metadata.
- Disabled candidate-save actions while validation errors remain.
- Added explicit UI messaging when normalization was required and when saving is blocked.
- Added tests for line-break flattening, typography normalization, semantic paraphrase rejection and LLM amendment extraction.
- No database schema migration required.

## v0.4: Integrated amendment management

- Combined the main obligation monitor with amendment review, version control and audit history.
- Added integrated amendment review and a separate lifecycle simulator database.
- Added field-level evidence lookup and main application audit events.
