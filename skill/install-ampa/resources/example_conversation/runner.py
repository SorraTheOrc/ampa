#!/usr/bin/env python3
"""Simple example conversation engine: two managers exchange messages.

Produces a newline-delimited JSON transcript with timestamps and session ids.
"""

import argparse
import json
import os
import subprocess
import uuid
from datetime import datetime

try:
    import opencode_ai
    from opencode_ai import Opencode
except ModuleNotFoundError as exc:
    raise SystemExit(
        "Missing dependency: opencode_ai. Install with 'pip install --pre opencode-ai'"
    ) from exc


def now_iso() -> str:
    return datetime.utcnow().isoformat() + "Z"


def _extract_text(parts) -> str:
    texts = []
    for part in parts or []:
        try:
            if getattr(part, "type", None) == "text":
                text = getattr(part, "text", None)
                if text:
                    texts.append(text)
        except Exception:
            continue
    return "\n".join(texts).strip()


def _next_work_item_id() -> str:
    try:
        result = subprocess.run(
            ["wl", "next", "--json"],
            check=True,
            capture_output=True,
            text=True,
        )
    except FileNotFoundError as exc:
        raise SystemExit("Missing dependency: wl (Worklog CLI)") from exc
    except subprocess.CalledProcessError as exc:
        raise SystemExit(f"Failed to run wl next: {exc.stderr.strip()}") from exc

    try:
        payload = json.loads(result.stdout)
        work_item = payload.get("workItem") or {}
        work_id = work_item.get("id")
    except Exception as exc:
        raise SystemExit("Failed to parse wl next output") from exc

    if not work_id:
        raise SystemExit("wl next did not return a work item id")
    return work_id


def _display_base_url(base_url: str | None) -> str:
    if not base_url:
        return "unknown"
    return base_url


class ConversationManager:
    def __init__(
        self,
        name: str,
        seed: str,
        client: Opencode,
        provider_id: str,
        model_id: str,
        base_url: str | None,
        verbose: bool,
    ):
        self.name = name
        if verbose:
            print(
                f"[{self.name}] creating session provider={provider_id} "
                f"model={model_id} base_url={_display_base_url(base_url)}"
            )
        self.session = client.session.create(extra_body={})
        self.session_id = self.session.id
        self.last_message = seed
        self.client = client
        self.provider_id = provider_id
        self.model_id = model_id
        self.verbose = verbose

    def respond(self, incoming: str, turn: int) -> str:
        prompt = f"{incoming}\n\nRespond briefly as {self.name}."
        try:
            self.client.session.chat(
                self.session_id,
                provider_id=self.provider_id,
                model_id=self.model_id,
                parts=[{"type": "text", "text": prompt}],
            )
            messages = self.client.session.messages(self.session_id)
        except opencode_ai.APIError as exc:
            raise SystemExit(f"OpenCode API error during chat: {exc}") from exc

        last = messages[-1] if messages else None
        reply = _extract_text(getattr(last, "parts", None)) if last is not None else ""
        if not reply:
            reply = "(no assistant reply)"
        if self.verbose and self.name != "AMPA":
            print(f"[{self.name}] message: {reply}")
        return reply


def _summarize_transcript(
    client: Opencode,
    transcript_path: str,
    provider_id: str,
    model_id: str,
    verbose: bool,
) -> None:
    try:
        with open(transcript_path, "r", encoding="utf-8") as f:
            log_text = f.read().strip()
    except Exception as exc:
        raise SystemExit(f"Failed to read transcript for summary: {exc}") from exc

    session = client.session.create(extra_body={})
    session_id = session.id
    prompt = (
        "Summarize the conversation and actions in the following conversation-log:\n\n"
        f"{log_text}"
    )
    try:
        client.session.chat(
            session_id,
            provider_id=provider_id,
            model_id=model_id,
            parts=[{"type": "text", "text": prompt}],
        )
        messages = client.session.messages(session_id)
    except opencode_ai.APIError as exc:
        raise SystemExit(f"OpenCode API error during summary chat: {exc}") from exc

    last = messages[-1] if messages else None
    summary = _extract_text(getattr(last, "parts", None)) if last is not None else ""
    if not summary:
        summary = "(no summary reply)"
    if verbose:
        print(f"[summary] message: {summary}")
    print(summary)


