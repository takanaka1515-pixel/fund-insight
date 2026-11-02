#!/usr/bin/env python3
"""Update PUBLIC mutual-fund NAV quotes without brokerage credentials.

The repository must contain prices.json. Fetch only public, verified values;
never publish invented values or include private portfolio/account information.
"""
from __future__ import annotations

import datetime as dt
from html.parser import HTMLParser
import json
import pathlib
import re
import sys
import unicodedata
import urllib.error
import urllib.request

# Works from either repo-root/fetch_prices.py or repo-root/scripts/fetch_prices.py.
HERE = pathlib.Path(__file__).resolve().parent
ROOT = HERE.parent if HERE.name == "scripts" else HERE
OUT = ROOT / "prices.json"

FUNDS = {
    "sp500": {
        "fund_cd": "253266", "association_cd": "03311187",
        "required_name": ("emaxis", "slim", "米国株式", "s&p500"),
        "name": "eMAXIS Slim 米国株式（S&P500）",
    },
    "allcountry": {
        "fund_cd": "253425", "association_cd": "0331418A",
        "required_name": ("emaxis", "slim", "全世界株式", "オール", "カントリー"),
        "name": "eMAXIS Slim 全世界株式（オール・カントリー）",
    },
}

OFFICIAL = "https://developer.am.mufg.jp/fund_information_latest/fund_cd/{}"
FALLBACK = "https://site0.sbisec.co.jp/marble/fund/history/standardprice.do?fund_sec_code={}"
HEADERS = {
    "User-Agent": "Mozilla/5.0 (compatible; fund-insight-personal/0.14)",
    "Accept": "application/json,text/html;q=0.9,*/*;q=0.8",
    "Accept-Language": "ja,en;q=0.7",
}


def clean_name(value: str) -> str:
    return re.sub(r"\s+", "", unicodedata.normalize("NFKC", str(value))).lower()


def check_name(fid: str, value: str) -> None:
    name = clean_name(value)
    if not all(clean_name(term) in name for term in FUNDS[fid]["required_name"]):
        raise ValueError("ファンド名を検証できません: " + repr(value[:140]))


def check_day(day: str) -> dt.date:
    raw = day.replace("/", "").replace("-", "")
    if not re.fullmatch(r"20\d{6}", raw):
        raise ValueError("不正な基準日")
    result = dt.datetime.strptime(raw, "%Y%m%d").date()
    today = dt.datetime.now(dt.timezone(dt.timedelta(hours=9))).date()
    if not dt.date(2020, 1, 1) <= result <= today:
        raise ValueError("基準日が範囲外")
    if (today - result).days > 14:
        raise ValueError("基準日が古い: " + result.isoformat())
    return result


def make_quote(fid: str, date: str, nav: int, source: str, url: str) -> dict:
    day = check_day(date)
    if not 1000 <= nav <= 500_000:
        raise ValueError("価格が異常値: " + repr(nav))
    return {
        "fundId": fid, "date": day.isoformat(), "price": nav,
        "unit": "per10000", "source": source, "sourceUrl": url,
        "name": FUNDS[fid]["name"],
    }


def fetch_text(url: str) -> str:
    req = urllib.request.Request(url, headers=HEADERS)
    with urllib.request.urlopen(req, timeout=20) as resp:
        if resp.status != 200:
            raise ValueError("HTTP " + str(resp.status))
        raw = resp.read(2_000_001)
        if len(raw) > 2_000_000:
            raise ValueError("レスポンスが大きすぎます")
        charset = resp.headers.get_content_charset()
    # Avoid blindly accepting Latin-1: it decodes corrupted Japanese text too.
    encodings = ["utf-8-sig", "cp932", charset]
    for enc in encodings:
        if not enc or enc.lower() in ("iso-8859-1", "latin-1"):
            continue
        try:
            text = raw.decode(enc)
            if "datasets" in text or "基準価額" in text:
                return text
        except (UnicodeError, LookupError):
            continue
    raise ValueError("日本語の公開価格データを正しく解読できません")


def from_official(fid: str) -> dict:
    cfg = FUNDS[fid]
    url = OFFICIAL.format(cfg["fund_cd"])
    response = json.loads(fetch_text(url))
    if not isinstance(response, dict) or not isinstance(response.get("datasets"), list):
        raise ValueError("公式APIの応答形式が不正")
    items = response["datasets"]
    if len(items) != 1 or not isinstance(items[0], dict):
        raise ValueError("公式APIで銘柄を1件に特定できません")
    record = items[0]
    check_name(fid, str(record.get("fund_name", "")))
    if record.get("fund_cd") is not None and str(record["fund_cd"]).strip() != cfg["fund_cd"]:
        raise ValueError("公式APIのファンドコードが一致しません")
    nav = int(str(record.get("nav", "")).replace(",", ""))
    return make_quote(fid, str(record.get("base_date", "")), nav,
                      "三菱UFJアセットマネジメント公式API", url)


