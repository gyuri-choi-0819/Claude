#!/usr/bin/env python3
"""
네이버부동산 시세 트래커 - 로컬 프록시 서버 (표준 라이브러리만 사용)

브라우저는 CORS/인증 토큰 문제로 네이버부동산 API를 직접 호출할 수 없기 때문에,
이 서버가 index.html 을 띄워주고 네이버 API 호출을 대신 처리합니다.

실행:
    python3 server.py              # 실제 네이버부동산 데이터
    python3 server.py --demo       # 가짜 데이터로 화면만 확인
    MOLIT_API_KEY=발급키 python3 server.py   # (선택) 국토부 실거래가 API 병행

그 다음 브라우저에서 http://localhost:8000 접속
"""
import argparse
import json
import math
import os
import random
import re
import statistics
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from datetime import date, datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

BASE = "https://new.land.naver.com"
UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/130.0.0.0 Safari/537.36")
HERE = Path(__file__).resolve().parent

# 화면의 유형 선택값 -> 네이버 realEstateType 코드
TYPE_CODES = {
    "APT": "APT:ABYG:JGC",   # 아파트 (+분양권, 재건축)
    "OPST": "OPST",          # 오피스텔
    "VL": "VL:YR:DSD",       # 빌라/연립/다세대
}
TRADE_CODES = {"A1": "매매", "B1": "전세"}
PYEONG = 3.3058


# ---------------------------------------------------------------- 유틸
def parse_price(text):
    """'12억 5,000' -> 125000 (만원 단위)"""
    if text is None:
        return None
    if isinstance(text, (int, float)):
        return int(text)
    s = str(text).replace(",", "").replace(" ", "")
    if not s:
        return None
    total = 0
    if "억" in s:
        eok, _, rest = s.partition("억")
        total += int(float(eok or 0) * 10000)
        s = rest
    s = re.sub(r"[^0-9.]", "", s)
    if s:
        total += int(float(s))
    return total or None


def per_pyeong(price_manwon, area_m2):
    try:
        if price_manwon and area_m2 and float(area_m2) > 0:
            return price_manwon / (float(area_m2) / PYEONG)
    except (TypeError, ValueError):
        pass
    return None


def pct(new, old):
    if new and old:
        return round((new - old) / old * 100, 2)
    return None


def haversine_km(lat1, lon1, lat2, lon2):
    r = 6371.0
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dp, dl = p2 - p1, math.radians(lon2 - lon1)
    a = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return 2 * r * math.asin(math.sqrt(a))


def months_back(n):
    """오늘 기준 n개월 전까지의 YYYYMM 리스트 (최근 -> 과거)"""
    y, m = date.today().year, date.today().month
    out = []
    for _ in range(n):
        out.append(f"{y}{m:02d}")
        m -= 1
        if m == 0:
            y, m = y - 1, 12
    return out


class TTLCache:
    def __init__(self, ttl):
        self.ttl, self.data, self.lock = ttl, {}, threading.Lock()

    def get(self, key):
        with self.lock:
            v = self.data.get(key)
            if v and time.time() - v[0] < self.ttl:
                return v[1]
        return None

    def set(self, key, value):
        with self.lock:
            self.data[key] = (time.time(), value)