def run(
    rounds: int,
    out_path: str,
    seed_ampa: str,
    seed_build: str,
    provider_ampa: str,
    model_ampa: str,
    provider_build: str,
    model_build: str,
    base_url: str | None,
    verbose: bool,
):
    client = Opencode(base_url=base_url) if base_url else Opencode()
    try:
        a = ConversationManager(
            "AMPA",
            seed_ampa,
            client,
            provider_ampa,
            model_ampa,
            base_url,
            verbose,
        )
        b = ConversationManager(
            "BUILD",
            seed_build,
            client,
            provider_build,
            model_build,
            base_url,
            verbose,
        )
    except opencode_ai.APIConnectionError as exc:
        hint = "Set OPENCODE_BASE_URL to your running OpenCode API (e.g. http://localhost:8083)."
        raise SystemExit(f"Connection error. {hint}") from exc
    except opencode_ai.APIStatusError as exc:
        hint = "The API rejected session creation. Ensure your OpenCode server supports POST /session."
        raise SystemExit(f"Request error. {hint}") from exc

    transcript = []

    # initial message from AMPA using its seed
    current_sender = a
    other = b
    message = a.last_message

    for turn in range(1, rounds * 2 + 1):
        ts = now_iso()
        entry = {
            "timestamp": ts,
            "turn": turn,
            "sender": current_sender.name,
            "session_id": current_sender.session_id,
            "provider_id": current_sender.provider_id,
            "model_id": current_sender.model_id,
            "message": message,
        }
        transcript.append(entry)
        if verbose and current_sender.name == "AMPA":
            print(f"[AMPA] sent message: {message}")

        # prepare response from other
        response = other.respond(message, turn)

        # rotate
        message = response
        current_sender, other = other, current_sender

    # write newline-delimited JSON
    with open(out_path, "w", encoding="utf-8") as f:
        for e in transcript:
            f.write(json.dumps(e, ensure_ascii=False) + "\n")

    print(f"Wrote {len(transcript)} messages to {out_path}")
    _summarize_transcript(client, out_path, provider_ampa, model_ampa, verbose)


def main():
    p = argparse.ArgumentParser()
    p.add_argument("--rounds", type=int, default=10)
    p.add_argument("--out", default="transcript.jsonl")
    p.add_argument("--seed-ampa", default=None)
    p.add_argument("--seed-build", default="Hello from BUILD")
    p.add_argument(
        "--provider-ampa",
        default=os.getenv("OPENCODE_PROVIDER_ID_AMPA", "LLama"),
    )
    p.add_argument(
        "--provider-build",
        default=os.getenv("OPENCODE_PROVIDER_ID_BUILD", "Github Copilot"),
    )
    p.add_argument(
        "--model-ampa",
        default=os.getenv("OPENCODE_MODEL_ID_AMPA", "Qwen 3 Next (local)"),
    )
    p.add_argument(
        "--model-build",
        default=os.getenv("OPENCODE_MODEL_ID_BUILD", "GPT-5-mini"),
    )
    p.add_argument(
        "--base-url", default=os.getenv("OPENCODE_BASE_URL", "http://localhost:9999")
    )
    p.add_argument("--verbose", action="store_true")
    args = p.parse_args()

    seed_ampa = args.seed_ampa
    if not seed_ampa:
        seed_ampa = f"audit {_next_work_item_id()}"

    run(
        args.rounds,
        args.out,
        seed_ampa,
        args.seed_build,
        args.provider_ampa,
        args.model_ampa,
        args.provider_build,
        args.model_build,
        args.base_url,
        args.verbose,
    )


if __name__ == "__main__":
    main()
