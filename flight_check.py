#!/usr/bin/env python3
"""Search China Southern and return observed round-trip price information."""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import re
import traceback
from datetime import datetime
from pathlib import Path
from typing import Any
from urllib.parse import urlencode
from urllib.request import Request, urlopen

from dotenv import load_dotenv


# Local development: load secrets and settings from the .env file beside this
# script. Existing shell or GCP environment variables take precedence.
load_dotenv(Path(__file__).with_name(".env"), override=False)


BASE_URL = "https://oversea.csair.com/tka/us/en/book/flights"
PRICE_RE = re.compile(
    r"(?P<currency>USD|CNY|RMB|EUR|GBP|JPY|CAD|AUD|\$|¥|€|£)\s*"
    r"(?P<amount>\d{1,3}(?:,\d{3})*(?:\.\d{1,2})?)",
    re.IGNORECASE,
)


def compact_date(value: str) -> str:
    return datetime.strptime(value, "%Y-%m-%d").strftime("%Y%m%d")


def build_url(
    origin: str,
    destination: str,
    departure: str,
    return_date: str,
    adults: int,
    flexible: bool,
) -> str:
    route = f"{origin.upper()}-{destination.upper()}-{compact_date(departure)}-{compact_date(return_date)}"
    query = urlencode(
        {
            "m": adults,
            "p": 100,
            "flex": 1 if flexible else 0,
            "t": route,
            "orderChannel": "JPSS-JPCX",
        }
    )
    return f"{BASE_URL}?{query}"


def price_candidates(text: str) -> list[dict[str, Any]]:
    found: list[dict[str, Any]] = []
    seen: set[tuple[str, str]] = set()
    for match in PRICE_RE.finditer(text):
        currency = match.group("currency").upper()
        amount = match.group("amount").replace(",", "")
        key = (currency, amount)
        if key in seen:
            continue
        seen.add(key)
        start = max(0, match.start() - 100)
        end = min(len(text), match.end() + 160)
        context = " ".join(text[start:end].split())
        found.append({"currency": currency, "amount": float(amount), "context": context})
    return found


def labeled_total_fares(text: str) -> list[dict[str, Any]]:
    """Return visible prices whose nearby page text explicitly says Total Fare."""
    totals: list[dict[str, Any]] = []
    seen: set[tuple[str, float]] = set()
    for match in PRICE_RE.finditer(text):
        currency = match.group("currency").upper()
        amount = float(match.group("amount").replace(",", ""))
        start = max(0, match.start() - 100)
        end = min(len(text), match.end() + 160)
        context = " ".join(text[start:end].split())
        if re.search(r"\btotal\s+(?:round[- ]?trip\s+)?fare\b", context, re.IGNORECASE):
            key = (currency, amount)
            if key not in seen:
                seen.add(key)
                totals.append({"currency": currency, "amount": amount, "context": context})
    return sorted(totals, key=lambda item: item["amount"])


async def collect_page(url: str, output_dir: Path, show_browser: bool) -> dict[str, Any]:
    try:
        from playwright.async_api import async_playwright
    except ImportError as exc:
        raise SystemExit(
            "Playwright is not installed. Follow the README setup instructions first."
        ) from exc

    output_dir.mkdir(parents=True, exist_ok=True)
    network: list[dict[str, Any]] = []

    async with async_playwright() as playwright:
        browser = await playwright.chromium.launch(headless=not show_browser)
        context = await browser.new_context(
            locale="en-US",
            viewport={"width": 1440, "height": 1000},
            user_agent=(
                "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/140.0 Safari/537.36"
            ),
        )
        page = await context.new_page()

        async def record(response) -> None:
            headers = await response.all_headers()
            content_type = headers.get("content-type", "")
            request = response.request
            interesting = (
                request.resource_type in {"xhr", "fetch"}
                or "json" in content_type.lower()
                or any(word in response.url.lower() for word in ("flight", "fare", "search", "shopping"))
            )
            if not interesting:
                return
            item: dict[str, Any] = {
                "url": response.url,
                "status": response.status,
                "method": request.method,
                "resource_type": request.resource_type,
                "content_type": content_type,
            }
            try:
                item["body"] = (await response.text())[:1_000_000]
            except Exception as error:
                item["body_error"] = str(error)
            network.append(item)

        page.on("response", record)
        main = await page.goto(url, wait_until="domcontentloaded", timeout=90_000)
        try:
            await page.wait_for_load_state("networkidle", timeout=45_000)
        except Exception:
            pass
        await page.wait_for_timeout(12_000)

        visible_text = await page.locator("body").inner_text()
        html = await page.content()
        title = await page.title()
        final_url = page.url

        (output_dir / "visible_text.txt").write_text(visible_text, encoding="utf-8")
        (output_dir / "page.html").write_text(html, encoding="utf-8")
        (output_dir / "network_responses.json").write_text(
            json.dumps(network, indent=2, ensure_ascii=False), encoding="utf-8"
        )
        await page.screenshot(path=output_dir / "page.png", full_page=True)
        await browser.close()

    combined = visible_text + "\n" + "\n".join(str(item.get("body", "")) for item in network)
    return {
        "requested_url": url,
        "final_url": final_url,
        "status": main.status if main else None,
        "title": title,
        "network_response_count": len(network),
        "price_candidates": price_candidates(combined),
        # China Southern labels the lowest round-trip search quote "Total Fare".
        # Keep this separate from unlabeled amounts found in API/debug payloads.
        "visible_labeled_total_fares": labeled_total_fares(visible_text),
        "visible_text": visible_text,
        "network": network,
    }