# ---------------------------------------------------------------- 네이버 클라이언트
class NaverLand:
    def __init__(self, delay=0.35):
        self.token = None
        self.token_time = 0
        self.delay = delay
        self.lock = threading.Lock()
        self.cache = TTLCache(300)  # 같은 요청은 5분 캐시

    def _raw(self, url, headers=None):
        req = urllib.request.Request(url, headers={
            "User-Agent": UA,
            "Referer": BASE + "/",
            "Accept": "*/*",
            "Accept-Language": "ko-KR,ko;q=0.9",
            **(headers or {}),
        })
        with urllib.request.urlopen(req, timeout=15) as r:
            return r.read().decode("utf-8", "replace")

    def _ensure_token(self):
        if self.token and time.time() - self.token_time < 60 * 60 * 2:
            return
        html = self._raw(BASE + "/complexes")
        m = re.search(r'["\']?token["\']?\s*[:=]\s*["\'](eyJ[\w-]+\.[\w-]+\.[\w-]+)', html) \
            or re.search(r"(eyJ[\w-]+\.[\w-]+\.[\w-]+)", html)
        if not m:
            raise RuntimeError("네이버부동산 인증 토큰을 찾지 못했습니다 (페이지 구조 변경 가능성).")
        self.token, self.token_time = m.group(1), time.time()

    def get(self, path, params=None):
        url = BASE + path + ("?" + urllib.parse.urlencode(params) if params else "")
        cached = self.cache.get(url)
        if cached is not None:
            return cached
        with self.lock:  # 네이버 차단(429) 방지를 위해 순차 + 딜레이
            for attempt in range(3):
                self._ensure_token()
                try:
                    body = self._raw(url, {"Authorization": "Bearer " + self.token})
                    time.sleep(self.delay)
                    data = json.loads(body)
                    self.cache.set(url, data)
                    return data
                except urllib.error.HTTPError as e:
                    if e.code == 401:
                        self.token = None
                    elif e.code == 429:
                        time.sleep(2 + attempt * 3)
                    else:
                        raise
            raise RuntimeError("네이버부동산 요청이 반복 실패했습니다 (요청 과다 차단일 수 있음). 잠시 후 다시 시도하세요.")

    # --- API 래퍼
    def search(self, keyword):
        return self.get("/api/search", {"keyword": keyword})

    def cortar_by_coord(self, lat, lon):
        return self.get("/api/cortars", {"zoom": 16, "centerLat": lat, "centerLon": lon})

    def region_list(self, cortar_no):
        return self.get("/api/regions/list", {"cortarNo": cortar_no}).get("regionList", [])

    def complexes(self, cortar_no, rtype):
        d = self.get("/api/regions/complexes",
                     {"cortarNo": cortar_no, "realEstateType": rtype, "order": ""})
        return d.get("complexList", []) or []

    def complex_overview(self, complex_no):
        return self.get(f"/api/complexes/overview/{complex_no}", {"complexNo": complex_no})

    def complex_articles(self, complex_no, rtype, trade, pages=2):
        out = []
        for page in range(1, pages + 1):
            d = self.get(f"/api/articles/complex/{complex_no}", {
                "realEstateType": rtype, "tradeType": trade, "tag": "::::::::",
                "rentPriceMin": 0, "rentPriceMax": 900000000, "priceMin": 0,
                "priceMax": 900000000, "areaMin": 0, "areaMax": 900000000,
                "showArticle": "false", "sameAddressGroup": "true",
                "priceType": "RETAIL", "page": page, "complexNo": complex_no,
                "type": "list", "order": "prc",
            })
            out += d.get("articleList", []) or []
            if not d.get("isMoreData"):
                break
        return out

    def region_articles(self, cortar_no, rtype, trade, pages=3):
        out = []
        for page in range(1, pages + 1):
            d = self.get("/api/articles", {
                "cortarNo": cortar_no, "order": "rank", "realEstateType": rtype,
                "tradeType": trade, "tag": "::::::::", "rentPriceMin": 0,
                "rentPriceMax": 900000000, "priceMin": 0, "priceMax": 900000000,
                "areaMin": 0, "areaMax": 900000000, "showArticle": "false",
                "sameAddressGroup": "false", "priceType": "RETAIL", "page": page,
            })
            out += d.get("articleList", []) or []
            if not d.get("isMoreData"):
                break
        return out

    def real_prices(self, complex_no, trade, area_no):
        d = self.get(f"/api/complexes/{complex_no}/prices/real", {
            "complexNo": complex_no, "tradeType": trade, "year": 5,
            "priceChartChange": "false", "areaNo": area_no, "type": "table",
        })
        rows = []
        for month in d.get("realPriceOnMonthList", []) or []:
            for p in month.get("realPriceList", []) or []:
                price = parse_price(p.get("dealPrice") or p.get("formattedPrice"))
                y, mo = p.get("tradeYear"), p.get("tradeMonth")
                if not (price and y and mo):
                    continue
                rows.append({
                    "ym": f"{int(y)}{int(mo):02d}",
                    "day": int(p.get("tradeDate") or 1),
                    "price": price,
                    "floor": p.get("floor"),
                    "area": p.get("exclusiveArea") or p.get("representativeArea"),
                    "canceled": bool(p.get("deleteYn") == "Y"),
                })
        return [r for r in rows if not r["canceled"]]


