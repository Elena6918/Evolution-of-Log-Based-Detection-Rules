#!/usr/bin/env python3
"""
run_llm.py
========================
Send LLM-ready prompt records through an OpenAI-compatible API and collect
structured JSON responses.

Input:   JSONL produced by prepare_llm_prompts.py
         Each line: {lineage_id, repo, rule_canonical,
                     version_a, version_b, commit_a, commit_b, prompt}

Output:  JSONL with the original metadata + LLM response fields:
         {…metadata…, model, llm_result, llm_error}

Usage:
    # Full corpus
    python run_llm.py \\
        --input  ../llm_data/sigma/prompts.jsonl \\
        --outfile ../llm_data/sigma/results.jsonl \\
        --model  gpt-4o-mini

    # Test set
    python run_llm.py \\
        --input  ../llm_data/test/prompts.jsonl \\
        --outfile ../llm_data/test/results.jsonl \\
        --model  gpt-4o

    # Dry run (print first N prompts, no API calls)
    python run_llm.py \\
        --input  ../llm_data/test/prompts.jsonl \\
        --dry-run --limit 2

    # Aggregate multiple results files into one JSONL
    python run_llm.py --aggregate ../llm_data/

    # Process only records with missing/errored results in a prior results file
    python run_llm.py \
        --input data_prep/llm_data/sigma/prompts.jsonl \
        --outfile data_prep/llm_data/sigma/results_retry_2026-04-13.jsonl \
        --only-errors-from data_prep/llm_data/sigma/results.jsonl \
        --model gpt-5 \
        --parallel 10

"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import os
import re
import sys
from datetime import datetime
from pathlib import Path

try:
    import aiohttp
    import aiofiles
except ImportError:
    aiohttp = None   # type: ignore[assignment]
    aiofiles = None  # type: ignore[assignment]

try:
    import tiktoken
except ImportError:
    tiktoken = None  # type: ignore[assignment]


# ── Logging ────────────────────────────────────────────────────────────────

def setup_logging(log_file: str = "run_llm.log",
                  level: int = logging.INFO) -> logging.Logger:
    logger = logging.getLogger("llm_diff")
    logger.setLevel(level)
    fmt = logging.Formatter("%(asctime)s [%(levelname)s] %(message)s")
    fh = logging.FileHandler(log_file, encoding="utf-8")
    fh.setLevel(level)
    fh.setFormatter(fmt)
    ch = logging.StreamHandler()
    ch.setLevel(level)
    ch.setFormatter(logging.Formatter("%(message)s"))
    if not logger.handlers:
        logger.addHandler(fh)
        logger.addHandler(ch)
    return logger


# ── Prompt sizing ──────────────────────────────────────────────────────────

def get_token_encoder(model: str):
    """Best-effort tokenizer for the requested model; return None if unavailable."""
    if tiktoken is None:
        return None
    try:
        return tiktoken.encoding_for_model(model)
    except Exception:
        return tiktoken.get_encoding("cl100k_base")


def estimate_prompt_tokens(prompt: str, encoder) -> tuple[int, str]:
    """
    Estimate prompt tokens.
    Uses tiktoken when available; otherwise falls back to a conservative char-based estimate.
    """
    if encoder is not None:
        return len(encoder.encode(prompt)), "tiktoken"
    # Conservative fallback when tokenizer support is unavailable.
    # Detection-rule prompts contain lots of punctuation, paths, escapes, and JSON,
    # which can tokenize much more densely than plain English prose.
    return max(1, (len(prompt) + 1) // 2), "char_fallback"


# ── LLM call ──────────────────────────────────────────────────────────────

async def call_llm(
    session,
    model: str,
    prompt: str,
    semaphore: asyncio.Semaphore,
    logger: logging.Logger,
    base_url: str = "https://api.openai.com/v1",
    max_retries: int = 3,
) -> tuple[str | None, str | None]:
    """Send one prompt; return (raw_response, error_message)."""
    url = f"{base_url.rstrip('/')}/chat/completions"
    headers = {
        "Authorization": f"Bearer {os.getenv('OPENAI_API_KEY', '')}",
        "Content-Type":  "application/json",
    }
    payload = {
        "model":           model,
        "messages":        [{"role": "user", "content": prompt}],
        # "temperature":     0, # default is 0, but some models may reject non-default values; see retry logic below
        "response_format": {"type": "json_object"},
    }
    retried_without_temperature = False
    last_error: str | None = None

    async with semaphore:
        for attempt in range(1, max_retries + 1):
            try:
                async with session.post(
                    url, headers=headers, json=payload, timeout=aiohttp.ClientTimeout(total=240)
                ) as resp:
                    if resp.status == 200:
                        data = await resp.json()
                        return data["choices"][0]["message"]["content"], None
                    text = await resp.text()
                    compact_text = " ".join(text.split())
                    last_error = f"http_{resp.status}: {compact_text[:500]}"
                    if resp.status in {429, 500, 502, 503}:
                        logger.warning(
                            f"Retryable HTTP {resp.status} (attempt {attempt}); sleeping..."
                        )
                        await asyncio.sleep(2 ** attempt)
                    else:
                        if (
                            resp.status == 400
                            and "temperature" in payload
                            and not retried_without_temperature
                            and "Unsupported value" in compact_text
                            and "temperature" in compact_text
                            and "Only the default (1) value is supported" in compact_text
                        ):
                            retried_without_temperature = True
                            payload.pop("temperature", None)
                            logger.info(
                                f"Model {model} rejected custom temperature; retrying without it."
                            )
                            continue
                        logger.error(f"Non-retryable HTTP {resp.status}: {text[:200]}")
                        return None, last_error
            except Exception as exc:
                last_error = f"{type(exc).__name__}: {exc}"
                logger.warning(f"Exception on attempt {attempt}: {exc}")
                await asyncio.sleep(2 ** attempt)
        logger.error(f"Max retries exceeded. Last error: {last_error}")
        return None, (last_error or "max_retries_exceeded")


# ── Parse LLM output ──────────────────────────────────────────────────────

def parse_llm_response(raw: str) -> tuple[dict | None, str | None]:
    """
    Strip markdown fences and parse JSON.
    Returns (parsed_dict, error_message).
    """
    cleaned = re.sub(r"^```(?:json)?\s*|\s*```$", "", raw.strip(), flags=re.DOTALL)
    try:
        return json.loads(cleaned), None
    except json.JSONDecodeError as exc:
        return None, f"JSON parse error: {exc} | raw[:200]={raw[:200]!r}"


# ── Process one record ────────────────────────────────────────────────────

async def process_record(
    session,
    model: str,
    record: dict,
    semaphore: asyncio.Semaphore,
    logger: logging.Logger,
    base_url: str,
    max_input_tokens: int | None,
    token_encoder,
) -> dict:
    """Run LLM for one prompt record; return the enriched output dict."""
    lid     = record.get("lineage_id", "?")
    va, vb  = record.get("version_a"), record.get("version_b")
    repo    = record.get("repo", "?")
    prompt  = record["prompt"]

    logger.debug(f"[{repo}] {lid} v{va}→v{vb} sending...")
    if max_input_tokens is not None:
        prompt_tokens, token_count_method = estimate_prompt_tokens(prompt, token_encoder)
        if prompt_tokens > max_input_tokens:
            out = {k: v for k, v in record.items() if k != "prompt"}
            out["model"] = model
            out["llm_result"] = None
            out["llm_error"] = (
                f"prompt_too_large: estimated_input_tokens={prompt_tokens} "
                f"exceeds_guard={max_input_tokens} ({token_count_method})"
            )
            logger.warning(
                f"[{repo}] {lid} v{va}→v{vb}: prompt too large "
                f"({prompt_tokens} > {max_input_tokens}, {token_count_method})"
            )
            return out

    raw, call_err = await call_llm(session, model, prompt, semaphore, logger, base_url)

    out = {k: v for k, v in record.items() if k != "prompt"}
    out["model"] = model

    if raw is None:
        out["llm_result"] = None
        out["llm_error"]  = call_err or "no_response"
        logger.warning(f"[{repo}] {lid} v{va}→v{vb}: {out['llm_error'][:120]}")
    else:
        parsed, err = parse_llm_response(raw)
        out["llm_result"] = parsed if parsed is not None else {"raw_output": raw}
        out["llm_error"]  = err
        if err:
            logger.warning(f"[{repo}] {lid} v{va}→v{vb}: parse error — {err[:80]}")
        else:
            logger.debug(f"[{repo}] {lid} v{va}→v{vb}: OK")

    return out


# ── Main async runner ─────────────────────────────────────────────────────

async def run_all(
    records: list[dict],
    outfile: Path,
    model: str,
    parallel: int,
    base_url: str,
    logger: logging.Logger,
    skip_existing: bool = True,
    retry_errors: bool = False,
    max_input_tokens: int | None = 250000,
) -> None:
    """Process all records, optionally retrying pairs whose prior result had llm_error."""
    done: set[tuple] = set()
    prior_errors: set[tuple] = set()
    if skip_existing and outfile.exists():
        with outfile.open(encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    r = json.loads(line)
                    key = (r.get("lineage_id"), r.get("version_a"), r.get("version_b"))
                    if r.get("llm_error"):
                        prior_errors.add(key)
                    else:
                        done.add(key)
                except Exception:
                    pass
        if retry_errors:
            logger.info(
                f"Resuming: {len(done)} successful pairs already done; "
                f"{len(prior_errors)} prior error pairs will be retried."
            )
        else:
            done |= prior_errors
            if done:
                logger.info(f"Resuming: {len(done)} pairs already done, skipping.")

    pending = [
        r for r in records
        if (r.get("lineage_id"), r.get("version_a"), r.get("version_b")) not in done
    ]
    logger.info(f"{len(pending):,} pairs to process ({len(records)-len(pending):,} skipped).")

    if not pending:
        logger.info("Nothing to do.")
        return

    semaphore = asyncio.Semaphore(parallel)
    token_encoder = get_token_encoder(model)
    outfile.parent.mkdir(parents=True, exist_ok=True)

    if max_input_tokens is None:
        logger.info("Prompt-size guard disabled.")
    else:
        method = "tiktoken" if token_encoder is not None else "char_fallback"
        logger.info(
            f"Prompt-size guard enabled: max_input_tokens={max_input_tokens} "
            f"(counting={method})"
        )
        if token_encoder is None:
            logger.warning(
                "tiktoken not available; prompt-size guard is using a conservative "
                "character-based estimate."
            )

    async with aiohttp.ClientSession() as session:
        tasks = [
            asyncio.create_task(
                process_record(
                    session,
                    model,
                    rec,
                    semaphore,
                    logger,
                    base_url,
                    max_input_tokens,
                    token_encoder,
                )
            )
            for rec in pending
        ]
        # Write results as they complete; append mode for resume support
        with outfile.open("a", encoding="utf-8") as fout:
            success = errors = 0
            for coro in asyncio.as_completed(tasks):
                result = await coro
                fout.write(json.dumps(result) + "\n")
                fout.flush()
                if result.get("llm_error"):
                    errors += 1
                else:
                    success += 1

    logger.info(
        f"Done: {success} OK, {errors} errors. Results → {outfile}"
    )


# ── Aggregation ───────────────────────────────────────────────────────────

def aggregate_results(root: Path, logger: logging.Logger) -> None:
    """Merge results JSONL files, preferring successful rows for duplicate keys."""
    out_path = root / "aggregate_results.jsonl"
    result_files = sorted(
        fpath for fpath in root.rglob("results*.jsonl")
        if fpath != out_path
    )
    if not result_files:
        logger.warning(f"No results*.jsonl files found under {root}")
        return

    merged: dict[tuple, dict] = {}
    total = errors = duplicates = replaced = 0
    with out_path.open("w", encoding="utf-8") as fout:
        for fpath in result_files:
            with fpath.open(encoding="utf-8") as fin:
                for line in fin:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        obj = json.loads(line)
                        total += 1
                        key = (obj.get("lineage_id"), obj.get("version_a"), obj.get("version_b"))
                        existing = merged.get(key)
                        if existing is None:
                            merged[key] = obj
                            continue

                        duplicates += 1
                        existing_has_error = bool(existing.get("llm_error"))
                        new_has_error = bool(obj.get("llm_error"))

                        should_replace = False
                        if existing_has_error and not new_has_error:
                            should_replace = True
                        elif existing_has_error == new_has_error:
                            # For equally good rows, later files overwrite earlier ones.
                            should_replace = True

                        if should_replace:
                            merged[key] = obj
                            replaced += 1
                    except Exception as exc:
                        logger.warning(f"Skipping bad line in {fpath}: {exc}")

        for obj in merged.values():
            fout.write(json.dumps(obj) + "\n")
            if obj.get("llm_error"):
                errors += 1

    logger.info(
        f"Aggregated {total:,} input records into {len(merged):,} unique pairs "
        f"({duplicates:,} overlaps, {replaced:,} replacements, {errors:,} with errors) "
        f"→ {out_path}"
    )


# ── CLI ───────────────────────────────────────────────────────────────────

def main() -> None:
    parser = argparse.ArgumentParser(
        description="Send prompt records to an LLM and save structured results."
    )
    parser.add_argument(
        "--input", metavar="PROMPTS_JSONL",
        help="Input prompts JSONL (from prepare_llm_prompts.py).",
    )
    parser.add_argument(
        "--outfile", metavar="RESULTS_JSONL",
        help="Output results JSONL. Defaults to same dir as input: results.jsonl.",
    )
    parser.add_argument(
        "--model", default="gpt-4o-mini",
        help="LLM model name (default: gpt-4o-mini).",
    )
    parser.add_argument(
        "--parallel", type=int, default=10,
        help="Max concurrent API requests (default: 10).",
    )
    parser.add_argument(
        "--base-url", default="https://api.openai.com/v1",
        help="API base URL (default: OpenAI; override for compatible endpoints).",
    )
    parser.add_argument(
        "--max-input-tokens", type=int, default=250000,
        help=(
            "Preflight guard for prompt size. Requests estimated above this token count "
            "are marked with llm_error=prompt_too_large instead of being sent "
            "(default: 250000). Use 0 to disable."
        ),
    )
    parser.add_argument(
        "--limit", type=int, default=None,
        help="Process only the first N records.",
    )
    parser.add_argument(
        "--dry-run", action="store_true",
        help="Print prompts to stdout without calling the API.",
    )
    parser.add_argument(
        "--no-resume", action="store_true",
        help="Re-process all records even if outfile already has results.",
    )
    parser.add_argument(
        "--retry-errors", action="store_true",
        help="When resuming, only skip prior successful rows; rows with llm_error stay pending.",
    )
    parser.add_argument(
        "--only-errors-from", metavar="RESULTS_JSONL",
        help="Process only prompts whose lineage/version pair is missing or errored in this results file.",
    )
    parser.add_argument(
        "--aggregate", metavar="DIR",
        help="Merge all results.jsonl files under DIR into aggregate_results.jsonl.",
    )
    parser.add_argument(
        "--log-level", default="INFO",
        choices=["DEBUG", "INFO", "WARNING", "ERROR"],
    )
    args = parser.parse_args()

    start_time = datetime.now()
    logger = setup_logging(level=getattr(logging, args.log_level))
    logger.info(f"=== run_llm started {start_time:%Y-%m-%d %H:%M:%S} ===")

    # ── Aggregate-only mode ────────────────────────────────────────────────
    if args.aggregate:
        aggregate_results(Path(args.aggregate), logger)
        return

    # ── Normal mode ───────────────────────────────────────────────────────
    if not args.input:
        parser.error("--input is required unless --aggregate is used.")

    if aiohttp is None and not args.dry_run:
        logger.error("aiohttp / aiofiles not installed. Run: pip install aiohttp aiofiles")
        sys.exit(1)

    input_path = Path(args.input)
    if not input_path.exists():
        parser.error(f"Input file not found: {input_path}")

    # Load records
    records: list[dict] = []
    with input_path.open(encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if line:
                records.append(json.loads(line))

    if args.only_errors_from:
        ref_path = Path(args.only_errors_from)
        if not ref_path.exists():
            parser.error(f"Reference results file not found: {ref_path}")
        successful: set[tuple] = set()
        errored: set[tuple] = set()
        with ref_path.open(encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    row = json.loads(line)
                except Exception:
                    continue
                key = (row.get("lineage_id"), row.get("version_a"), row.get("version_b"))
                if row.get("llm_error"):
                    errored.add(key)
                else:
                    successful.add(key)
        target_keys = errored | {
            (r.get("lineage_id"), r.get("version_a"), r.get("version_b"))
            for r in records
            if (r.get("lineage_id"), r.get("version_a"), r.get("version_b")) not in successful
        }
        before = len(records)
        records = [
            r for r in records
            if (r.get("lineage_id"), r.get("version_a"), r.get("version_b")) in target_keys
        ]
        logger.info(
            f"Filtered against {ref_path}: {len(records):,} pending/error records "
            f"selected from {before:,} prompts."
        )

    if args.limit:
        records = records[: args.limit]
    logger.info(f"Loaded {len(records):,} prompt records from {input_path}")

    # ── Dry run ───────────────────────────────────────────────────────────
    if args.dry_run:
        for i, rec in enumerate(records, 1):
            print(f"\n{'='*70}")
            print(f"Record {i}: [{rec.get('repo')}] {rec.get('lineage_id')} "
                  f"v{rec.get('version_a')}→v{rec.get('version_b')}")
            print(f"Rule: {rec.get('rule_canonical')}")
            print(f"{'─'*70}")
            print(rec["prompt"])
        return

    # ── Resolve output path ───────────────────────────────────────────────
    if args.outfile:
        outfile = Path(args.outfile)
    else:
        outfile = input_path.parent / "results.jsonl"

    logger.info(f"Output → {outfile}  |  model={args.model}  |  parallel={args.parallel}")

    # ── Run ───────────────────────────────────────────────────────────────
    asyncio.run(
        run_all(
            records      = records,
            outfile      = outfile,
            model        = args.model,
            parallel     = args.parallel,
            base_url     = args.base_url,
            logger       = logger,
            skip_existing= not args.no_resume,
            retry_errors = args.retry_errors,
            max_input_tokens = (None if args.max_input_tokens == 0 else args.max_input_tokens),
        )
    )

    elapsed = (datetime.now() - start_time).total_seconds()
    logger.info(f"=== Finished in {elapsed:.1f}s ===")


if __name__ == "__main__":
    main()
