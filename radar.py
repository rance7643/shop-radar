from playwright.sync_api import sync_playwright
import folium
import re
import json
import os
import requests

URL = "https://rent.591.com.tw/list?region=1&kind=5&mrt=1"

TARGET_AREAS = ["大安區", "內湖區", "信義區", "中山區", "文山區"]
MAX_RENT = 60000
MIN_PING = 15
MAX_PING = 30
MAX_MRT_METERS = 1000

LINE_CHANNEL_ACCESS_TOKEN = os.getenv("LINE_CHANNEL_ACCESS_TOKEN")

print("開始用 Playwright 搜尋 591 店面...")


def clean_text(t: str) -> str:
    return re.sub(r"\s+", " ", t).strip()


def parse_rent(text: str):
    m = re.search(r"(\d[\d,]*)\s*元/月", text)
    if m:
        return int(m.group(1).replace(",", ""))
    return None


def parse_ping(text: str):
    m = re.search(r"(\d+(?:\.\d+)?)\s*坪", text)
    if m:
        return float(m.group(1))
    return None


def parse_mrt_distance(text: str):
    m1 = re.search(r"距.*?(\d+)\s*公尺", text)
    if m1:
        return int(m1.group(1))

    m2 = re.search(r"距.*?(\d+(?:\.\d+)?)\s*公里", text)
    if m2:
        return int(float(m2.group(1)) * 1000)

    return None


def parse_floor(text: str):
    m = re.search(r"(\d+F/\d+F|\d+樓/\d+樓|\d+F|\d+樓)", text)
    if m:
        return m.group(1)
    return "未標示"


def area_ok(text: str):
    return any(area in text for area in TARGET_AREAS)


def get_area(text: str):
    for area in TARGET_AREAS:
        if area in text:
            return area
    return "未標示區域"


def floor_ok(text: str):
    floor = parse_floor(text)

    if floor == "未標示":
        return False

    return (
        floor.startswith("1F") or
        floor.startswith("2F") or
        floor.startswith("1樓") or
        floor.startswith("2樓")
    )


def recent_ok(text: str):
    if "今日更新" in text or "小時內更新" in text:
        return True

    m = re.search(r"(\d+)天內更新", text)
    if m:
        return int(m.group(1)) <= 3

    m2 = re.search(r"(\d+)天前更新", text)
    if m2:
        return int(m2.group(1)) <= 3

    return False


def food_ok(text: str):
    keywords = [
        "可餐飲", "餐飲", "店面", "商業用", "營業用",
        "可開店", "可輕食", "可咖啡"
    ]
    return any(k in text for k in keywords)


def basement_bad(text: str):
    bad = ["地下室", "B1", "B2", "地下一樓"]
    return any(k in text for k in bad)


def alley_score(text: str):
    keywords = ["巷", "巷弄", "靜巷", "住宅巷", "鬧中取靜", "文青"]
    return any(k in text for k in keywords)


def life_score(text: str):
    keywords = ["住宅", "社區", "學區", "學校", "公園", "市場", "生活圈"]
    return any(k in text for k in keywords)


def shop_type(text: str):
    if alley_score(text):
        return "巷弄店面"
    return "一般店面"


def calc_score(text: str, rent: int, ping: float, mrt: int):
    score = 0

    if mrt <= 1000:
        score += 3
    if 15 <= ping <= 30:
        score += 2
    if rent <= 60000:
        score += 2

    floor = parse_floor(text)
    if floor.startswith("1F") or floor.startswith("1樓"):
        score += 2
    elif floor.startswith("2F") or floor.startswith("2樓"):
        score += 1

    if alley_score(text):
        score += 2
    if life_score(text):
        score += 2
    if not basement_bad(text):
        score += 1
    if food_ok(text):
        score += 2

    return score


def make_item_id(text: str, link: str):
    base = (link + "|" + re.sub(r"\s+", "", text))[:200]
    return base


def load_sent_ids():
    try:
        with open("sent_ids.json", "r", encoding="utf-8") as f:
            return set(json.load(f))
    except Exception:
        return set()


def save_sent_ids(sent_ids):
    with open("sent_ids.json", "w", encoding="utf-8") as f:
        json.dump(sorted(list(sent_ids)), f, ensure_ascii=False, indent=2)


def extract_name(text: str, area: str):
    lines = [x.strip() for x in re.split(r"\s+", text) if x.strip()]
    joined = " ".join(lines)

    joined = joined.replace(area, "").strip()
    joined = re.sub(r"\d[\d,]*\s*元/月.*", "", joined).strip()

    if len(joined) >= 6:
        return joined[:28]

    return f"{area}店面"


def send_line_broadcast(text: str):
    if not LINE_CHANNEL_ACCESS_TOKEN:
        print("尚未設定 LINE_CHANNEL_ACCESS_TOKEN，略過 LINE 推播")
        return

    url = "https://api.line.me/v2/bot/message/broadcast"
    headers = {
        "Authorization": f"Bearer {LINE_CHANNEL_ACCESS_TOKEN}",
        "Content-Type": "application/json"
    }
    payload = {
        "messages": [
            {
                "type": "text",
                "text": text[:5000]
            }
        ]
    }

    try:
        r = requests.post(url, headers=headers, json=payload, timeout=30)
        print("LINE broadcast 狀態碼：", r.status_code)
        print("LINE broadcast 回應：", r.text)
    except Exception as e:
        print("LINE 推播失敗：", str(e))