# ---------------------------------------------------------------- 국토부 실거래 (선택)
MOLIT_EP = {
    "APT": ("RTMSDataSvcAptTrade/getRTMSDataSvcAptTrade", "aptNm"),
    "OPST": ("RTMSDataSvcOffiTrade/getRTMSDataSvcOffiTrade", "offiNm"),
    "VL": ("RTMSDataSvcRHTrade/getRTMSDataSvcRHTrade", "mhouseNm"),
}


def molit_trades(key, lawd_cd, ym, kind):
    path, name_field = MOLIT_EP[kind]
    url = (f"https://apis.data.go.kr/1613000/{path}?serviceKey={key}"
           f"&LAWD_CD={lawd_cd}&DEAL_YMD={ym}&numOfRows=2000&pageNo=1")
    req = urllib.request.Request(url, headers={"User-Agent": UA})
    with urllib.request.urlopen(req, timeout=20) as r:
        root = ET.fromstring(r.read())
    out = []
    for it in root.iter("item"):
        g = lambda k: (it.findtext(k) or "").strip()
        if g("cdealType") == "O":  # 해제된 거래 제외
            continue
        price = parse_price(g("dealAmount"))
        if not price:
            continue
        out.append({
            "name": g(name_field), "dong": g("umdNm"), "ym": ym,
            "day": int(g("dealDay") or 1), "price": price,
            "area": float(g("excluUseAr") or g("totalFloorAr") or 0) or None,
            "floor": g("floor"),
        })
    return out


# ---------------------------------------------------------------- 리포트 생성
def summarize_trades(trades):
    """월별 평당가 추이 + 최근 3개월 vs 직전 3개월 변동률"""
    by_month = {}
    for t in trades:
        pp = per_pyeong(t["price"], t.get("area"))
        if pp:
            by_month.setdefault(t["ym"], []).append(pp)
    months = sorted(by_month)
    series = [{"ym": m, "avgPerPyeong": round(statistics.median(by_month[m])),
               "count": len(by_month[m])} for m in months]
    recent = months_back(3)
    prev = months_back(6)[3:]
    yago = months_back(15)[12:]
    def med(ms):
        vals = [v for m in ms for v in by_month.get(m, [])]
        return statistics.median(vals) if vals else None
    r, p, y = med(recent), med(prev), med(yago)
    return series, {
        "recent3m": round(r) if r else None,
        "prev3m": round(p) if p else None,
        "yearAgo3m": round(y) if y else None,
        "change3m": pct(r, p),
        "changeYoY": pct(r, y),
        "tradeCount3m": sum(len(by_month.get(m, [])) for m in recent),
    }


