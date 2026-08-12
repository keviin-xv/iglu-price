#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Iglu 房态价格流水线
====================
架构：
  - 动态数据（价格/余量/状态/起租）：每天 3 次（10:00/13:00/17:00）→ prices.json
  - 静态数据（面积/床型）：每两周 1 次 → static_info.json
  - 复检机制：数据校验 + 数量异常回退 + 失败保留上次数据

用法：
  python3 iglu_pipeline.py --mode status   # 动态抓取（含校验+渲染）
  python3 iglu_pipeline.py --mode static   # 静态抓取（面积/床型）
  python3 iglu_pipeline.py --mode all      # 全量
"""
import argparse
import json
import re
import sys
import time
import urllib.request
import html as htmllib

# ═══════════════════ 配置 ═══════════════════

CITIES = {
    "悉尼 Sydney": {
        "Broadway": "https://iglu.com.au/properties/sydney/broadway/",
        "Central": "https://iglu.com.au/properties/sydney/central/",
        "Central Park": "https://iglu.com.au/properties/sydney/central-park/",
        "Chatswood": "https://iglu.com.au/properties/sydney/chatswood/",
        "Mascot": "https://iglu.com.au/properties/sydney/mascot/",
        "Mascot Duo": "https://iglu.com.au/properties/sydney/mascot-duo/",
        "Redfern": "https://iglu.com.au/properties/sydney/redfern/",
        "Summer Hill": "https://iglu.com.au/properties/sydney/summer-hill/",
        "Waterloo": "https://iglu.com.au/properties/sydney/waterloo/",
    },
    "布里斯班 Brisbane": {
        "Brisbane City": "https://iglu.com.au/properties/brisbane/brisbane-city/",
        "Kelvin Grove": "https://iglu.com.au/properties/brisbane/kelvin-grove/",
    },
    "墨尔本 Melbourne": {
        "Flagstaff Gardens": "https://iglu.com.au/properties/melbourne/flagstaff-gardens/",
        "Flagstaff Station": "https://iglu.com.au/properties/melbourne/flagstaff-station/",
        "Melbourne Central": "https://iglu.com.au/properties/melbourne/melbourne-central/",
        "Melbourne City": "https://iglu.com.au/properties/melbourne/melbourne-city/",
        "South Yarra": "https://iglu.com.au/properties/melbourne/south-yarra/",
    },
}

# 租期中文映射（动态识别用）
TERM_ZH = {"12 Months": "12个月", "22 Weeks": "22周", "44 Weeks": "44周", "Short Stay": "短租"}
TERM_ORDER = ["12个月", "22周", "44周", "短租"]

UA = "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36"
RETRIES = 3
REQUEST_GAP = 0.6  # 请求间隔（秒），避免触发反爬

# ═══════════════════ 基础工具 ═══════════════════


def clean(t):
    t = re.sub(r"<[^>]+>", "", t)
    t = htmllib.unescape(t)
    return re.sub(r"\s+", " ", t).strip()


def fetch(url, retries=RETRIES):
    """HTTP 抓取，失败自动重试"""
    last_err = None
    for attempt in range(retries):
        try:
            req = urllib.request.Request(url, headers={"User-Agent": UA})
            with urllib.request.urlopen(req, timeout=30) as resp:
                return resp.read().decode("utf-8", errors="ignore")
        except Exception as e:
            last_err = e
            time.sleep(2 * (attempt + 1))
    raise last_err


def load_json(path, default):
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return default


def save_json(path, data):
    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)


def zh_term(raw):
    """租期英文 → 中文（新租期智能转换）"""
    if raw in TERM_ZH:
        return TERM_ZH[raw]
    m = re.match(r"(\d+)\s*Months?", raw, re.I)
    if m:
        return f"{m.group(1)}个月"
    m = re.match(r"(\d+)\s*Weeks?", raw, re.I)
    if m:
        return f"{m.group(1)}周"
    return raw


# ═══════════════════ 解析：公寓页（动态数据） ═══════════════════


def parse_property_page(name, url):
    """公寓页：房型列表 + From价 + 余量 + 状态 + 起租日期 + 房型链接"""
    page = fetch(url)
    cards = re.split(r'<div id="room-\d+', page)[1:]
    rooms = []
    for card in cards:
        m = re.search(r'<h3[^>]*>\s*<a[^>]*href="([^"]*)"[^>]*>([^<]+)</a>', card)
        if not m:
            continue
        room_url, raw_name = m.group(1), clean(m.group(2))

        # 分组
        rtype = "studio" if ("section-studio" in card or "studio" in raw_name.lower()) else "share"

        # From 价格
        price = None
        m = re.search(r'From <span>\$([0-9,]+)/wk</span>', card)
        if m:
            price = int(m.group(1).replace(",", ""))

        # 剔除注释后解析状态
        card_clean = re.sub(r"<!--.*?-->", "", card, flags=re.S)
        left = None
        m = re.search(r'badge-units-left-new">~\s*(\d+)\s*left', card_clean)
        if m:
            left = int(m.group(1))

        status = "unavailable"
        wm = re.search(r'price_waitlist[^>]*>\s*<p[^>]*>(.*?)</p>', card_clean, re.S)
        avail_text = clean(wm.group(1)).lower() if wm else ""
        if "wait list" in avail_text or "waitlist" in avail_text:
            status = "waitlist"
        elif left is not None or "available" in avail_text:
            status = "available"

        # 起租日期
        start = ""
        m = re.search(r'Available\s*(?:from)?\s*<br\s*/?>?\s*<span class="m-dt">([^<]+)</span>', card_clean, re.S)
        if not m:
            m = re.search(r'Available\s*(from\s*)?([A-Z][a-z]+ \d{1,2} \d{4})', card_clean)
        if m:
            start = m.group(1) if m.lastindex else clean(m.group(1))

        rooms.append({
            "name": raw_name, "url": room_url, "type": rtype,
            "price": price, "left": left, "status": status, "start": start,
        })
    return {"name": name, "rooms": rooms}


# ═══════════════════ 解析：详情页 ═══════════════════


def parse_detail_page(url, need_static):
    """详情页：租期价格（动态）+ 面积/床型（静态）"""
    page = fetch(url)
    result = {"terms": None, "area": None, "bed": None}

    # 动态：所有租期价格
    terms = {}
    for m in re.finditer(
        r'<span class="btn[^"]*"\s+id="\w+_span"[^>]*>([^<]+)</span>\s*<strong>\s*\(\$([0-9,]+)/wk\)\s*</strong>',
        page,
    ):
        zh = zh_term(clean(m.group(1)))
        terms.setdefault(zh, int(m.group(2).replace(",", "")))
    result["terms"] = terms if terms else None

    if need_static:
        # 静态：面积（合理范围 5~200）
        for m in re.finditer(r'(\d+(?:\.\d+)?)\s*(?:m²|m2|sqm|sq\.?\s*m)', page, re.I):
            try:
                v = float(m.group(1))
            except ValueError:
                continue
            if 5 <= v <= 200:
                result["area"] = f"{v:g}m²"
                break
        # 静态：床型（功能列表，截断到主床型）
        m = re.search(r'<li[^>]*>([^<>]*bed[^<>]*)</li>', page, re.I)
        if m:
            text = re.sub(r"\s+", " ", m.group(1)).strip()
            bm = re.search(r"(King single|Queen single|King|Double|Single|Twin)[^<>]*?bed", text, re.I)
            if bm:
                bed = re.sub(r"\s*bed\s*$", "", bm.group(0), flags=re.I)
                bed = re.split(r"\s+or\s+", bed, flags=re.I)[0]
                bed = re.sub(r"\s+", " ", bed.replace("-", " ")).strip()
                result["bed"] = bed
    return result


# ═══════════════════ 复检机制 ═══════════════════


def validate_and_fallback(new_data, old_data):
    """复检：数量异常回退 + 字段合理性校验。返回 (通过?, 数据)"""
    def count_rooms(data):
        return sum(len(p.get("rooms", [])) for c in data.get("cities", []) for p in c["properties"])

    new_n = count_rooms(new_data)
    old_n = count_rooms(old_data)

    # 1. 数量骤减（<70%）→ 视为抓取异常，回退旧数据
    if old_n > 0 and new_n < old_n * 0.7:
        print(f"⚠️ 复检失败: 房型数 {old_n} → {new_n}（骤减），回退上次数据")
        return False, old_data

    # 2. 字段合理性：价格/面积范围
    bad = 0
    for city in new_data.get("cities", []):
        for prop in city["properties"]:
            for r in prop.get("rooms", []):
                if r.get("price") and not (50 <= r["price"] <= 5000):
                    bad += 1
                if r.get("area"):
                    try:
                        v = float(re.sub(r"[^\d.]", "", r["area"]))
                        if not (5 <= v <= 200):
                            bad += 1
                    except ValueError:
                        bad += 1
    if bad > 0:
        print(f"⚠️ 复检发现 {bad} 条异常字段，已保留但标记")
    print(f"✅ 复检通过: {new_n} 个房型")
    return True, new_data


# ═══════════════════ 流水线 ═══════════════════


def run_status():
    """动态抓取：公寓页 + 详情页租期价格"""
    print("── 动态抓取（价格/余量/状态/起租）──")
    result = []
    for city, props in CITIES.items():
        city_data = {"city": city, "properties": []}
        for pname, purl in props.items():
            print(f"  [{city}] {pname}...")
            prop = parse_property_page(pname, purl)
            for r in prop["rooms"]:
                try:
                    detail = parse_detail_page(r["url"], need_static=False)
                    r["terms"] = detail["terms"]
                except Exception as e:
                    print(f"    ⚠️ 详情页失败: {e}")
                    r["terms"] = None
                time.sleep(REQUEST_GAP)
            city_data["properties"].append(prop)
        result.append(city_data)

    old = load_json("prices.json", {})
    fetched_at = time.strftime("%Y-%m-%d %H:%M:%S")
    new_data = {"fetched_at": fetched_at, "cities": result}
    ok, final = validate_and_fallback(new_data, old)
    if ok:
        save_json("prices.json", final)
        # 更新涨跌历史
        history = load_json("price_history.json", {})
        for city in final["cities"]:
            for prop in city["properties"]:
                for r in prop["rooms"]:
                    key = f"{prop['name']}|{r['name']}"
                    history[key] = r.get("terms") or {}
        save_json("price_history.json", history)
    return ok


def run_static():
    """静态抓取：面积/床型（两周一次）"""
    print("── 静态抓取（面积/床型）──")
    prices = load_json("prices.json", {})
    if not prices.get("cities"):
        print("⚠️ 没有 prices.json，请先跑 --mode status")
        return

    static = load_json("static_info.json", {})
    count = 0
    for city in prices["cities"]:
        for prop in city["properties"]:
            for r in prop["rooms"]:
                key = f"{prop['name']}|{r['name']}"
                try:
                    detail = parse_detail_page(r["url"], need_static=True)
                    static[key] = {"area": detail["area"], "bed": detail["bed"]}
                    count += 1
                except Exception as e:
                    print(f"  ⚠️ {key}: {e}")
                time.sleep(REQUEST_GAP)
    static["updated_at"] = time.strftime("%Y-%m-%d %H:%M:%S")
    save_json("static_info.json", static)
    print(f"✅ 静态数据已更新 {count} 个房型")


def merge_static():
    """把静态信息合并进 prices.json（渲染前调用）"""
    prices = load_json("prices.json", {})
    static = load_json("static_info.json", {})
    for city in prices.get("cities", []):
        for prop in city["properties"]:
            for r in prop["rooms"]:
                info = static.get(f"{prop['name']}|{r['name']}", {})
                r["area"] = info.get("area")
                r["bed"] = info.get("bed")
    return prices


# ═══════════════════ 渲染 ═══════════════════

STATUS_MAP = {"available": ("有房", "ok"), "waitlist": ("等位", "warn"), "unavailable": ("售罄", "bad")}

CSS = """
:root {
  --bg: #fafaf9; --card-bg: #fff;
  --text: #1a1a1a; --text-muted: #6b7280; --border: #e5e4e1;
  --green: #059669; --green-bg: #ecfdf5;
  --amber: #d97706; --amber-bg: #fffbeb;
  --red: #dc2626; --red-bg: #fef2f2;
  --radius: 8px;
  --font: 'Satoshi', system-ui, -apple-system, sans-serif;
}
@media (prefers-color-scheme: dark) {
  :root { --bg: #0c0c0c; --card-bg: #161616; --text: #e5e5e5; --text-muted: #8b8b8b; --border: #262626; }
}
*, *::before, *::after { box-sizing: border-box; margin: 0; padding: 0; }
html { font-family: var(--font); -webkit-font-smoothing: antialiased; background: var(--bg); color: var(--text); }
body { max-width: 1180px; margin: 0 auto; padding: 32px 20px 60px; line-height: 1.5; }
.header { margin-bottom: 24px; }
.header-top { display: flex; align-items: baseline; justify-content: space-between; flex-wrap: wrap; gap: 12px; margin-bottom: 4px; }
.header h1 { font-size: clamp(1.3rem, 3vw, 1.6rem); font-weight: 700; letter-spacing: -0.025em; }
.header .meta { color: var(--text-muted); font-size: 0.8rem; }
.city-nav { display: flex; gap: 6px; margin-bottom: 12px; }
.city-btn { padding: 8px 18px; border-radius: 8px; cursor: pointer; font-size: 0.85rem; font-weight: 700; color: var(--text); border: 1px solid var(--border); background: var(--card-bg); font-family: var(--font); transition: all 200ms cubic-bezier(0.32,0.72,0,1); }
.city-btn:hover { border-color: var(--text-muted); }
.city-btn.active { background: var(--text); color: var(--bg); border-color: var(--text); }
.city-btn .count { font-size: 0.68rem; opacity: 0.5; margin-left: 3px; font-weight: 400; }
.prop-nav { display: flex; gap: 6px; margin-bottom: 20px; overflow-x: auto; -webkit-overflow-scrolling: touch; scrollbar-width: none; }
.prop-nav::-webkit-scrollbar { display: none; }
.prop-btn { flex-shrink: 0; padding: 6px 14px; border-radius: 7px; cursor: pointer; font-size: 0.82rem; font-weight: 600; color: var(--text-muted); border: 1px solid var(--border); background: var(--card-bg); font-family: var(--font); transition: all 200ms cubic-bezier(0.32,0.72,0,1); white-space: nowrap; }
.prop-btn:hover { color: var(--text); border-color: var(--text-muted); }
.prop-btn.active { background: var(--text); color: var(--bg); border-color: var(--text); }
.prop-btn .count { font-size: 0.66rem; opacity: 0.5; margin-left: 3px; font-weight: 400; }
.group-title { display: flex; align-items: center; gap: 8px; margin: 16px 0 8px; font-size: 0.9rem; font-weight: 700; }
.group-title::after { content: ''; flex: 1; height: 1px; background: var(--border); }
.group-title .gcount { font-size: 0.7rem; color: var(--text-muted); font-weight: 500; }
.table-wrap { background: var(--card-bg); border: 1px solid var(--border); border-radius: var(--radius); overflow: hidden; }
.table-wrap table { width: 100%; border-collapse: collapse; font-size: 0.82rem; }
.table-wrap th { text-align: left; padding: 12px 12px; font-weight: 600; font-size: 0.68rem; text-transform: uppercase; letter-spacing: 0.05em; color: var(--text-muted); background: var(--bg); border-bottom: 1px solid var(--border); white-space: nowrap; }
.table-wrap td { padding: 13px 12px; border-bottom: 1px solid var(--border); font-variant-numeric: tabular-nums; white-space: nowrap; }
.table-wrap tr:last-child td { border-bottom: none; }
.table-wrap tbody tr { transition: background 200ms cubic-bezier(0.32,0.72,0,1); }
.table-wrap tbody tr:hover { background: var(--bg); }
.row-ok { box-shadow: inset 3px 0 0 var(--green); }
.row-warn { box-shadow: inset 3px 0 0 var(--amber); }
.row-bad { box-shadow: inset 3px 0 0 var(--red); }
.tag { display: inline-flex; align-items: center; gap: 5px; padding: 3px 10px; border-radius: 99px; font-size: 0.75rem; font-weight: 650; }
.tag::before { content: ''; width: 7px; height: 7px; border-radius: 50%; flex-shrink: 0; }
.tag-ok { background: var(--green-bg); color: var(--green); }
.tag-ok::before { background: var(--green); }
.tag-warn { background: var(--amber-bg); color: var(--amber); }
.tag-warn::before { background: var(--amber); }
.tag-bad { background: var(--red-bg); color: var(--red); }
.tag-bad::before { background: var(--red); }
.price { font-weight: 600; }
.room-name a { color: inherit; text-decoration: none; font-weight: 600; }
.room-name a:hover { text-decoration: underline; }
.chg-up { color: var(--red); font-size: 0.7rem; font-weight: 700; }
.chg-down { color: var(--green); font-size: 0.7rem; font-weight: 700; }
.city-group { display: none; }
.city-group.active { display: block; }
.prop-panel { display: none; }
.prop-panel.active { display: block; }
.coming-soon { text-align: center; padding: 48px 24px; background: var(--card-bg); border: 1px dashed var(--border); border-radius: var(--radius); margin-top: 8px; }
.cs-title { font-size: 1.2rem; font-weight: 700; margin: 0 0 8px; }
.cs-text { font-size: 0.88rem; color: var(--text-muted); }
.footer { margin-top: 40px; padding-top: 20px; border-top: 1px solid var(--border); text-align: center; }
.footer p { color: var(--text-muted); font-size: 0.75rem; }
.fade-in { opacity: 0; transform: translateY(8px); animation: fadeIn 550ms cubic-bezier(0.32,0.72,0,1) forwards; }
@keyframes fadeIn { to { opacity: 1; transform: translateY(0); } }
@media (max-width:768px) {
  body { padding: 20px 12px 50px; }
  .table-wrap { overflow-x: auto; }
  .table-wrap table { min-width: 760px; }
  .header-top { flex-direction: column; align-items: flex-start; }
}
"""

MONTHS_ZH = {"January": "1月", "February": "2月", "March": "3月", "April": "4月", "May": "5月", "June": "6月",
             "July": "7月", "August": "8月", "September": "9月", "October": "10月", "November": "11月", "December": "12月"}


def esc(s):
    return (str(s).replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;").replace('"', "&quot;"))


def zh_date(s):
    if not s:
        return ""
    s = s.strip()
    if s.lower() == "available now":
        return "即刻可住"
    m = re.match(r"(\d{1,2})\s+([A-Za-z]+)\s+(\d{4})", s)
    if m:
        return f"{m.group(3)}年{MONTHS_ZH.get(m.group(2), m.group(2))}{int(m.group(1))}日"
    return s


def short_name(name, rtype):
    n = name
    if rtype == "studio":
        n = n.replace(" Apartment", "")
    else:
        n = n.replace("Single Bedroom – ", "").replace("Single Bedroom", "").replace("Single Room – ", "")
    return re.sub(r"\s{2,}", " ", n).strip()


def prop_term_labels(prop):
    seen = set()
    for r in prop.get("rooms", []):
        seen.update((r.get("terms") or {}))
    return [t for t in TERM_ORDER if t in seen] + [t for t in sorted(seen - set(TERM_ORDER))]


def term_cell(label, r, history):
    terms = r.get("terms") or {}
    price = terms.get(label)
    if price is None:
        return "<td>—</td>"
    old = history.get(f"{r['_prop']}|{r['name']}", {}).get(label)
    if old is not None and old != price:
        cls = "chg-up" if price > old else "chg-down"
        arrow = f"▲{price - old}" if price > old else f"▼{old - price}"
        return f'<td><span class="price">${price:,}</span> <span class="{cls}">{arrow}</span></td>'
    return f'<td><span class="price">${price:,}</span></td>'


def render_group(rooms, rtype, labels, history):
    rows = ""
    for r in rooms:
        label, cls = STATUS_MAP.get(r["status"], ("未知", "bad"))
        left = f"剩{r['left']}间" if r["left"] is not None else "—"
        price = f"${r['price']:,}" if r["price"] else "—"
        start = zh_date(r.get("start") or "")
        disp = short_name(r["name"], rtype)
        link = (f'<span class="room-name"><a href="{esc(r["url"])}" target="_blank">{esc(disp)} '
                f'<span style="font-size:0.7rem;opacity:0.45;">↗</span></a></span>')
        terms = "".join(term_cell(t, r, history) for t in labels)
        rows += (f'<tr class="row-{cls}"><td>{link}</td>'
                 f'<td>{esc(r.get("area") or "—")}</td><td>{esc(r.get("bed") or "—")}</td>'
                 f'<td><span class="price">{price}</span></td>{terms}'
                 f'<td>{left}</td><td><span class="tag tag-{cls}">{label}</span></td>'
                 f'<td>{esc(start)}</td></tr>')
    if not rows:
        return ""
    head = ("<thead><tr><th>房型</th><th>面积</th><th>床型</th><th>起价</th>"
            + "".join(f"<th>{t}</th>" for t in labels)
            + "<th>剩余</th><th>状态</th><th>起租日期</th></tr></thead>")
    return f'<div class="table-wrap"><table>{head}<tbody>{rows}</tbody></table></div>'


def render(history=None):
    prices = merge_static()
    history = history or load_json("price_history.json", {})
    # 给每房型挂所属公寓（涨跌对比用）
    for city in prices.get("cities", []):
        for prop in city["properties"]:
            for r in prop["rooms"]:
                r["_prop"] = prop["name"]

    fetched = prices.get("fetched_at", "")
    static_at = load_json("static_info.json", {}).get("updated_at", "")
    static_note = f" &ensp;|&ensp; 面积/床型更新于 {static_at}" if static_at else ""

    city_nav = city_groups = ""
    for ci, city in enumerate(prices.get("cities", [])):
        active = " active" if ci == 0 else ""
        props = city["properties"]
        city_nav += (f'<button class="city-btn{active}" id="btn-city-{ci}" onclick="switchCity({ci})">'
                     f'{esc(city["city"])}<span class="count">{len(props)}</span></button>')
        prop_nav = panels = ""
        for pi, prop in enumerate(props):
            pact = " active" if pi == 0 else ""
            total = len(prop.get("rooms", []))
            prop_nav += (f'<button class="prop-btn{pact}" id="btn-p{ci}-{pi}" onclick="switchProp({ci},{pi})">'
                         f'{esc(prop["name"])}<span class="count">{total}</span></button>')
            if not prop.get("rooms"):
                panels += (f'<div class="prop-panel{pact}" id="panel-p{ci}-{pi}">'
                           '<div class="coming-soon"><div class="cs-title">🚧 即将开业</div>'
                           '<div class="cs-text">该公寓即将开放，目前仅接受登记</div></div></div>')
            else:
                labels = prop_term_labels(prop)
                studios = [r for r in prop["rooms"] if r.get("type") == "studio"]
                shares = [r for r in prop["rooms"] if r.get("type") == "share"]
                others = [r for r in prop["rooms"] if r.get("type") not in ("studio", "share")]
                body = ""
                if studios:
                    body += f'<div class="group-title">🏠 Studio <span class="gcount">{len(studios)} 种</span></div>{render_group(studios, "studio", labels, history)}'
                if shares:
                    body += f'<div class="group-title">👥 合租户型 <span class="gcount">{len(shares)} 种</span></div>{render_group(shares, "share", labels, history)}'
                if others:
                    body += f'<div class="group-title">📦 其他 <span class="gcount">{len(others)} 种</span></div>{render_group(others, "other", labels, history)}'
                panels += f'<div class="prop-panel{pact}" id="panel-p{ci}-{pi}">{body}</div>'
        city_groups += (f'<div class="city-group{active}" id="group-city-{ci}">'
                        f'<nav class="prop-nav fade-in">{prop_nav}</nav>{panels}</div>')

    html = f"""<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>Iglu 房态价格 — 异乡好居悉尼</title>
<link href="https://api.fontshare.com/v2/css?f[]=satoshi@400,500,600,700&display=swap" rel="stylesheet">
<style>{CSS}</style>
</head>
<body>
<div class="header fade-in">
  <div class="header-top"><h1>Iglu 房态价格</h1></div>
  <p class="meta">📍 悉尼 · 布里斯班 · 墨尔本 &ensp;|&ensp; 房态更新于 {fetched}{static_note} &ensp;|&ensp; 数据来源 iglu.com.au</p>
</div>
<nav class="city-nav fade-in">{city_nav}</nav>
{city_groups}
<div class="footer fade-in"><p>异乡好居悉尼 · 仅供内部参考</p></div>
<script>
function switchCity(ci){{
  document.querySelectorAll('.city-btn').forEach((b,i)=>b.classList.toggle('active',i===ci));
  document.querySelectorAll('.city-group').forEach((g,i)=>g.classList.toggle('active',i===ci));
}}
function switchProp(ci,pi){{
  var g=document.getElementById('group-city-'+ci);
  g.querySelectorAll('.prop-btn').forEach((b,i)=>b.classList.toggle('active',i===pi));
  g.querySelectorAll('.prop-panel').forEach((p,i)=>p.classList.toggle('active',i===pi));
}}
</script>
</body>
</html>"""
    with open("index.html", "w", encoding="utf-8") as f:
        f.write(html)
    print(f"✅ 已生成 index.html（更新于 {fetched}）")


# ═══════════════════ 入口 ═══════════════════

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--mode", choices=["status", "static", "all"], default="status")
    args = ap.parse_args()

    if args.mode in ("status", "all"):
        run_status()
    if args.mode in ("static", "all"):
        run_static()
    render()
    print("🎉 流水线完成")


if __name__ == "__main__":
    main()