def summarize_with_openai(raw: dict[str, Any], model: str) -> dict[str, Any]:
    """Normalize observed evidence. Never asks the model to browse or guess."""
    try:
        from openai import OpenAI
        from pydantic import BaseModel, Field
    except ImportError as exc:
        raise SystemExit("OpenAI mode requires the openai and pydantic packages.") from exc

    if not os.getenv("OPENAI_API_KEY"):
        raise SystemExit("Set OPENAI_API_KEY before using --use-llm.")

    class Fare(BaseModel):
        origin: str
        destination: str
        departure_date: str
        return_date: str
        currency: str | None
        total_round_trip_price: float | None
        outbound_price: float | None
        return_price: float | None
        itinerary: list[str] = Field(default_factory=list)
        confidence: str
        evidence: list[str] = Field(default_factory=list)
        notes: list[str] = Field(default_factory=list)

    evidence = {
        "url": raw["requested_url"],
        "page_title": raw["title"],
        "price_candidates": raw["price_candidates"][:100],
        "visible_labeled_total_fares": raw["visible_labeled_total_fares"][:20],
        "visible_text": raw["visible_text"][:80_000],
        "network_bodies": [
            {"url": item["url"], "body": str(item.get("body", ""))[:80_000]}
            for item in raw["network"][:30]
        ],
    }

    client = OpenAI()
    response = client.responses.parse(
        model=model,
        input=[
            {
                "role": "developer",
                "content": (
                    "Extract a flight fare only from the supplied browser evidence. "
                    "Do not estimate, infer, or invent a price. Distinguish one-way, "
                    "per-segment, and total round-trip prices. This is a round-trip search on "
                    "China Southern: an amount visibly and explicitly labeled 'Total Fare' is "
                    "the site's tax-inclusive round-trip quote for the displayed itinerary, "
                    "not an outbound-only price. Use the lowest such visible quote as "
                    "total_round_trip_price. If no evidence proves a total price, set "
                    "total_round_trip_price to null and explain why."
                ),
            },
            {"role": "user", "content": json.dumps(evidence, ensure_ascii=False)},
        ],
        text_format=Fare,
    )
    result = response.output_parsed.model_dump()

    # Structured-output models can still be over-cautious. The airline's visible
    # "Total Fare" label is stronger evidence than an interpretation of an
    # unlabeled network value, so preserve it deterministically when necessary.
    labeled_totals = raw["visible_labeled_total_fares"]
    if result["total_round_trip_price"] is None and labeled_totals:
        best = labeled_totals[0]
        result["total_round_trip_price"] = best["amount"]
        result["currency"] = best["currency"]
        result["confidence"] = "High"
        result["evidence"].append(best["context"])
        result["notes"] = [
            "China Southern explicitly labels this amount as Total Fare for the round-trip search."
        ]
    return result


