# Prototype (optional extra credit)

Small, dependency-light scripts that mirror the design: **AI reads** (`extract.py`), **code decides** (`score.py`), **humans own consequences** (`workflow.py`).

| File | Needs network / key? | Purpose |
|---|---|---|
| `score.py` | No | Criteria lint and preview, gates, scoring, ranking, rank envelopes, close-call check, robustness analysis. Deterministic. |
| `workflow.py` | No | Proposal states, four-eyes controls, clarification rules, finalisation gate, in = out reconciliation, hash-chained audit log. Includes an end-to-end demo. |
| `extract.py` | Yes (`ANTHROPIC_API_KEY`, two models) | Sends the prompt from `../PROMPTS.md` to two readers, forces a JSON-schema tool call, verifies quotes, runs a regex witness, reconciles the readers. Fails closed. |
| `criteria.json` | No | The admin's configuration: weights, anchors, mandatory rules, missing-data policy, governance thresholds, field definitions. Criteria are data, never code. |
| `extractions/A..E.json` | No | Hand-prepared fixtures: the **facts** the extraction step would return. No proposal text, no quotes. |
| `test_score.py`, `test_workflow.py`, `test_extract.py` | No | 51 tests for the properties claimed in `WRITEUP.md`. `test_extract.py` runs offline on synthetic text. |
| `output/` | | Generated: `ranking.json`, `breakdown.json`, `demo_transcript.txt`, `demo_audit_log.jsonl`. |

## Run it

```bash
cd prototype
python score.py                      # rank the fixtures; writes output/ranking.json + breakdown.json
python score.py --stress             # does the winner survive 263 alternative anchor and weight settings?
python score.py --lint               # lint the criteria and preview what each anchor means
python workflow.py                   # end-to-end demo: refusals, reviewer actions, award, tamper check
python -m unittest -v                # 51 tests
```

`workflow.py` shows the controls doing their job on the sample set: the approver is refused while bids are unresolved; a lone reviewer cannot confirm an exclusion; a "clarification" that tries to change a price is refused; a clarified schedule re-scores D with no AI call (15 weeks puts D third); the approver is refused until evidence for the top places is opened; and editing one audit entry by hand breaks the hash chain. The full transcript is in `output/demo_transcript.txt`.

## Running the AI step on your own copy of the brief's text

The brief's sample text is kept out of this repository on purpose.

```bash
pip install -r requirements.txt
cp .env.example .env                 # add your key and two different models: MODEL and MODEL_B
mkdir samples_local                  # git-ignored; put A.txt ... E.txt here
python extract.py --dry-run A        # prints the exact prompt; no key needed
python extract.py A B C D E          # writes extractions_local/ (git-ignored)
python score.py --extractions extractions_local --uploaded A,B,C,D,E
```

`--uploaded` makes the scorer flag any uploaded id that has no extraction file (for example because a reader failed) instead of silently leaving it out.

## Notes

* `extract.py` forces a tool call (`tool_choice`), which the API does not allow together with extended thinking, and some models do not support it. Choose models that support forced tool use.
* Two readers are required. `--single-reader` exists for development only and leaves decisive facts unchecked.
* The live API path was written against the documented Messages API but has **not** been run end to end (no key in the authoring environment). Prompt assembly, pre-screen, schema builder, grounding, witness and reconciliation are tested offline.
* `score.py` and `workflow.py` are fully runnable and tested.