def build_report(nv, query, kind, trade, radius_km, limit, molit_key, log):
    rtype = TYPE_CODES[kind]
    s = nv.search(query)
    regions = s.get("regions") or []
    cpx = s.get("complexes") or []
    if regions:
        center = {"name": regions[0].get("cortarName"), "cortarNo": regions[0]["cortarNo"],
                  "lat": float(regions[0]["centerLat"]), "lon": float(regions[0]["centerLon"])}
    elif cpx:
        c = cpx[0]
        center = {"name": f"{query} ({c.get('complexName')} 인근)", "cortarNo": c.get("cortarNo"),
                  "lat": float(c["latitude"]), "lon": float(c["longitude"])}
    else:
        raise RuntimeError(f"'{query}' 검색 결과가 없습니다. 동 이름(예: 역삼동)이나 단지명으로 다시 시도해 보세요.")

    if not center.get("cortarNo"):
        cc = nv.cortar_by_coord(center["lat"], center["lon"])
        center["cortarNo"] = (cc.get("cortarVO") or cc).get("cortarNo")
    log(f"기준 위치: {center['name']} ({center['cortarNo']})")

    # 주변 동 찾기: 같은 구의 동 중 반경 내
    gu_no = str(center["cortarNo"])[:5] + "00000"
    dongs = []
    for d in nv.region_list(gu_no):
        try:
            dist = haversine_km(center["lat"], center["lon"], float(d["centerLat"]), float(d["centerLon"]))
        except (KeyError, TypeError, ValueError):
            continue
        if dist <= radius_km or d["cortarNo"] == center["cortarNo"]:
            dongs.append({"cortarNo": d["cortarNo"], "name": d["cortarName"], "dist": round(dist, 2)})
    if not any(d["cortarNo"] == center["cortarNo"] for d in dongs):
        dongs.insert(0, {"cortarNo": center["cortarNo"], "name": center["name"], "dist": 0})
    dongs.sort(key=lambda d: d["dist"])
    dongs = dongs[:8]
    log("대상 동: " + ", ".join(d["name"] for d in dongs))

    complexes_out, listings_out, all_trades = [], [], []

    if kind in ("APT", "OPST"):
        cands = []
        for d in dongs:
            for c in nv.complexes(d["cortarNo"], kind if kind == "OPST" else "APT:ABYG:JGC"):
                c["_dong"] = d["name"]
                try:
                    c["_dist"] = haversine_km(center["lat"], center["lon"],
                                              float(c["latitude"]), float(c["longitude"]))
                except (KeyError, TypeError, ValueError):
                    c["_dist"] = 99
                if c["_dist"] <= radius_km + 0.3:
                    cands.append(c)
        # 매물 많은 + 큰 단지 우선
        cands.sort(key=lambda c: -((c.get("dealCount") or 0) * 3 + (c.get("totalHouseholdCount") or 0) / 100))
        cands = cands[:limit]
        log(f"분석 단지 {len(cands)}개")

        for i, c in enumerate(cands, 1):
            no = c["complexNo"]
            log(f"[{i}/{len(cands)}] {c.get('complexName')}")
            arts = nv.complex_articles(no, rtype, trade)
            asks = []
            for a in arts:
                p = parse_price(a.get("dealOrWarrantPrc"))
                area = a.get("area2") or a.get("area1")
                asks.append(p)
                listings_out.append({
                    "complex": c.get("complexName"), "dong": c["_dong"], "price": p,
                    "priceText": a.get("dealOrWarrantPrc"), "area": area,
                    "perPyeong": round(per_pyeong(p, area) or 0) or None,
                    "floor": a.get("floorInfo"), "direction": a.get("direction"),
                    "confirmed": a.get("articleConfirmYmd"), "articleNo": a.get("articleNo"),
                    "realtor": a.get("realtorName"), "feature": a.get("articleFeatureDesc"),
                })
            ask_pp = [per_pyeong(parse_price(a.get("dealOrWarrantPrc")), a.get("area2") or a.get("area1"))
                      for a in arts]
            ask_pp = [x for x in ask_pp if x]

            # 실거래: 국토부 키가 없으면 네이버 단지별 실거래 (매물이 가장 많은 평형 기준)
            trades = []
            if not (molit_key and trade == "A1"):
                try:
                    ov = nv.complex_overview(no)
                    pyeongs = ov.get("pyeongs") or []
                    if pyeongs:
                        cnt = {}
                        for a in arts:
                            cnt[str(a.get("area2"))] = cnt.get(str(a.get("area2")), 0) + 1
                        pyeongs.sort(key=lambda p: -cnt.get(str(p.get("exclusiveArea")), 0))
                        for py in pyeongs[:2]:
                            for t in nv.real_prices(no, trade, py.get("pyeongNo")):
                                t["area"] = t.get("area") or py.get("exclusiveArea")
                                trades.append(t)
                except Exception as e:  # 실거래 실패해도 호가는 보여준다
                    log(f"  실거래 조회 실패: {e}")
            for t in trades:
                t["name"] = c.get("complexName")
                t["dong"] = c["_dong"]
            all_trades += trades
            complexes_out.append(make_complex_row(c, asks, ask_pp, trades))
    else:  # 빌라: 단지 개념이 없어서 동 단위 매물
        for d in dongs:
            log(f"{d['name']} 빌라 매물 조회")
            for a in nv.region_articles(d["cortarNo"], rtype, trade):
                p = parse_price(a.get("dealOrWarrantPrc"))
                area = a.get("area2") or a.get("area1")
                listings_out.append({
                    "complex": a.get("articleName") or a.get("buildingName"), "dong": d["name"],
                    "price": p, "priceText": a.get("dealOrWarrantPrc"), "area": area,
                    "perPyeong": round(per_pyeong(p, area) or 0) or None,
                    "floor": a.get("floorInfo"), "direction": a.get("direction"),
                    "confirmed": a.get("articleConfirmYmd"), "articleNo": a.get("articleNo"),
                    "realtor": a.get("realtorName"), "feature": a.get("articleFeatureDesc"),
                })

    # 국토부 실거래 (선택, 매매만)
    trade_source = "네이버부동산 단지 실거래"
    if molit_key and trade == "A1":
        trade_source = "국토교통부 실거래가 공개시스템"
        lawd = str(center["cortarNo"])[:5]
        dong_names = {d["name"] for d in dongs}
        cpx_names = {c["name"] for c in complexes_out}
        for ym in months_back(15):
            log(f"국토부 실거래 {ym} 조회")
            try:
                for t in molit_trades(molit_key, lawd, ym, kind):
                    if t["dong"] in dong_names:
                        all_trades.append(t)
            except Exception as e:
                log(f"  국토부 조회 실패: {e}")
        # 단지별 행에 국토부 실거래 반영
        for row in complexes_out:
            ts = [t for t in all_trades if t["name"] and (t["name"] in row["name"] or row["name"] in t["name"])]
            row.update(trade_stats(ts))
        if kind == "VL":
            complexes_out = []
    elif kind == "VL":
        trade_source = "없음 (빌라 실거래는 MOLIT_API_KEY 설정 시 제공)"

    series, change = summarize_trades(all_trades)
    ask_vals = [l["perPyeong"] for l in listings_out if l["perPyeong"]]
    ask_med = round(statistics.median(ask_vals)) if ask_vals else None
    listings_out.sort(key=lambda l: (l["perPyeong"] or 0), reverse=True)
    recent_trades = sorted(all_trades, key=lambda t: (t["ym"], t.get("day", 0)), reverse=True)[:60]

    return {
        "query": query, "kind": kind, "trade": trade, "tradeName": TRADE_CODES[trade],
        "center": center, "dongs": dongs, "generatedAt": datetime.now().isoformat(timespec="seconds"),
        "tradeSource": trade_source,
        "summary": {
            "askPerPyeong": ask_med,
            "listingCount": len(listings_out),
            "complexCount": len(complexes_out),
            "askVsTrade": pct(ask_med, change["recent3m"]),
            **change,
        },
        "series": series,
        "complexes": complexes_out,
        "listings": listings_out[:300],
        "trades": recent_trades,
    }


