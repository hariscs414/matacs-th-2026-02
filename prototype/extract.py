#!/usr/bin/env python3
"""The only AI step: turn one proposal's text into verified, evidence-backed facts.

Two independent readers (two different models), one proposal per call, no tools, no shared context.
A rule-based regex witness cross-checks key numbers. Any disagreement, failed quote, error or
timeout becomes a flag for a human: the pipeline fails closed.

  python extract.py --dry-run A     # print the exact system + user prompt (no API call, no key needed)
  python extract.py A B C D E       # call both readers, verify, reconcile, write extractions_local/<id>.json
  python extract.py --prescreen A   # run only the rule-based injection pre-screen

Proposal text is NOT stored in this repository. Put your own text files in
prototype/samples_local/ (git-ignored), one per proposal, named A.txt ... E.txt.
Optional page markers: [[page 1]], [[page 2]]. Without them everything is page 1.

Needs ANTHROPIC_API_KEY and two models (MODEL, MODEL_B) in .env. Results go to extractions_local/
(git-ignored) because they contain verbatim quotes. Feed them to the scorer with:
  python score.py --extractions extractions_local
"""
from __future__ import annotations

import argparse
import json
import os
import re
import secrets
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
PROMPTS_MD = HERE.parent / "PROMPTS.md"
CRITERIA = HERE / "criteria.json"
SAMPLES = HERE / "samples_local"
OUT = HERE / "extractions_local"

PROMPT_VERSION = "extract-v1"
TOOL_NAME = "submit_extraction"
PAGE_RE = re.compile(r"\[\[page (\d+)\]\]")
BLOCK_RE = re.compile(r"<!-- prompt:(\w+):start -->\s*```[a-z]*\n(.*?)\n```\s*<!-- prompt:\1:end -->", re.S)


# ------------------------------------------------------------------ prompt assembly
def load_prompts() -> dict[str, str]:
    blocks = {m.group(1): m.group(2) for m in BLOCK_RE.finditer(PROMPTS_MD.read_text(encoding="utf-8"))}
    missing = {"system", "user"} - set(blocks)
    if missing:
        raise SystemExit(f"PROMPTS.md is missing marked blocks: {sorted(missing)}")
    return blocks


def render_fields(fields: list[dict]) -> str:
    lines = []
    for i, f in enumerate(fields, 1):
        lines.append(f"{i}. {f['id']} ({f['type']})")
        lines.append(f"   {f['definition']}")
        lines.append("")
    return "\n".join(lines).rstrip()


def neutralise(text: str) -> str:
    """A proposal must not be able to forge a delimiter or a page marker."""
    return text.replace("<<<", "< < <").replace("[[page", "[ [page")


def split_pages(raw: str) -> list[str]:
    """Convenience for LOCAL .txt files only. In production each PDF page arrives as its own
    string from the renderer, so proposal content can never create or split a page."""
    pages = [p.strip() for p in PAGE_RE.split(raw)]
    return [p for p in pages if p] or [""]


def assemble_pages(pages: list[str]) -> str:
    """Page markers are inserted by code, after look-alikes inside the content are neutralised."""
    return "\n".join(f"[[page {i}]]\n{neutralise(p)}" for i, p in enumerate(pages, 1))


def build_user_message(template: str, criteria: dict, proposal_id: str, text: str, nonce: str) -> str:
    safe = text
    out = template
    for key, val in {
        "category_name": criteria["category"],
        "field_definitions": render_fields(criteria["fields"]),
        "proposal_id": proposal_id,
        "nonce": nonce,
        "proposal_text": safe,
    }.items():
        out = out.replace("{{" + key + "}}", val)
    return out


