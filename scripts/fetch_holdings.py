"""抓取台股主動式ETF每日持股變動,存成 data/latest.json 與 data/archive/<date>.json。"""
import gzip
import json
import time
import urllib.error
from datetime import datetime, timezone, timedelta
from pathlib import Path
from urllib.request import Request, urlopen


def _read_body(resp) -> bytes:
    raw = resp.read()
    if resp.headers.get("Content-Encoding") == "gzip":
        raw = gzip.decompress(raw)
    return raw


def _request_with_retry(url: str, retries: int = 3, delay: float = 2.0) -> bytes:
    last_error = None
    for attempt in range(retries):
        try:
            req = Request(url, headers=HEADERS)
            with urlopen(req, timeout=20) as resp:
                return _read_body(resp)
        except urllib.error.HTTPError as e:
            last_error = e
            time.sleep(delay * (attempt + 1))
    raise last_error

ETF_NAMES = {
    "00981a": "主動統一台股增長",
    "00403a": "主動統一升級50",
    "00991a": "主動復華未來50",
    "00982a": "主動群益台灣強棒",
    "00992a": "主動群益科技創新",
}
ETF_CODES = list(ETF_NAMES.keys())
HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0 Safari/537.36",
    "Accept": "application/json, text/html;q=0.9, */*;q=0.8",
    "Accept-Language": "zh-TW,zh;q=0.9,en;q=0.8",
    "Accept-Encoding": "gzip, deflate",
    "Referer": "https://www.wantgoo.com/",
}
ROOT = Path(__file__).resolve().parent.parent
TAIPEI = timezone(timedelta(hours=8))


def fetch_json(url: str) -> dict:
    return json.loads(_request_with_retry(url).decode("utf-8"))


def fetch_fund_name(code: str) -> str:
    return ETF_NAMES.get(code, code.upper())


def diff_holdings(current: list, previous: list) -> list:
    prev_by_code = {row["stockCode"]: row for row in previous}
    curr_by_code = {row["stockCode"]: row for row in current}
    rows = []

    for code, curr in curr_by_code.items():
        prev = prev_by_code.get(code)
        shares_prev = prev["shares"] if prev else 0
        change = curr["shares"] - shares_prev
        rows.append({
            "stockCode": code,
            "stockName": curr["stockName"],
            "sharesPrev": shares_prev,
            "sharesNew": curr["shares"],
            "change": change,
            "weight": curr["weight"],
            "status": "new" if prev is None else ("unchanged" if change == 0 else ("increase" if change > 0 else "decrease")),
        })

    for code, prev in prev_by_code.items():
        if code not in curr_by_code:
            rows.append({
                "stockCode": code,
                "stockName": prev["stockName"],
                "sharesPrev": prev["shares"],
                "sharesNew": 0,
                "change": -prev["shares"],
                "weight": 0,
                "status": "removed",
            })

    rows.sort(key=lambda r: abs(r["change"]), reverse=True)
    return rows


def fetch_etf(code: str) -> dict:
    data = fetch_json(f"https://www.wantgoo.com/stock/etf/{code}/all-constituent-combine-data")
    as_of = datetime.fromtimestamp(data["date"] / 1000, tz=timezone.utc).astimezone(TAIPEI).strftime("%Y-%m-%d")
    holdings = diff_holdings(data.get("stockHoldings", []), data.get("previousStockHoldings", []))
    return {
        "code": code.upper(),
        "name": fetch_fund_name(code),
        "asOfDate": as_of,
        "holdings": holdings,
    }


def main():
    results = []
    for i, code in enumerate(ETF_CODES):
        if i > 0:
            time.sleep(1.5)
        results.append(fetch_etf(code))

    updated_at = datetime.now(TAIPEI).strftime("%Y-%m-%d %H:%M:%S")
    payload = {"updatedAt": updated_at, "etfs": results}

    data_dir = ROOT / "data"
    data_dir.mkdir(exist_ok=True)
    (data_dir / "latest.json").write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")

    archive_dir = data_dir / "archive"
    archive_dir.mkdir(exist_ok=True)
    as_of_date = results[0]["asOfDate"] if results else datetime.now(TAIPEI).strftime("%Y-%m-%d")
    (archive_dir / f"{as_of_date}.json").write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")

    print(f"Saved {len(results)} ETFs, as of {as_of_date}")


if __name__ == "__main__":
    main()