def trade_stats(trades):
    trades = sorted(trades, key=lambda t: (t["ym"], t.get("day", 0)))
    if not trades:
        return {"lastTrade": None, "lastTradeYm": None, "lastTradeArea": None,
                "tradeChange": None, "tradePerPyeong": None}
    last = trades[-1]
    last_pp = per_pyeong(last["price"], last.get("area"))
    # 1년 전 (9~15개월 전) 거래 평당가 중앙값과 비교
    window = set(months_back(15)[9:])
    old = [per_pyeong(t["price"], t.get("area")) for t in trades if t["ym"] in window]
    old = [x for x in old if x]
    return {
        "lastTrade": last["price"], "lastTradeYm": last["ym"], "lastTradeArea": last.get("area"),
        "tradePerPyeong": round(last_pp) if last_pp else None,
        "tradeChange": pct(last_pp, statistics.median(old)) if old and last_pp else None,
    }


def make_complex_row(c, asks, ask_pp, trades):
    asks = [a for a in asks if a]
    row = {
        "name": c.get("complexName"), "complexNo": c.get("complexNo"), "dong": c.get("_dong"),
        "dist": round(c.get("_dist", 0), 2), "households": c.get("totalHouseholdCount"),
        "approve": (c.get("useApproveYmd") or "")[:4],
        "listingCount": len(asks),
        "askMin": min(asks) if asks else None,
        "askMax": max(asks) if asks else None,
        "askPerPyeong": round(statistics.median(ask_pp)) if ask_pp else None,
    }
    row.update(trade_stats(trades))
    row["gap"] = pct(row["askPerPyeong"], row["tradePerPyeong"])
    return row