def build_tool(fields: list[dict]) -> dict:
    scalar = {"boolean": ["boolean", "null"], "integer": ["integer", "null"], "number": ["number", "null"]}
    evidence = {"type": "array", "maxItems": 3, "items": {
        "type": "object",
        "properties": {"quote": {"type": "string"}, "page": {"type": "integer"}},
        "required": ["quote", "page"]}}

    def one(f):
        return {"type": "object", "properties": {
            "value": {"type": scalar[f["type"]]},
            "status": {"type": "string", "enum": ["found", "not_stated", "ambiguous"]},
            "evidence": evidence,
            "note": {"type": "string"}},
            "required": ["value", "status", "evidence"]}

    return {
        "name": TOOL_NAME,
        "description": "Submit the extracted fields and any integrity flags for this proposal.",
        "input_schema": {"type": "object", "properties": {
            "fields": {"type": "object", "properties": {f["id"]: one(f) for f in fields},
                       "required": [f["id"] for f in fields]},
            "integrity_flags": {"type": "array", "items": {"type": "object", "properties": {
                "type": {"type": "string",
                         "enum": ["instruction_to_automated_system", "claim_of_pre_approval", "other"]},
                "quote": {"type": "string"}, "page": {"type": "integer"}},
                "required": ["type", "quote", "page"]}}},
            "required": ["fields", "integrity_flags"]},
    }


# ------------------------------------------------------------------ rule-based pre-screen
PATTERNS = [
    (r"\b(ignore|disregard|override|forget)\b.{0,40}\b(previous|prior|above|earlier|your)\b.{0,25}\b(instructions?|prompts?|rules?)\b",
     "override_instruction"),
    (r"\b(note|message|instruction|attention)s?\s+(to|for)\s+(the\s+)?(automated|ai|llm|language model|assistant|evaluator|review)",
     "addressed_to_automated_reviewer"),
    (r"\b(automated|ai|llm)\s+(review|evaluat|scor|screen|triage)\w*\s*(systems?|tools?|models?)?\b",
     "mentions_automated_reviewer"),
    (r"\b(rank|score|rate|grade|mark)\b.{0,40}\b(first|top|highest|maximum|full marks|perfect)\b",
     "ranking_demand"),
    (r"\b(pre-?approved|already approved|endorsed by (the )?(client|procurement))\b", "claim_of_pre_approval"),
    (r"[\u200b-\u200f\u202a-\u202e\u2060\ufeff]", "invisible_characters"),
]


def prescreen(text: str) -> list[dict]:
    """Flag only. Never alters the text and never touches a score."""
    flags = []
    page = 1
    for line in text.splitlines():
        m = PAGE_RE.search(line)
        if m:
            page = int(m.group(1))
        kinds = [kind for pattern, kind in PATTERNS if re.search(pattern, line, flags=re.I)]
        if kinds:  # one flag per line, listing every pattern that matched
            flags.append({"type": kinds[0], "also": kinds[1:], "page": page, "source": "prescreen",
                          "quote": line.strip()[:200]})
    return flags


# ------------------------------------------------------------------ verification
def norm(s: str) -> str:
    return re.sub(r"\s+", " ", s).strip()


def numbers_in(s: str) -> set[float]:
    vals = set()
    for m in re.finditer(r"(\d[\d,]*(?:\.\d+)?)\s*(m\b|million|k\b|thousand)?", s, flags=re.I):
        n = float(m.group(1).replace(",", ""))
        unit = (m.group(2) or "").lower()
        n *= 1_000_000 if unit in ("m", "million") else 1_000 if unit in ("k", "thousand") else 1
        vals.add(n)
    return vals


def verify(result: dict, text: str, fields: list[dict]) -> tuple[dict, list[str]]:
    """Enforce the contract in code: never trust the model's own claim of grounding."""
    source = norm(PAGE_RE.sub(" ", text))
    warnings, out = [], {}
    returned = result.get("fields", {})
    for f in fields:
        r = returned.get(f["id"])
        if not isinstance(r, dict):
            out[f["id"]] = {"value": None, "status": "not_stated", "evidence": [], "note": "absent from model output"}
            warnings.append(f"{f['id']}: absent from model output")
            continue
        r = dict(r)
        if r.get("status") == "found":
            ev = r.get("evidence") or []
            if r.get("value") is None or not ev:
                r.update(status="ungrounded", value=None)
                warnings.append(f"{f['id']}: found without value or evidence")
            elif any(norm(e.get("quote", "")) not in source for e in ev):
                r.update(status="ungrounded", value=None)
                warnings.append(f"{f['id']}: quote not found verbatim in source")
            elif f["type"] in ("integer", "number"):
                nums = set().union(*(numbers_in(e["quote"]) for e in ev))
                if float(r["value"]) not in nums:
                    warnings.append(f"{f['id']}: value {r['value']} not literally in quote (check)")
        else:
            r["value"] = None  # enforce: no value without status found
        out[f["id"]] = r
    return out, warnings


