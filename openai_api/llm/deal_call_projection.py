"""Deal-only transcript projection for the Full Analysis prompt."""

from __future__ import annotations


def project_transcript_for_deal_prompt(transcript_text: str, *, deal_id: str) -> str:
    """Keep all transcript evidence while removing local filesystem paths."""
    del deal_id
    return "\n".join(
        line
        for line in transcript_text.splitlines()
        if not line.startswith(("- Transcript JSON:", "- Transcript MD:"))
    ).strip()