# ---------------------------------------------------------------- 데모 데이터
def demo_report(query, kind, trade, radius_km, limit):
    rnd = random.Random(hash((query, kind, trade)) & 0xFFFF)
    base = {"APT": 5200, "OPST": 2600, "VL": 1900}[kind] * (0.6 if trade == "B1" else 1)
    trend = rnd.uniform(-0.6, 1.4)  # 월 변동 %
    dongs = [{"cortarNo": f"11680{i:05d}", "name": n, "dist": round(i * 0.6, 2)}
             for i, n in enumerate(["역삼동", "논현동", "삼성동", "대치동"][: 2 + int(radius_km)])]
    all_trades, complexes, listings = [], [], []
    names = ["래미안", "자이", "e편한세상", "힐스테이트", "푸르지오", "아이파크", "롯데캐슬", "더샵", "센트럴", "SK뷰"]
    n_cpx = 0 if kind == "VL" else min(limit, 10)
    months = list(reversed(months_back(24)))
    for i in range(n_cpx if n_cpx else 12):
        nm = (f"{dongs[i % len(dongs)]['name'][:2]}{names[i % len(names)]}"
              if kind != "VL" else f"{dongs[i % len(dongs)]['name'][:2]}빌라 {i + 1}동")
        lvl = base * rnd.uniform(0.75, 1.35)
        area = rnd.choice([59.9, 84.9, 114.2]) if kind == "APT" else rnd.choice([24.5, 33.1, 45.6])
        trades = []
        for k, ym in enumerate(months):
            for _ in range(rnd.randint(0, 2)):
                pp = lvl * (1 + trend / 100) ** k * rnd.uniform(0.95, 1.05)
                trades.append({"name": nm, "dong": dongs[i % len(dongs)]["name"], "ym": ym,
                               "day": rnd.randint(1, 28), "price": round(pp * area / PYEONG / 100) * 100,
                               "area": area, "floor": str(rnd.randint(1, 25))})
        all_trades += trades
        asks, ask_pp = [], []
        cur = lvl * (1 + trend / 100) ** len(months)
        for _ in range(rnd.randint(2, 9)):
            pp = cur * rnd.uniform(1.0, 1.12)
            p = round(pp * area / PYEONG / 500) * 500
            asks.append(p)
            ask_pp.append(per_pyeong(p, area))
            eok, man = divmod(p, 10000)
            listings.append({"complex": nm, "dong": dongs[i % len(dongs)]["name"], "price": p,
                             "priceText": (f"{eok}억 {man:,}" if man else f"{eok}억") if eok else f"{man:,}",
                             "area": area, "perPyeong": round(per_pyeong(p, area)),
                             "floor": f"{rnd.randint(1, 25)}/25", "direction": rnd.choice(["남향", "동향", "남동향"]),
                             "confirmed": date.today().strftime("%Y%m%d"), "articleNo": None,
                             "realtor": "데모공인중개사", "feature": "데모 데이터"})
        if kind != "VL":
            c = {"complexName": nm, "complexNo": str(100000 + i), "_dong": dongs[i % len(dongs)]["name"],
                 "_dist": rnd.uniform(0.1, radius_km), "totalHouseholdCount": rnd.randint(150, 3000),
                 "useApproveYmd": str(rnd.randint(1995, 2023))}
            complexes.append(make_complex_row(c, asks, ask_pp, trades))
    series, change = summarize_trades(all_trades)
    ask_vals = [l["perPyeong"] for l in listings]
    ask_med = round(statistics.median(ask_vals)) if ask_vals else None
    listings.sort(key=lambda l: l["perPyeong"] or 0, reverse=True)
    return {
        "query": query, "kind": kind, "trade": trade, "tradeName": TRADE_CODES[trade],
        "center": {"name": f"{query} (데모)", "cortarNo": "1168010100", "lat": 37.5, "lon": 127.03},
        "dongs": dongs, "generatedAt": datetime.now().isoformat(timespec="seconds"),
        "tradeSource": "데모 데이터 (실제 시세 아님)", "demo": True,
        "summary": {"askPerPyeong": ask_med, "listingCount": len(listings),
                    "complexCount": len(complexes), "askVsTrade": pct(ask_med, change["recent3m"]), **change},
        "series": series, "complexes": complexes, "listings": listings,
        "trades": sorted(all_trades, key=lambda t: (t["ym"], t["day"]), reverse=True)[:60],
    }