# ------------------------------------------------------------------ witness and reconciliation
DOLLAR_RE = re.compile(r"\$\s?(\d[\d,]*(?:\.\d+)?)\s*(m\b|million|k\b|thousand)?", re.I)
WEEKS_RE = re.compile(r"(\d+(?:\.\d+)?)\s*[- ]?\s*weeks?\b", re.I)
YEARS_RE = re.compile(r"(\d+(?:\.\d+)?)\s*[- ]?\s*years?\b", re.I)
WITNESS_FIELDS = {"total_price_cad": "dollars", "liability_insurance_cad": "dollars",
                  "completion_weeks": "weeks", "panel_warranty_years": "years",
                  "workmanship_warranty_years": "years"}


def witness(field_id: str, value, text: str) -> str:
    """Regex-only second opinion on a numeric fact: confirmed | contradicted | inconclusive.
    'contradicted' means the number appears nowhere in the text as a literal."""
    kind = WITNESS_FIELDS.get(field_id)
    if kind is None or value is None:
        return "inconclusive"
    body = PAGE_RE.sub(" ", text)
    cands: set[float] = set()
    if kind == "dollars":
        for m in DOLLAR_RE.finditer(body):
            n = float(m.group(1).replace(",", ""))
            unit = (m.group(2) or "").lower()
            cands.add(n * (1_000_000 if unit in ("m", "million") else 1_000 if unit in ("k", "thousand") else 1))
    else:
        rx = WEEKS_RE if kind == "weeks" else YEARS_RE
        cands = {float(m.group(1)) for m in rx.finditer(body)}
    if not cands:
        return "inconclusive"
    return "confirmed" if any(abs(c - float(value)) < 1e-6 for c in cands) else "contradicted"


def decisive_fields(criteria: dict) -> set[str]:
    """Every fact that can exclude a bid or move a numeric score. Checklist items are the rest."""
    ids = {r["field"] for m in criteria["mandatory"] for r in m["rules"]}
    for w in criteria["weighted"]:
        if w["type"] == "linear":
            ids.add(w["field"])
        elif w["type"] == "composite":
            ids.update(p["field"] for p in w["parts"])
    ids.update(f["id"] for f in criteria["fields"] if f["id"].startswith("installs_period"))
    return ids


def _same(a, b) -> bool:
    if isinstance(a, (int, float)) and isinstance(b, (int, float)) and not isinstance(a, bool):
        return abs(a - b) < 1e-9
    return a == b


def reconcile_readers(a: dict, b: dict, decisive: set[str]) -> dict:
    """Two readers must agree. On a decisive fact any disagreement becomes 'disputed' (a human decides).
    On a checklist item a disagreement earns no credit. Never average, never pick one."""
    out = {}
    for fid in sorted(a.keys() | b.keys()):
        ra = a.get(fid) or {"value": None, "status": "absent", "evidence": []}
        rb = b.get(fid) or {"value": None, "status": "absent", "evidence": []}
        if ra["status"] == rb["status"] and (ra["status"] != "found" or _same(ra["value"], rb["value"])):
            out[fid] = {**ra, "agreement": "both_readers"}
        elif fid in decisive:
            out[fid] = {"value": None, "status": "disputed", "evidence": [],
                        "note": f"readers disagree: A={ra['status']}/{ra['value']}, B={rb['status']}/{rb['value']}"}
        else:
            out[fid] = {"value": None, "status": "not_stated", "evidence": [],
                        "note": "readers disagreed on a checklist item; no credit given"}
    return out


def apply_witness(fields: dict, text: str, decisive: set[str]) -> list[str]:
    warnings = []
    for fid, r in fields.items():
        if fid in decisive and r["status"] == "found" and witness(fid, r["value"], text) == "contradicted":
            r.update(status="disputed", value=None,
                     note="value not found as a literal number in the text (regex witness)")
            warnings.append(f"{fid}: contradicted by regex witness")
    return warnings


