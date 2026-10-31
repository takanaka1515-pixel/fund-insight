#!/usr/bin/env python3
"""Update *public* NAV quotes; no brokerage login or personal holdings are accessed.

Only the asset manager's documented public JSON API is used. If an API call
fails or data is inconsistent, the previous quote is retained and logged.
"""
from __future__ import annotations
import datetime as dt
import json
import pathlib
import re
import sys
import unicodedata
import urllib.error
import urllib.request

ROOT = pathlib.Path(__file__).resolve().parents[1]
OUT = ROOT / "prices.json"
FUNDS = {
    "sp500": {"code": "03311187", "expected": ("eMAXIS", "Slim", "米国株式")},
    "allcountry": {"code": "0331418A", "expected": ("eMAXIS", "Slim", "全世界株式")},
}
API = "https://developer.am.mufg.jp/fund_information_latest/association_fund_cd/{}"


def clean_name(text):
    return re.sub(r"\s+", "", unicodedata.normalize("NFKC", str(text))).lower()


def parse_quote(fid, response):
    if not isinstance(response, dict) or not isinstance(response.get("datasets"), list):
        raise ValueError("API形式が異なります")
    items = response["datasets"]
    if len(items) != 1 or not isinstance(items[0], dict):
        raise ValueError("対象ファンドが1件に特定できません")
    data = items[0]
    name = clean_name(data.get("fund_name", ""))
    if not all(clean_name(term) in name for term in FUNDS[fid]["expected"]):
        raise ValueError("ファンド名が一致しません: " + repr(data.get("fund_name")))
    code = data.get("association_fund_cd")
    if code and str(code).strip() != FUNDS[fid]["code"]:
        raise ValueError("協会コードが一致しません")
    raw_day = str(data.get("base_date", "")).replace("/", "").replace("-", "")
    if not re.fullmatch(r"20\d{6}", raw_day):
        raise ValueError("基準日の形式が不正")
    day = dt.datetime.strptime(raw_day, "%Y%m%d").date()
    if day > dt.date.today() + dt.timedelta(days=1) or day < dt.date(2020, 1, 1):
        raise ValueError("基準日の範囲が不正")
    nav = int(str(data.get("nav", "")).replace(",", ""))
    if not 100 <= nav <= 9_999_999:
        raise ValueError("基準価額が異常値")
    return {
        "fundId": fid, "date": day.isoformat(), "price": nav,
        "unit": "per10000", "source": "三菱UFJアセットマネジメント公式API",
        "sourceUrl": API.format(FUNDS[fid]["code"]),
        "name": str(data["fund_name"]),
    }


def main():
    previous = {}
    if OUT.exists():
        try:
            doc = json.loads(OUT.read_text(encoding="utf-8"))
            for q in doc.get("quotes", []):
                if q.get("fundId") in FUNDS and isinstance(q.get("price"), (int, float)):
                    previous[q["fundId"]] = q
        except (ValueError, OSError, AttributeError, TypeError) as exc:
            print("既存ファイルは読み取れません:", exc, file=sys.stderr)
    changes = 0
    failures = []
    for fid, cfg in FUNDS.items():
        url = API.format(cfg["code"])
        try:
            request = urllib.request.Request(url, headers={
                "Accept": "application/json", "User-Agent": "fund-insight-personal/0.13"
            })
            with urllib.request.urlopen(request, timeout=25) as res:
                if res.status != 200:
                    raise ValueError("HTTP " + str(res.status))
                if not res.headers.get("Content-Type", "").lower().startswith("application/json"):
                    raise ValueError("JSON以外のレスポンス")
                data = json.loads(res.read(2_000_000).decode("utf-8"))
            quote = parse_quote(fid, data)
            before = previous.get(fid)
            if before and before.get("date", "") > quote["date"]:
                print(fid, "新しい既存価格を保持", before["date"])
                continue
            if quote != before:
                changes += 1
            previous[fid] = quote
            print(fid, quote["date"], quote["price"])
        except Exception as exc:
            failures.append(f"{fid}: {type(exc).__name__}: {exc}")
    if not previous:
        raise SystemExit("価格データなし。公開用ファイルを上書きしません。\n" + "\n".join(failures))
    new_doc = {
        "schemaVersion": 1,
        "generatedAt": dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds"),
        "quotes": [previous[fid] for fid in FUNDS if fid in previous],
    }
    # Do not leak any personal holdings, amounts, account IDs or private data.
    if changes or not OUT.exists():
        OUT.write_text(json.dumps(new_doc, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(f"有効価格 {len(previous)}銘柄、更新 {changes}銘柄")
    if failures:
        print("取得失敗 (前回の価額を維持):", *failures, sep="\n", file=sys.stderr)


if __name__ == "__main__":
    main()
