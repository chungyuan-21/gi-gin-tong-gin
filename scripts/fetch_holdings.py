"""抓取台股主動式ETF每日持股,存成 data/latest.json 與 data/archive/<date>.json。

資料源改用各投信官網(第一手揭露資料),不再透過第三方網站:
- 00981A / 00403A: 統一投信 ezmoney.com.tw
- 00991A: 復華投信 fhtrust.com.tw
- 00982A / 00992A: 群益投信 capitalfund.com.tw

官網不像先前的第三方彙整站會附帶「前一交易日」持股,所以自己在 data/snapshots/
存前一次抓到的持股快照,每次抓到新資料後跟快照比對算出增減,再更新快照。
第一次執行(沒有快照)不會有假的「全部新增」,而是顯示無變動,等下一次執行才會有真正的比較基準。
"""
import html
import json
import re
import subprocess
import time
from datetime import datetime, timezone, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
TAIPEI = timezone(timedelta(hours=8))
USER_AGENT = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0 Safari/537.36"

ETF_CONFIGS = [
    {"code": "00981A", "name": "主動統一台股增長", "source": "ezmoney", "param": "49YTW"},
    {"code": "00403A", "name": "主動統一升級50", "source": "ezmoney", "param": "63YTW"},
    {"code": "00991A", "name": "主動復華未來50", "source": "fhtrust", "param": "ETF23"},
    {"code": "00982A", "name": "主動群益台灣強棒", "source": "capital", "param": 399},
    {"code": "00992A", "name": "主動群益科技創新", "source": "capital", "param": 500},
]


def curl(args: list, retries: int = 3, delay: float = 5.0) -> bytes:
    last_error = None
    for attempt in range(retries):
        result = subprocess.run(["curl", "-sS", "-f", *args], capture_output=True, timeout=25)
        if result.returncode == 0:
            return result.stdout
        last_error = RuntimeError(f"curl failed ({result.returncode}): {result.stderr.decode(errors='replace')}")
        time.sleep(delay * (attempt + 1))
    raise last_error


def fetch_ezmoney(fund_code: str):
    cookie_path = ROOT / f".curl-cookies-{fund_code}.tmp"
    try:
        body = curl([
            "-L", "-c", str(cookie_path), "-b", str(cookie_path),
            "-A", USER_AGENT,
            f"https://www.ezmoney.com.tw/ETF/Fund/Info?fundCode={fund_code}",
        ])
    finally:
        cookie_path.unlink(missing_ok=True)

    page = html.unescape(body.decode("utf-8", errors="replace"))
    objs = re.findall(r'\{"FundCode":"[^{}]*?"AssetCode":"ST"[^{}]*?\}', page)

    holdings = []
    as_of = None
    for raw in objs:
        try:
            o = json.loads(raw)
        except json.JSONDecodeError:
            continue
        holdings.append({
            "stockCode": o["DetailCode"],
            "stockName": o["DetailName"],
            "shares": float(o["Share"]),
            "weight": float(o["NavRate"]),
        })
        as_of = as_of or o["TranDate"][:10]

    if not holdings:
        raise RuntimeError(f"ezmoney: no holdings parsed for fundCode={fund_code}")
    return holdings, as_of


def fetch_fhtrust(fund_id: str):
    # 當天的資料可能還沒公佈(dDate 會是 null),往回找最近一個有資料的日期
    today = datetime.now(TAIPEI).date()
    result = None
    for days_back in range(7):
        q_date = (today - timedelta(days=days_back)).strftime("%Y/%m/%d")
        url = f"https://www.fhtrust.com.tw/api/assets?fundID={fund_id}&qDate={q_date}"
        body = curl(["-A", USER_AGENT, url])
        candidate = json.loads(body.decode("utf-8"))["result"][0]
        if candidate.get("dDate"):
            result = candidate
            break

    if result is None:
        raise RuntimeError(f"fhtrust: no recent data found for fundID={fund_id}")

    as_of = result["dDate"].replace("/", "-")
    holdings = []
    for item in result["detail"]:
        if item["ftype"] != "股票":
            continue
        holdings.append({
            "stockCode": item["stockid"],
            "stockName": item["stockname"],
            "shares": float(item["qshare"].replace(",", "")),
            "weight": float(item["prate_addaccint"].rstrip("%")),
        })

    if not holdings:
        raise RuntimeError(f"fhtrust: no holdings parsed for fundID={fund_id}")
    return holdings, as_of


def fetch_capital(fund_id: int):
    body_json = json.dumps({"fundId": fund_id})
    resp = curl([
        "-X", "POST", "-H", "Content-Type: application/json",
        "-A", USER_AGENT, "-d", body_json,
        "https://www.capitalfund.com.tw/CFWeb/api/etf/buyback",
    ])
    data = json.loads(resp.decode("utf-8"))["data"]
    as_of = data["pcf"]["date2"]

    holdings = []
    for item in data["stocks"]:
        holdings.append({
            "stockCode": item["stocNo"],
            "stockName": item["stocName"],
            "shares": float(item["share"]),
            "weight": float(item["weight"]),
        })

    if not holdings:
        raise RuntimeError(f"capital: no holdings parsed for fundId={fund_id}")
    return holdings, as_of


FETCHERS = {
    "ezmoney": fetch_ezmoney,
    "fhtrust": fetch_fhtrust,
    "capital": fetch_capital,
}


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


def snapshot_path(code: str) -> Path:
    return ROOT / "data" / "snapshots" / f"{code}.json"


def load_snapshot(code: str):
    path = snapshot_path(code)
    if not path.exists():
        return None
    return json.loads(path.read_text(encoding="utf-8"))


def save_snapshot(code: str, as_of: str, holdings: list):
    path = snapshot_path(code)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"asOfDate": as_of, "holdings": holdings}, ensure_ascii=False, indent=2), encoding="utf-8")


def fetch_etf(cfg: dict) -> dict:
    current_holdings, as_of = FETCHERS[cfg["source"]](cfg["param"])
    snapshot = load_snapshot(cfg["code"])

    if snapshot is None:
        # 第一次執行,沒有比較基準,顯示為無變動而非全部標成新增
        previous_holdings = current_holdings
    else:
        previous_holdings = snapshot["holdings"]

    holdings_diff = diff_holdings(current_holdings, previous_holdings)

    if snapshot is None or as_of > snapshot["asOfDate"]:
        save_snapshot(cfg["code"], as_of, current_holdings)

    return {
        "code": cfg["code"],
        "name": cfg["name"],
        "asOfDate": as_of,
        "stale": False,
        "holdings": holdings_diff,
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
    for i, cfg in enumerate(ETF_CONFIGS):
        if i > 0:
            time.sleep(3)
        try:
            results.append(fetch_etf(cfg))
        except Exception as e:
            print(f"WARN: failed to fetch {cfg['code']}, will reuse previous data if available: {e}")
            fallback = previous_etfs.get(cfg["code"])
            if fallback:
                results.append(dict(fallback, stale=True))
            else:
                print(f"WARN: no previous data for {cfg['code']} either, skipping it for this run")

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
