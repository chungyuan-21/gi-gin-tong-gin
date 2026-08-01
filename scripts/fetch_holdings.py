"""抓取台股主動式ETF每日持股變動,存成 data/latest.json 與 data/archive/<date>.json。"""
import json
import subprocess
import time
from datetime import datetime, timezone, timedelta
from pathlib import Path

ETF_NAMES = {
    "00981a": "主動統一台股增長",
    "00403a": "主動統一升級50",
    "00991a": "主動復華未來50",
    "00982a": "主動群益台灣強棒",
    "00992a": "主動群益科技創新",
}
ETF_CODES = list(ETF_NAMES.keys())
USER_AGENT = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0 Safari/537.36"
ROOT = Path(__file__).resolve().parent.parent
TAIPEI = timezone(timedelta(hours=8))


def _request_with_retry(url: str, retries: int = 6, delay: float = 15.0) -> bytes:
    # wantgoo 對這支 API 的請求頻率很敏感,短時間內多次呼叫容易被 Cloudflare 暫時擋下 400,
    # 這是排程每天只跑一次的背景腳本,不趕時間,遇到就多等一下重試即可。
    last_error = None
    for attempt in range(retries):
        result = subprocess.run(
            [
                "curl", "-sS", "-f", "--compressed",
                "-H", f"User-Agent: {USER_AGENT}",
                "-H", "Accept: application/json, text/plain, */*",
                "-H", "Referer: https://www.wantgoo.com/",
                url,
            ],
            capture_output=True,
            timeout=25,
        )
        if result.returncode == 0:
            return result.stdout
        last_error = RuntimeError(f"curl failed ({result.returncode}) for {url}: {result.stderr.decode(errors='replace')}")
        time.sleep(delay * (attempt + 1))
    raise last_error


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


def build_cross_fund_signals(results: list) -> dict:
    by_stock = {}
    for etf in results:
        for h in etf["holdings"]:
            if h["status"] == "unchanged":
                continue
            entry = by_stock.setdefault(h["stockCode"], {"stockName": h["stockName"], "funds": []})
            entry["funds"].append({
                "code": etf["code"],
                "status": h["status"],
                "change": h["change"],
                "weight": h["weight"],
            })

    buy_statuses = {"new", "increase"}
    sell_statuses = {"decrease", "removed"}

    def build_group(statuses: set) -> list:
        group = []
        for stock_code, entry in by_stock.items():
            funds = [f for f in entry["funds"] if f["status"] in statuses]
            if len(funds) >= 2:
                group.append({
                    "stockCode": stock_code,
                    "stockName": entry["stockName"],
                    "count": len(funds),
                    "funds": funds,
                })
        group.sort(key=lambda r: r["count"], reverse=True)
        return group

    return {
        "coBuy": build_group(buy_statuses),
        "coSell": build_group(sell_statuses),
        "coNew": build_group({"new"}),
    }


def fetch_etf(code: str) -> dict:
    data = fetch_json(f"https://www.wantgoo.com/stock/etf/{code}/all-constituent-combine-data")
    as_of = datetime.fromtimestamp(data["date"] / 1000, tz=timezone.utc).astimezone(TAIPEI).strftime("%Y-%m-%d")
    holdings = diff_holdings(data.get("stockHoldings", []), data.get("previousStockHoldings", []))
    return {
        "code": code.upper(),
        "name": fetch_fund_name(code),
        "asOfDate": as_of,
        "stale": False,
        "holdings": holdings,
    }


def load_previous_etfs() -> dict:
    latest_path = ROOT / "data" / "latest.json"
    if not latest_path.exists():
        return {}
    try:
        old = json.loads(latest_path.read_text(encoding="utf-8"))
        return {etf["code"]: etf for etf in old.get("etfs", [])}
    except Exception:
        return {}


def main():
    previous_etfs = load_previous_etfs()
    results = []
    for i, code in enumerate(ETF_CODES):
        if i > 0:
            time.sleep(20)
        try:
            results.append(fetch_etf(code))
        except Exception as e:
            print(f"WARN: failed to fetch {code}, will reuse previous data if available: {e}")
            fallback = previous_etfs.get(code.upper())
            if fallback:
                fallback = dict(fallback, stale=True)
                results.append(fallback)
            else:
                print(f"WARN: no previous data for {code} either, skipping it for this run")

    if not results:
        raise SystemExit("No ETF data could be fetched or recovered; aborting without overwriting existing data.")

    updated_at = datetime.now(TAIPEI).strftime("%Y-%m-%d %H:%M:%S")
    payload = {
        "updatedAt": updated_at,
        "etfs": results,
        "crossFundSignals": build_cross_fund_signals(results),
    }

    data_dir = ROOT / "data"
    data_dir.mkdir(exist_ok=True)
    (data_dir / "latest.json").write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")

    archive_dir = data_dir / "archive"
    archive_dir.mkdir(exist_ok=True)
    as_of_date = max(etf["asOfDate"] for etf in results)
    (archive_dir / f"{as_of_date}.json").write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")

    stale_count = sum(1 for etf in results if etf.get("stale"))
    print(f"Saved {len(results)} ETFs ({stale_count} stale), as of {as_of_date}")


if __name__ == "__main__":
    main()