# ---------------------------------------------------------------- HTTP 서버
class Handler(BaseHTTPRequestHandler):
    nv = None
    demo = False
    molit_key = None
    report_cache = TTLCache(180)

    def log_message(self, fmt, *args):
        sys.stderr.write("  " + fmt % args + "\n")

    def send_json(self, obj, code=200):
        body = json.dumps(obj, ensure_ascii=False).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        u = urllib.parse.urlparse(self.path)
        q = {k: v[0] for k, v in urllib.parse.parse_qs(u.query).items()}
        if u.path in ("/", "/index.html"):
            body = (HERE / "index.html").read_bytes()
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return
        if u.path == "/api/report":
            query = (q.get("q") or "").strip()
            kind = q.get("type", "APT").upper()
            trade = q.get("trade", "A1").upper()
            if not query or kind not in TYPE_CODES or trade not in TRADE_CODES:
                return self.send_json({"error": "지역명/역 이름, 유형(APT/OPST/VL), 거래(A1/B1)를 확인하세요."}, 400)
            radius = min(max(float(q.get("radius", 1.5)), 0.3), 5)
            limit = min(max(int(q.get("limit", 12)), 1), 40)
            key = (query, kind, trade, radius, limit)
            if q.get("refresh") != "1":
                hit = Handler.report_cache.get(key)
                if hit:
                    return self.send_json(hit)
            try:
                logs = []
                log = lambda m: (logs.append(m), print("  " + m))
                if self.demo:
                    rep = demo_report(query, kind, trade, radius, limit)
                else:
                    rep = build_report(self.nv, query, kind, trade, radius, limit, self.molit_key, log)
                rep["log"] = logs
                Handler.report_cache.set(key, rep)
                return self.send_json(rep)
            except urllib.error.URLError as e:
                return self.send_json({"error": f"네이버부동산 접속 실패: {e}"}, 502)
            except Exception as e:
                return self.send_json({"error": str(e)}, 500)
        if u.path == "/api/status":
            return self.send_json({"demo": self.demo, "molit": bool(self.molit_key)})
        self.send_json({"error": "not found"}, 404)


def main():
    ap = argparse.ArgumentParser(description="네이버부동산 시세 트래커")
    ap.add_argument("--port", type=int, default=8000)
    ap.add_argument("--demo", action="store_true", help="가짜 데이터로 화면 확인")
    ap.add_argument("--molit-key", default=os.environ.get("MOLIT_API_KEY"),
                    help="공공데이터포털 국토부 실거래가 API 인증키 (선택)")
    a = ap.parse_args()
    Handler.nv = NaverLand()
    Handler.demo = a.demo
    Handler.molit_key = a.molit_key
    srv = ThreadingHTTPServer(("127.0.0.1", a.port), Handler)
    print(f"\n  ▶ 네이버부동산 시세 트래커: http://localhost:{a.port}"
          f"{'  [데모 모드]' if a.demo else ''}{'  [국토부 실거래 사용]' if a.molit_key else ''}\n")
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
