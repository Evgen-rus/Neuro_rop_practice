"""Replay deterministic deal-playbook post-processing against a saved compact result.

This utility never calls OpenAI and never overwrites the supplied compact snapshot.
The explicit flags describe observed evidence; they are not keyed to case IDs.
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import date
from pathlib import Path
from typing import Any

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from openai_api.llm.attention_delta import materialize_deal_attention_delta, validate_deal_attention_delta
from openai_api.llm.deal_attention_playbooks import (
    DATED_TECHNICAL_INPUT_CONTROL,
    DISPUTED_CLOSED_DEAL_REVIEW,
    INVOICE_PRICE_COMPETITOR_RISK,
)


def _review_for_playbook(args: argparse.Namespace, existing: dict[str, Any]) -> dict[str, Any]:
    old_review = existing.get("deal_review") if isinstance(existing.get("deal_review"), dict) else {}
    review = {
        "type": old_review.get("type", "other"),
        "decision": old_review.get("decision", "manager_action_required"),
        "action_playbook": args.playbook,
        "closure_status": args.closure_status,
        "technical_input_status": "not_applicable",
        "required_technical_inputs": list(args.technical_input),
        "invoice_status": "not_applicable",
        "invoice_agreed": False,
        "payment_intent_confirmed": False,
        "advance_agreed": False,
        "contract_signed": False,
        "payment_date_confirmed": False,
        "customer_compares_options": args.customer_compares_options,
        "comparison_subject_known": args.comparison_subject_known,
        "price_or_terms_gap_known": args.price_or_terms_gap_known,
        "budget_not_disclosed_confirmed": args.budget_not_disclosed_confirmed,
        "competitor_confirmed": args.competitor_confirmed,
        "confirmed_refusal": args.confirmed_refusal,
        "budget_known": args.budget_known,
        "decision_maker_known": args.decision_maker_known,
        "decision_date_known": args.decision_date_known,
        "clarifying_contact_completed": args.clarifying_contact_completed,
        "next_step_confirmed": args.next_step_confirmed,
        "price_competitor_risk": args.price_competitor_risk,
    }
    if args.playbook == DATED_TECHNICAL_INPUT_CONTROL:
        if not review["required_technical_inputs"]:
            raise ValueError("dated_technical_input_control requires at least one --technical-input from evidence")
        review["technical_input_status"] = args.technical_input_status
    elif args.playbook == INVOICE_PRICE_COMPETITOR_RISK:
        if not args.invoice_sent:
            raise ValueError("invoice_price_competitor_risk requires --invoice-sent from evidence")
        review["invoice_status"] = "sent_unconfirmed"
    elif args.playbook == DISPUTED_CLOSED_DEAL_REVIEW:
        review["type"] = old_review.get("type", "closed_wrong_qualification")
    return review


def replay_saved_deal_delta(payload: dict[str, Any], args: argparse.Namespace) -> dict[str, Any]:
    delta = payload.get("attention_delta") if isinstance(payload.get("attention_delta"), dict) else payload
    if delta.get("entity_type") != "deal":
        raise ValueError("Replay supports compact deal results only")
    result = dict(delta)
    result["deal_review"] = _review_for_playbook(args, result)
    result["rop_action"] = dict(result.get("rop_action") or {})
    result = materialize_deal_attention_delta(result, today=date.fromisoformat(args.today) if args.today else None)
    validate_deal_attention_delta(result)
    return result


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--input", required=True, help="Saved attention_delta.json")
    parser.add_argument("--output", required=True, help="New replay JSON path")
    parser.add_argument("--playbook", required=True, choices=(DISPUTED_CLOSED_DEAL_REVIEW, DATED_TECHNICAL_INPUT_CONTROL, INVOICE_PRICE_COMPETITOR_RISK))
    parser.add_argument("--today", help="Optional deterministic internal control date, YYYY-MM-DD")
    parser.add_argument("--technical-input", action="append", default=[])
    parser.add_argument("--technical-input-status", choices=("inputs_missing", "client_date_confirmed", "internal_control_only"), default="inputs_missing")
    parser.add_argument("--closure-status", choices=("confirmed", "disputed", "unconfirmed"), default="disputed")
    parser.add_argument("--invoice-sent", action="store_true")
    parser.add_argument("--customer-compares-options", action="store_true")
    parser.add_argument("--comparison-subject-known", action="store_true")
    parser.add_argument("--price-or-terms-gap-known", action="store_true")
    parser.add_argument("--budget-not-disclosed-confirmed", action="store_true")
    parser.add_argument("--competitor-confirmed", action="store_true")
    parser.add_argument("--confirmed-refusal", action="store_true")
    parser.add_argument("--budget-known", action="store_true")
    parser.add_argument("--decision-maker-known", action="store_true")
    parser.add_argument("--decision-date-known", action="store_true")
    parser.add_argument("--clarifying-contact-completed", action="store_true")
    parser.add_argument("--next-step-confirmed", action="store_true")
    parser.add_argument("--price-competitor-risk", choices=("none", "suspected", "confirmed"), default="suspected")
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    source = Path(args.input)
    output = Path(args.output)
    payload = json.loads(source.read_text(encoding="utf-8"))
    replay = replay_saved_deal_delta(payload, args)
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(
        json.dumps(
            {
                "source_compact_result": str(source),
                "mode": "local_deterministic_postprocessing_replay_no_api",
                "attention_delta": replay,
            },
            ensure_ascii=False,
            indent=2,
        ),
        encoding="utf-8",
    )
    print(f"Saved no-API deterministic replay: {output}")


if __name__ == "__main__":
    main()