def format_line_message(item, index):
    food_text = "是" if item["food_ok"] else "否"

    return (
        f"新物件 {index}\n"
        f"名稱：{item['name']}\n"
        f"地區：{item['area']}\n"
        f"租金：{item['rent']:,}\n"
        f"坪數：{item['ping']}坪\n"
        f"樓層：{item['floor']}\n"
        f"型態：{item['shop_kind']}\n"
        f"可餐飲：{food_text}\n"
        f"捷運距離：{item['mrt']}公尺\n"
        f"評分：{item['score']}分\n"
        f"連結：{item['link']}"
    )


def extract_cards_from_page(page):
    cards = page.evaluate("""
    () => {
        const clean = (s) => (s || "").replace(/\\s+/g, " ").trim();
        const results = [];
        const seen = new Set();

        const anchors = Array.from(document.querySelectorAll("a[href]"));

        for (const a of anchors) {
            let href = a.getAttribute("href") || "";
            if (!href) continue;

            if (href.startsWith("/")) {
                href = "https://rent.591.com.tw" + href;
            }

            if (!href.startsWith("http")) continue;
            if (!href.includes("591.com.tw")) continue;

            let node = a;
            let box = null;

            for (let i = 0; i < 6 && node; i++) {
                const txt = clean(node.innerText || "");
                if (txt.includes("元/月") && txt.includes("坪") && (txt.includes("更新") || txt.includes("距"))) {
                    box = node;
                    break;
                }
                node = node.parentElement;
            }

            if (!box) continue;

            const text = clean(box.innerText || "");
            if (!text) continue;

            const key = href + "|" + text.slice(0, 80);
            if (seen.has(key)) continue;
            seen.add(key);

            results.push({
                href,
                text
            });
        }

        return results;
    }
    """)
    return cards


with sync_playwright() as p:
    browser = p.chromium.launch(headless=True)
    page = browser.new_page()
    page.goto(URL, wait_until="domcontentloaded", timeout=60000)
    page.wait_for_timeout(15000)

    print("目前頁面標題：", page.title())

    raw_cards = extract_cards_from_page(page)
    print("抓到原始卡片數量：", len(raw_cards))

    sent_ids = load_sent_ids()
    filtered = []
    new_sent_ids = set(sent_ids)

    for raw in raw_cards:
        text = clean_text(raw["text"])
        link = raw["href"]

        rent = parse_rent(text)
        ping = parse_ping(text)
        mrt = parse_mrt_distance(text)

        if not area_ok(text):
            continue
        if rent is None or rent > MAX_RENT:
            continue
        if ping is None or not (MIN_PING <= ping <= MAX_PING):
            continue
        if mrt is None or mrt > MAX_MRT_METERS:
            continue
        if not floor_ok(text):
            continue
        if not recent_ok(text):
            continue
        if not food_ok(text):
            continue
        if basement_bad(text):
            continue

        item_id = make_item_id(text, link)
        if item_id in sent_ids:
            continue

        area = get_area(text)
        floor = parse_floor(text)
        score = calc_score(text, rent, ping, mrt)
        name = extract_name(text, area)

        item = {
            "id": item_id,
            "text": text,
            "rent": rent,
            "ping": ping,
            "mrt": mrt,
            "floor": floor,
            "area": area,
            "food_ok": food_ok(text),
            "shop_kind": shop_type(text),
            "score": score,
            "name": name,
            "link": link
        }

        filtered.append(item)
        new_sent_ids.add(item_id)

    print("符合條件的店面數量：", len(filtered))
    print("-" * 50)

    for i, item in enumerate(filtered[:10], start=1):
        print(format_line_message(item, i))
        print("-" * 50)

    if filtered:
        messages = []
        for i, item in enumerate(filtered[:3], start=1):
            messages.append(format_line_message(item, i))

        final_message = "\\n\\n".join(messages)
        send_line_broadcast(final_message)
    else:
        print("沒有新物件，這次不發 LINE")

    m = folium.Map(location=[25.04, 121.54], zoom_start=12)

    for i, item in enumerate(filtered, start=1):
        popup_text = f"""
        <b>物件 {i}</b><br>
        名稱：{item['name']}<br>
        地區：{item['area']}<br>
        租金：{item['rent']:,} 元/月<br>
        坪數：{item['ping']} 坪<br>
        樓層：{item['floor']}<br>
        捷運距離：{item['mrt']} 公尺<br>
        型態：{item['shop_kind']}<br>
        評分：{item['score']} 分<br>
        <a href="{item['link']}" target="_blank">打開物件頁面</a>
        """

        folium.Marker(
            [25.04 + i * 0.003, 121.54 + i * 0.003],
            popup=folium.Popup(popup_text, max_width=350)
        ).add_to(m)

    m.save("radar_map.html")
    print("已更新 radar_map.html")

    save_sent_ids(new_sent_ids)
    print("已更新 sent_ids.json")

    browser.close()