# ------------------------------------------------------------------ model call
def call_model(system: str, user: str, tool: dict, model: str, temperature: float | None):
    import anthropic  # imported lazily so --dry-run needs no dependencies

    client = anthropic.Anthropic()
    kwargs = dict(model=model, max_tokens=4096, system=system, tools=[tool],
                  tool_choice={"type": "tool", "name": tool["name"]},  # forced tool call
                  messages=[{"role": "user", "content": user}])
    if temperature is not None:
        kwargs["temperature"] = temperature
    resp = client.messages.create(**kwargs)
    for block in resp.content:
        if block.type == "tool_use":
            return block.input, {"model": resp.model, "stop_reason": resp.stop_reason,
                                 "input_tokens": resp.usage.input_tokens,
                                 "output_tokens": resp.usage.output_tokens}
    raise RuntimeError("model returned no tool call")


def read_proposal(pid: str) -> str:
    path = SAMPLES / f"{pid}.txt"
    if not path.exists():
        raise SystemExit(f"{path} not found. Proposal text is kept out of the repo; add it locally.")
    return assemble_pages(split_pages(path.read_text(encoding="utf-8")))


def read_once(system, user, tool, model, temperature, text, fields):
    """One reader: call, then enforce the contract in code (never trust the model's own grounding)."""
    raw, meta = call_model(system, user, tool, model, temperature)
    verified, warnings = verify(raw, text, fields)
    return verified, raw.get("integrity_flags", []), {**meta, "warnings": warnings}


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("ids", nargs="+", help="proposal ids, e.g. A B C")
    ap.add_argument("--dry-run", action="store_true", help="print the prompt, do not call the API")
    ap.add_argument("--prescreen", action="store_true", help="run only the rule-based pre-screen")
    ap.add_argument("--single-reader", action="store_true",
                    help="development only: skip the second reader (decisive facts are then unchecked)")
    ap.add_argument("--temperature", type=float, default=0.0)
    args = ap.parse_args()

    try:
        from dotenv import load_dotenv
        load_dotenv(HERE / ".env")
    except ImportError:
        pass

    criteria = json.loads(CRITERIA.read_text(encoding="utf-8"))
    prompts = load_prompts()
    tool = build_tool(criteria["fields"])
    decisive = decisive_fields(criteria)
    model_a = os.environ.get("MODEL", "claude-sonnet-5-5")
    model_b = os.environ.get("MODEL_B")
    if not args.dry_run and not args.prescreen and not model_b and not args.single_reader:
        raise SystemExit("Two independent readers are required (criteria governance.readers_required). "
                         "Set MODEL_B in .env to a different model, or pass --single-reader for development.")
    OUT.mkdir(exist_ok=True)

    for pid in args.ids:
        text = read_proposal(pid)
        flags = prescreen(text)
        if args.prescreen:
            print(pid, json.dumps(flags, indent=2))
            continue
        user = build_user_message(prompts["user"], criteria, pid, text, secrets.token_hex(8))
        if args.dry_run:
            print("=== SYSTEM ===\n" + prompts["system"] + "\n\n=== USER ===\n" + user)
            continue

        try:
            fa, ia, ma = read_once(prompts["system"], user, tool, model_a, args.temperature, text, criteria["fields"])
            if model_b and not args.single_reader:
                user_b = build_user_message(prompts["user"], criteria, pid, text, secrets.token_hex(8))
                fb, ib, mb = read_once(prompts["system"], user_b, tool, model_b, args.temperature, text, criteria["fields"])
                fields, model_flags = reconcile_readers(fa, fb, decisive), ia + ib
                readers = [ma, mb]
            else:
                fields, model_flags, readers = fa, ia, [ma]
            warnings = apply_witness(fields, text, decisive)
            record = {"proposal_id": pid, "fields": fields,
                      "integrity_flags": flags + [{**f, "source": "model"} for f in model_flags],
                      "_meta": {"readers": readers, "prompt_version": PROMPT_VERSION,
                                "criteria_version": criteria["criteria_version"], "witness_warnings": warnings}}
        except Exception as exc:  # fail closed: a crash or timeout must never drop or silently rank a bid
            print(f"{pid}: reader failed ({type(exc).__name__}: {exc}); no extraction written, "
                  f"the scorer will flag it as a pipeline failure")
            continue
        (OUT / f"{pid}.json").write_text(json.dumps(record, indent=2), encoding="utf-8")
        print(f"{pid}: wrote extractions_local/{pid}.json  readers={len(record['_meta']['readers'])} "
              f"flags={len(record['integrity_flags'])} witness_warnings={len(warnings)}")


if __name__ == "__main__":
    sys.exit(main())
