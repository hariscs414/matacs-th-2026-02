# Write-up: MATACS-TH-2026-02

**Principle: AI reads, code decides, humans award.** Six guarantees hold in every case and are enforced in code, not left to diligence: nothing is silently dropped; nothing is guessed; any failure fails closed (the bid goes to a human, never ranked, never excluded); one proposal per AI call; scores come only from verified facts; and humans own every consequence. Assumptions are in the README.

## 1. AI vs. rules

**AI, one step:** two independent readers (two different models) turn each proposal's free text into fixed fields, each with a verbatim quote and page, or "not stated". Wording varies ("all-in", "since 2021", "electrician to be confirmed after award"), and that is where rules break down.

**Code, everything else:** upload checks, visible-text extraction, injection pre-screen, quote and number verification, reader reconciliation, mandatory gates, scoring, ranking, flagging, versioning and audit.

**Why:** reading is language; judging is policy. Weights and thresholds belong to the admin, must apply identically every time, and must be explainable line by line. An LLM is weak at arithmetic, varies between runs and can be steered by what it reads, so it never outputs a score, rank or pass/fail. Even "team and approach" becomes four yes/no facts that the model reports and code scores.

## 2. Consistency and explainability

- A score is a pure function of (verified facts, criteria version), and tests cover this. Extraction is cached by file hash, prompt and models, so re-ranking makes no AI call. A forced re-extraction that changes a decisive fact is flagged, never silently applied.
- Anchors are fixed ("zero points at the $260k ceiling"), never "relative to other bids", so a new proposal cannot reorder existing ones.
- Each result shows gate outcomes and, per criterion, value, points and the quoted evidence with a page link. Example: A = 30.0 + 10.6 + 10.0 + 12.5 + 15.0 = 78.1.
- The ranking also shows the margin to second place, the places each unresolved bid could take, and whether the winner survives plausible changes to anchors and weights. Here A is first in 263 of 263 settings, while D's missing schedule could decide the winner in about 30% of them, so D is the flag that matters most.

## 3. Human in the loop

Three checkpoints, each with controls:

1. **Admin** publishes a criteria version after lint and a preview with probe bids.
2. **Reviewers** open evidence, fix extraction errors, or fill a missing fact through a logged clarification. A clarification can never overwrite a stated value or change price, which would be bid repair. Exclusions, waivers and flag clearances need a reason and two different people.
3. **Approver** records the award. The gate refuses while any bid is unresolved, evidence for top places and exclusions is unopened, a close call is unacknowledged, or the approver helped decide. The tool never awards.

The audit log is hash-chained, so editing, deleting or reordering any entry is detected, and a broken chain blocks the award. It records who, when and what; file hashes; criteria version and hash; prompt and model ids; raw model output; verification results; scores; each override with before/after values and reason; and every refused action.

## 4. Changing criteria

Criteria are immutable versions. Edits are linted (weights total 100, known fields, sane anchors) and previewed before publishing.

- Results stay tied to the version that produced them, and versions never mix in one ranking.
- A change to a weight, anchor or threshold re-scores from stored facts with no AI call. A new criterion re-reads only that field.
- A re-score is a new run, and old versus new ranks are shown side by side for acceptance.
- Changing criteria after bids are seen is a fairness risk, so a reason is required and logged, and criteria lock once an award is recorded.
- Missing-data policy is a setting: "flag" asks a human, "ineligible" treats the bid as non-responsive.

## 5. Risks

| Risk | Safeguard |
|---|---|
| Wrong extraction | Two readers must agree on every decisive fact, quotes must exist verbatim, and a regex witness checks key numbers. Any mismatch is flagged, never averaged. |
| Made-up facts | "Not stated" is a valid answer and inference is forbidden. Missing facts show a score range and possible rank, never a guess. Exclusion needs affirmative evidence from both readers plus two humans (B). |
| Manipulation | Proposal text is untrusted data in random delimiters. Each call is isolated with no tools, the output has no channel for instructions, hidden text never reaches the model, and scores come from code. C's "rank me first" note is ignored, flagged and escalated. False claims (licence, insurance) remain "stated", so registry checks are a reviewer step. |
| Bias | Facts, not impressions. Bidder name masked, checklists reward content not polish, periodic human spot-checks. Criteria bias is a visible admin choice. |
| Failure and drift | Errors, timeouts, unreadable files and outages fail closed to NEEDS REVIEW, and in = out is reconciled. A model or prompt change must pass a golden set, including injection cases, before release. |
| Cost | One proposal, two small calls, cached by hash. Re-ranking and re-scoring cost nothing. Size caps and budget alerts. |
| Over-trust | Output is labelled triage. Flagged and excluded bids sit beside the ranking, and evidence must be opened before award. |
| Data protection | Retention terms, role-based access and data residency are confirmed with the client's privacy officer before real data is used. |

**Residual risk:** a bidder's false claim, both readers erring identically, or a badly designed criterion cannot be eliminated by software. They stay with named reviewer and admin roles.