def telegram_send(message: str) -> None:
    token = os.getenv("TELEGRAM_BOT_TOKEN")
    chat_ids_value = os.getenv("TELEGRAM_CHAT_IDS") or os.getenv("TELEGRAM_CHAT_ID", "")
    chat_ids = [chat_id.strip() for chat_id in chat_ids_value.split(",") if chat_id.strip()]
    if not token or not chat_ids:
        raise RuntimeError(
            "TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_IDS (or TELEGRAM_CHAT_ID) are required"
        )
    for chat_id in chat_ids:
        body = urlencode(
            {
                "chat_id": chat_id,
                "text": message[:4096],
                "disable_web_page_preview": "true",
            }
        ).encode()
        request = Request(
            f"https://api.telegram.org/bot{token}/sendMessage",
            data=body,
            method="POST",
        )
        with urlopen(request, timeout=30) as response:
            result = json.loads(response.read().decode("utf-8"))
        if not result.get("ok"):
            raise RuntimeError(f"Telegram API error for chat {chat_id}: {result}")


def state_key(search: dict[str, Any]) -> str:
    values = [
        search["origin"],
        search["destination"],
        search["departure"],
        search["return"],
        str(search["adults"]),
    ]
    return "-".join(re.sub(r"[^A-Za-z0-9-]", "", value) for value in values) + ".json"


def load_previous(bucket_name: str | None, key: str, local_dir: Path) -> dict[str, Any] | None:
    if bucket_name:
        from google.cloud import storage

        blob = storage.Client().bucket(bucket_name).blob(f"flight-check/{key}")
        if not blob.exists():
            return None
        return json.loads(blob.download_as_text())
    path = local_dir / "previous_result.json"
    return json.loads(path.read_text()) if path.exists() else None


def save_current(bucket_name: str | None, key: str, value: dict[str, Any], local_dir: Path) -> None:
    text = json.dumps(value, indent=2, ensure_ascii=False)
    if bucket_name:
        from google.cloud import storage

        storage.Client().bucket(bucket_name).blob(f"flight-check/{key}").upload_from_string(
            text, content_type="application/json"
        )
    else:
        (local_dir / "previous_result.json").write_text(text, encoding="utf-8")


def total_price(result: dict[str, Any]) -> float | None:
    value = result.get("llm_result", {}).get("total_round_trip_price")
    return float(value) if value is not None else None


def should_notify(
    mode: str,
    current: float | None,
    previous: float | None,
    threshold: float | None,
) -> bool:
    if mode == "always":
        return True
    if current is None:
        return False
    if mode == "change":
        return previous is None or current != previous
    if mode == "drop":
        return previous is None or current < previous
    if mode == "threshold":
        return threshold is not None and current <= threshold
    raise ValueError(f"Unknown notification mode: {mode}")


def telegram_message(result: dict[str, Any], previous_price: float | None) -> str:
    search = result["search"]
    fare = result.get("llm_result", {})
    current = fare.get("total_round_trip_price")
    currency = fare.get("currency") or ""
    if current is None:
        price_line = "Round-trip price: not confirmed from the captured evidence"
        price_line_zh = "往返票价：无法根据已抓取的信息确认"
    else:
        price_line = f"Round-trip price: {currency} {current:,.2f}".strip()
        price_line_zh = f"往返票价：{currency} {current:,.2f}".strip()
    change_line = ""
    change_line_zh = ""
    if current is not None and previous_price is not None:
        difference = float(current) - previous_price
        change_line = f"\nChange from previous check: {difference:+,.2f}"
        change_line_zh = f"\n与上次查询相比：{difference:+,.2f}"
    notes = fare.get("notes") or []
    note_line = f"\nNote: {notes[0]}" if notes else ""
    if current is None:
        note_zh = "目前页面信息不足，无法确认完整往返票价。"
    else:
        note_zh = "中国南方航空将此金额明确标为该往返搜索的总票价。"
    note_line_zh = f"\n说明：{note_zh}" if notes else ""
    confidence = fare.get("confidence", "unknown")
    confidence_zh = {
        "high": "高",
        "medium": "中",
        "low": "低",
        "unknown": "未知",
    }.get(str(confidence).lower(), str(confidence))
    return (
        "✈️ Flight price check\n"
        f"{search['origin']} → {search['destination']}\n"
        f"Depart: {search['departure']} | Return: {search['return']}\n"
        f"{price_line}{change_line}\n"
        f"Confidence: {confidence}"
        f"{note_line}\n"
        f"Search: {result['final_url']}\n\n"
        "✈️ 航班价格查询\n"
        f"{search['origin']} → {search['destination']}\n"
        f"出发日期：{search['departure']} | 返回日期：{search['return']}\n"
        f"{price_line_zh}{change_line_zh}\n"
        f"置信度：{confidence_zh}"
        f"{note_line_zh}\n"
        f"查询链接：{result['final_url']}"
    )