class VisibleText(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.values: list[str] = []
        self.skip = 0

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag in ("script", "style"):
            self.skip += 1

    def handle_endtag(self, tag: str) -> None:
        if tag in ("script", "style") and self.skip:
            self.skip -= 1

    def handle_data(self, data: str) -> None:
        if not self.skip:
            self.values.append(data.strip())


def from_sbi_page(fid: str) -> dict:
    cfg = FUNDS[fid]
    url = FALLBACK.format(cfg["association_cd"])
    content = fetch_text(url)
    parser = VisibleText()
    parser.feed(content)
    readable = "\n".join(part for part in parser.values if part)
    # Validate fund identity before using numbers from the page.
    check_name(fid, readable)
    if "基準価額" not in readable or "年月日" not in readable:
        raise ValueError("SBIの基準価額一覧ではありません")
    # Daily quotes on the list are displayed as YYYY/MM/DD followed by NAV in yen.
    matches = re.findall(
        r"(?<!\d)(20\d{2}/\d{2}/\d{2})\s+([\d,]{4,9})\s*円",
        readable,
    )
    if len(matches) < 3:
        raise ValueError("基準価額の履歴を3件以上確認できません")
    values: dict[str, set[int]] = {}
    for day, nav_str in matches:
        values.setdefault(day, set()).add(int(nav_str.replace(",", "")))
    if any(len(items) != 1 for items in values.values()):
        raise ValueError("同じ日付に異なる価格があります")
    latest = max(values, key=lambda s: s.replace("/", ""))
    return make_quote(fid, latest, next(iter(values[latest])),
                      "SBI証券・公開基準価額一覧（代替）", url)


def fetch_quote(fid: str) -> dict:
    errors = []
    for method in (from_official, from_sbi_page):
        try:
            return method(fid)
        except Exception as exc:
            errors.append(f"{method.__name__}: {type(exc).__name__}: {exc}")
    raise ValueError(" / ".join(errors))


def main() -> int:
    previous: dict[str, dict] = {}
    if OUT.exists():
        try:
            raw = json.loads(OUT.read_text(encoding="utf-8"))
            for quote in raw.get("quotes", []):
                if (isinstance(quote, dict) and quote.get("fundId") in FUNDS
                        and isinstance(quote.get("price"), int)
                        and isinstance(quote.get("date"), str)):
                    previous[quote["fundId"]] = quote
        except (OSError, ValueError, TypeError, AttributeError) as exc:
            print("ERROR: 既存のprices.jsonを読み取れません:", exc, file=sys.stderr)
            return 1  # Never destroy a malformed original file.
    else:
        print("WARNING: 既存のprices.jsonがありません", file=sys.stderr)

    quotes = previous.copy()
    changed = 0
    for fid in FUNDS:
        try:
            quote = fetch_quote(fid)
            before = previous.get(fid)
            if before and before["date"] > quote["date"]:
                print(fid, "新しい既存価格を維持", before["date"])
                continue
            if before and before["date"] == quote["date"]:
                if before["price"] != quote["price"]:
                    raise ValueError("同じ基準日なのに価格が異なります")
                print(fid, "確認済み・変更なし", quote["date"])
                continue
            # A large change over a few days is more likely to be a parsing error.
            if before:
                gap = (dt.date.fromisoformat(quote["date"]) - dt.date.fromisoformat(before["date"])).days
                if gap <= 10 and not 0.8 <= quote["price"] / before["price"] <= 1.2:
                    raise ValueError("価格変化が20%を超え、更新を保留します")
            quotes[fid] = quote
            changed += 1
            print(fid, "更新", quote["date"], quote["price"], quote["source"])
        except Exception as exc:
            print(fid, "取得不可・既存値を維持:", exc, file=sys.stderr)

    if not quotes:
        print("ERROR: 有効な公開価格がないため更新を中止", file=sys.stderr)
        return 1
    if changed:
        doc = {
            "schemaVersion": 1,
            "generatedAt": dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds"),
            "quotes": [quotes[fid] for fid in FUNDS if fid in quotes],
        }
        OUT.write_text(json.dumps(doc, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(f"公開価格: {len(quotes)}件 / 新規更新: {changed}件 / 自動更新対象: 2銘柄")
    if changed == 0:
        print("NOTICE: 基準価額は更新されていません。公開元の接続状況を確認してください")
    return 0


if __name__ == "__main__":
    sys.exit(main())