async def async_main(args: argparse.Namespace) -> None:
    url = build_url(
        args.origin,
        args.destination,
        args.departure,
        args.return_date,
        args.adults,
        args.flexible,
    )
    print("Search URL:", url)
    raw = await collect_page(url, args.output, args.show_browser)

    result: dict[str, Any] = {
        "search": {
            "origin": args.origin.upper(),
            "destination": args.destination.upper(),
            "departure": args.departure,
            "return": args.return_date,
            "adults": args.adults,
            "flexible": args.flexible,
        },
        "page_status": raw["status"],
        "final_url": raw["final_url"],
        "price_candidates": raw["price_candidates"],
    }

    if args.use_llm:
        result["llm_result"] = summarize_with_openai(raw, args.model)

    result_path = args.output / "result.json"
    result_path.write_text(json.dumps(result, indent=2, ensure_ascii=False), encoding="utf-8")
    print(json.dumps(result, indent=2, ensure_ascii=False))
    print("Saved result:", result_path.resolve())

    if args.notify:
        key = state_key(result["search"])
        previous = load_previous(args.state_bucket, key, args.output)
        previous_price = total_price(previous) if previous else None
        current_price = total_price(result)
        if should_notify(args.notify_mode, current_price, previous_price, args.price_threshold):
            telegram_send(telegram_message(result, previous_price))
            print("Telegram notification sent")
        else:
            print("Notification skipped by rule:", args.notify_mode)
        save_current(args.state_bucket, key, result, args.output)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--origin", default=os.getenv("FLIGHT_ORIGIN"), help="IATA code, e.g. SFO")
    parser.add_argument("--destination", default=os.getenv("FLIGHT_DESTINATION"), help="IATA code, e.g. WUH")
    parser.add_argument("--departure", default=os.getenv("FLIGHT_DEPARTURE"), help="YYYY-MM-DD")
    parser.add_argument("--return-date", default=os.getenv("FLIGHT_RETURN"), help="YYYY-MM-DD")
    parser.add_argument("--adults", type=int, default=int(os.getenv("FLIGHT_ADULTS", "1")))
    parser.add_argument(
        "--flexible",
        action=argparse.BooleanOptionalAction,
        default=os.getenv("FLIGHT_FLEXIBLE", "true").lower() == "true",
    )
    parser.add_argument("--show-browser", action="store_true")
    parser.add_argument(
        "--use-llm",
        action=argparse.BooleanOptionalAction,
        default=os.getenv("USE_LLM", "false").lower() == "true",
    )
    parser.add_argument("--model", default=os.getenv("OPENAI_MODEL", "gpt-4o-mini"))
    parser.add_argument("--output", type=Path, default=Path("results"))
    parser.add_argument(
        "--notify",
        action=argparse.BooleanOptionalAction,
        default=os.getenv("TELEGRAM_NOTIFY", "false").lower() == "true",
    )
    parser.add_argument(
        "--notify-mode",
        choices=("always", "change", "drop", "threshold"),
        default=os.getenv("NOTIFY_MODE", "change"),
    )
    parser.add_argument(
        "--price-threshold",
        type=float,
        default=float(os.environ["PRICE_THRESHOLD"]) if os.getenv("PRICE_THRESHOLD") else None,
    )
    parser.add_argument("--state-bucket", default=os.getenv("STATE_BUCKET"))
    args = parser.parse_args()

    missing = [
        name
        for name, value in (
            ("origin/FLIGHT_ORIGIN", args.origin),
            ("destination/FLIGHT_DESTINATION", args.destination),
            ("departure/FLIGHT_DEPARTURE", args.departure),
            ("return-date/FLIGHT_RETURN", args.return_date),
        )
        if not value
    ]
    if missing:
        parser.error("Missing required settings: " + ", ".join(missing))
    if args.adults < 1:
        parser.error("--adults must be at least 1")
    for value in (args.departure, args.return_date):
        try:
            datetime.strptime(value, "%Y-%m-%d")
        except ValueError:
            parser.error(f"Invalid date {value!r}; use YYYY-MM-DD")

    try:
        asyncio.run(async_main(args))
    except Exception as error:
        print(traceback.format_exc())
        if os.getenv("TELEGRAM_NOTIFY_ERRORS", "true").lower() == "true":
            try:
                telegram_send(f"❌ Flight check failed\n{type(error).__name__}: {error}")
            except Exception as telegram_error:
                print("Could not send Telegram error notification:", telegram_error)
        raise


if __name__ == "__main__":
    main()
