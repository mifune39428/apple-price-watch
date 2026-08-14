#!/usr/bin/env python3
"""Apple Store（日本）の全モデルの価格を毎回取り直し、前回との差分を記録する。

値上げ・値下げ・新登場・取り扱い終了の4種類を「できごと」として docs/prices.json に
積んでいく。サイト側（docs/index.html）はこのJSONだけを読んで表示する。

LLMもAPIキーも使わない。やっているのは購入ページに埋め込まれた
window.PRODUCT_SELECTION_BOOTSTRAP の JSON を読んで、SKUごとの価格を拾うことだけ。
"""

from __future__ import annotations

import datetime as dt
import html as htmllib
import json
import os
import re
import sys
import time
import urllib.error
import urllib.request

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
FAMILIES_PATH = os.path.join(BASE_DIR, "families.json")
OUTPUT_PATH = os.path.join(BASE_DIR, "docs", "prices.json")

STORE_ROOT = "https://www.apple.com/jp/shop/"
USER_AGENT = "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0 Safari/537.36"
FETCH_TIMEOUT = 30
# 連続で叩かないための待ち。Apple 側に迷惑をかけない程度に開ける。
FETCH_INTERVAL = 1.2

# できごとを残す期間と件数の上限。
KEEP_EVENT_DAYS = 365
KEEP_EVENT_MAX = 1500

JST = dt.timezone(dt.timedelta(hours=9))

# サイトの並び順。カテゴリ名をそのまま並べ替えると Mac より AirPods が先に来てしまう。
CATEGORY_ORDER = ["iPhone", "iPad", "Mac", "Apple Watch", "AirPods", "Vision", "その他"]

CATEGORY_BY_PREFIX = {
    "buy-mac": "Mac",
    "buy-iphone": "iPhone",
    "buy-ipad": "iPad",
    "buy-watch": "Apple Watch",
    "buy-airpods": "AirPods",
    "buy-vision": "Vision",
}


# --------------------------------------------------------------------------
# 取得
# --------------------------------------------------------------------------

def fetch(url: str) -> tuple[str, str] | tuple[None, None]:
    """本文と、リダイレクトを追いかけた後の最終URLを返す。

    最終URLが要るのは、buy-airpods/airpods-max のような旧スラッグが
    新モデルのページに転送されるため。同じページを2つの製品として
    数えてしまわないよう、最終URLで重複を落とす。
    """
    req = urllib.request.Request(url, headers={
        "User-Agent": USER_AGENT,
        "Accept-Language": "ja-JP,ja;q=0.9",
    })
    try:
        with urllib.request.urlopen(req, timeout=FETCH_TIMEOUT) as res:
            return res.read().decode("utf-8", "replace"), res.geturl()
    except (urllib.error.URLError, urllib.error.HTTPError, TimeoutError, OSError) as e:
        print(f"  取得できませんでした: {url} ({e})")
        return None, None


def extract_bootstrap(html: str) -> dict | None:
    """window.PRODUCT_SELECTION_BOOTSTRAP の productSelectionData を取り出す。"""
    i = html.find("productSelectionData:")
    if i < 0:
        return None
    try:
        i = html.index("{", i)
        obj, _ = json.JSONDecoder().raw_decode(html[i:])
    except (ValueError, json.JSONDecodeError):
        return None
    return obj if isinstance(obj, dict) else None


# --------------------------------------------------------------------------
# ラベルの掃除
# --------------------------------------------------------------------------

# 「10コアCPU、8コアGPU<div class="form-label-small">あらゆることを…</div>」のように
# 補足説明がぶら下がっているので、そこから後ろは名前に含めない。
_TAIL = re.compile(r'<(?:div|span) class="form-label-small".*', re.S)
_DROP = re.compile(
    r"<as-footnote.*?</as-footnote>"
    r"|<sup[^>]*>.*?</sup>"
    r'|<span class="visuallyhidden">.*?</span>',
    re.S,
)
_TAGS = re.compile(r"<[^>]+>")


def clean_label(raw: str | None) -> str:
    if not raw:
        return ""
    s = _TAIL.sub("", raw)
    s = _DROP.sub("", s)
    s = _TAGS.sub("", s)
    s = htmllib.unescape(s).replace(" ", " ")
    s = re.sub(r"\s+", " ", s).strip()
    # 脚注を消した後に残る記号のかけら。
    return s.strip(" ・,、/").strip()


def humanize(value: str) -> str:
    """ラベルが用意されていない値（Apple Watchの色など）を、そのまま出すよりは読める形に。"""
    # 49mm を 49Mm にしないよう、数字を含む語はさわらない。
    return " ".join(
        w if any(c.isdigit() for c in w) else w.capitalize()
        for w in re.split(r"[_-]", value) if w
    )


def label_for(node: dict | None, value: str) -> str:
    """寸法の1つぶんの表示名。header / value → コア数 → 素の値、の順に諦めていく。"""
    if isinstance(node, dict):
        label = clean_label(node.get("header") or node.get("value"))
        if label:
            return label
        comp = node.get("dimensionComponents")
        if isinstance(comp, dict) and comp.get("cpuCoreCount"):
            cpu, gpu = comp.get("cpuCoreCount"), comp.get("gpuCoreCount")
            return f"{cpu}コアCPU、{gpu}コアGPU" if gpu else f"{cpu}コアCPU"
    return humanize(value)


def price_of(entry: dict) -> float | None:
    """価格の入り方がページの型によって違うので、拾えるところから拾う。"""
    for key in ("amountBeforeTradeIn", "amount", "seoPrice"):
        v = entry.get(key)
        if isinstance(v, (int, float)) and v > 0:
            return float(v)
    # ディスプレイやAirPodsの型では currentPrice の中に文字列で入っている。
    current = entry.get("currentPrice")
    if isinstance(current, dict):
        try:
            v = float(current.get("raw_amount"))
        except (TypeError, ValueError):
            return None
        if v > 0:
            return v
    return None


def price_key_of(product: dict) -> str | None:
    """SKUと価格表をつなぐキー。ページの型ごとに置き場所が違う。"""
    for key in ("priceKey", "fullPrice", "price"):
        v = product.get(key)
        if isinstance(v, str) and v:
            return v
    return None


# --------------------------------------------------------------------------
# ページ1枚 → SKU一覧
# --------------------------------------------------------------------------

def parse_family(path: str, html: str) -> list[dict]:
    """購入ページからSKU（構成ごとの1商品）を取り出す。

    ページには2つの型がある。
      Mac型    … mainDisplayValues を持ち、寸法は products[].dimensions に入る
      iPhone型 … displayValues を持ち、寸法は products[] の直下に平らに並ぶ
    どちらも価格は displayValues.prices[キー].amount 系にある。
    """
    data = extract_bootstrap(html)
    if not data:
        return parse_single_product(path, html)

    dv = data.get("mainDisplayValues") or data.get("displayValues") or {}
    prices = dv.get("prices") or {}
    products = data.get("products") or []
    if not isinstance(products, list) or not prices:
        return parse_single_product(path, html)

    dim_keys = [
        k for k in dv
        if k != "prices" and not k.startswith("carrier") and isinstance(dv.get(k), dict)
    ]

    # SIMフリーとキャリア版が両方並ぶページ（iPhone）では、比較の軸になる
    # SIMフリー版だけを見る。キャリア版は同じ端末が3回出てくるだけで、
    # しかも各社の割引で値段が動くので、値上げ・値下げの判定が濁る。
    unlocked = [p for p in products if p.get("carrierPolicyType") == "UNLOCKED"]
    if unlocked:
        products = unlocked

    skus: dict[str, dict] = {}
    for p in products:
        if not isinstance(p, dict):
            continue
        key = price_key_of(p)
        entry = prices.get(key) if key else None
        if not isinstance(entry, dict):
            continue
        amount = price_of(entry)
        if amount is None:
            continue

        part = p.get("partNumber") or p.get("btrOrFdPartNumber") or p.get("part")
        dims = p.get("dimensions") if isinstance(p.get("dimensions"), dict) else None

        labels = []
        for dk in dim_keys:
            value = dims.get(dk) if dims else p.get(dk)
            if not isinstance(value, str):
                continue
            labels.append(label_for(dv.get(dk, {}).get(value), value))

        # 部品番号が無い構成（同じ値段の色違いをまとめている等）もあるので、
        # その場合は価格キーを識別子にする。どちらも実行をまたいで変わらない。
        sku_id = part if part else f"{path}#{key}"
        if sku_id in skus:
            continue
        skus[sku_id] = {
            "id": sku_id,
            "part": part,
            "name": " / ".join(x for x in labels if x) or "標準構成",
            "price": int(round(amount)),
        }
    return list(skus.values())


_CURRENT_PRICE = re.compile(r'class="current_price"[^>]*>\s*([0-9,]+)円')


def parse_single_product(path: str, html: str) -> list[dict]:
    """AirPodsのように構成が1つしかないページ用。価格が本文のHTMLに直接出ている。"""
    m = _CURRENT_PRICE.search(html)
    if not m:
        return []
    data = extract_bootstrap(html) or {}
    products = data.get("products") or []
    part = None
    if products and isinstance(products[0], dict):
        part = products[0].get("partNumber") or products[0].get("part")
    sku_id = part or f"{path}#single"
    return [{
        "id": sku_id,
        "part": part,
        "name": "標準構成",
        "price": int(m.group(1).replace(",", "")),
    }]


# --------------------------------------------------------------------------
# 監視対象の決定
# --------------------------------------------------------------------------

_BUY_PATH = re.compile(r"/jp/shop/(buy-[a-z]+/[a-z0-9-]+)")


def discover_paths(store_html: str | None) -> list[str]:
    """Apple Store トップに並んでいる購入ページを拾う。

    新しい製品ラインが増えたときに families.json を書き足さなくても追随できる。
    """
    if not store_html:
        return []
    return sorted(set(_BUY_PATH.findall(store_html)))


def family_label(slug: str, labels: dict) -> str:
    if slug in labels:
        return labels[slug]
    return " ".join(w.capitalize() for w in slug.split("-"))


# --------------------------------------------------------------------------
# 状態の読み書きと差分
# --------------------------------------------------------------------------

def load_state() -> dict:
    if not os.path.exists(OUTPUT_PATH):
        return {"skus": {}, "events": [], "families": {}}
    with open(OUTPUT_PATH, encoding="utf-8") as f:
        data = json.load(f)
    return {
        "skus": {s["id"]: s for s in data.get("skus", [])},
        "events": data.get("events", []),
        "families": {f["id"]: f for f in data.get("families", [])},
    }


def diff_family(family_id: str, label: str, category: str, url: str,
                found: list[dict], previous: dict, today: str,
                baseline: bool = False) -> tuple[list[dict], list[dict]]:
    """1ファミリーぶんの新旧を突き合わせて、SKUの新しい姿とできごとを返す。

    baseline=True（前回の記録がまったく無い初回）のときは、全SKUが「新登場」に
    なってしまうので、できごとは作らずに今の値段を基準として置くだけにする。
    """
    events: list[dict] = []
    current: list[dict] = []
    seen = set()

    for sku in found:
        seen.add(sku["id"])
        old = previous.get(sku["id"])
        record = {
            "id": sku["id"],
            "part": sku["part"],
            "family": family_id,
            "family_label": label,
            "category": category,
            "url": url,
            "name": sku["name"],
            "price": sku["price"],
            "first_seen": old.get("first_seen", today) if old else today,
            "price_since": old.get("price_since", today) if old else today,
        }
        if old is None:
            if baseline:
                current.append(record)
                continue
            events.append({
                "date": today, "type": "new", "sku": sku["id"], "family": family_id,
                "family_label": label, "category": category, "name": sku["name"],
                "url": url, "old_price": None, "new_price": sku["price"], "diff": None,
            })
        elif old.get("price") != sku["price"]:
            gap = sku["price"] - old["price"]
            record["price_since"] = today
            events.append({
                "date": today, "type": "price_up" if gap > 0 else "price_down",
                "sku": sku["id"], "family": family_id, "family_label": label,
                "category": category, "name": sku["name"], "url": url,
                "old_price": old["price"], "new_price": sku["price"], "diff": gap,
            })
        current.append(record)

    # このファミリーの取得に成功したときだけ、消えたSKUを「取り扱い終了」とみなす。
    # 取得に失敗したファミリーはこの関数に来ない（前回の姿をそのまま残す）。
    for sku_id, old in previous.items():
        if not baseline and old.get("family") == family_id and sku_id not in seen:
            events.append({
                "date": today, "type": "gone", "sku": sku_id, "family": family_id,
                "family_label": label, "category": category, "name": old.get("name", ""),
                "url": url, "old_price": old.get("price"), "new_price": None, "diff": None,
            })
    return current, events


def prune_events(events: list[dict]) -> list[dict]:
    limit = (dt.datetime.now(JST) - dt.timedelta(days=KEEP_EVENT_DAYS)).strftime("%Y-%m-%d")
    kept = [e for e in events if e.get("date", "") >= limit]
    kept.sort(key=lambda e: (e.get("date", ""), e.get("family_label", ""), e.get("name", "")), reverse=True)
    return kept[:KEEP_EVENT_MAX]


# --------------------------------------------------------------------------
# 本体
# --------------------------------------------------------------------------

def main() -> int:
    with open(FAMILIES_PATH, encoding="utf-8") as f:
        conf = json.load(f)
    blocklist = set(conf.get("blocklist", []))
    labels = conf.get("labels", {})

    now = dt.datetime.now(JST)
    today = now.strftime("%Y-%m-%d")

    print("Apple Store トップから購入ページを探しています…")
    store_html, _ = fetch(STORE_ROOT)
    discovered = discover_paths(store_html)
    paths = [p for p in dict.fromkeys(list(conf.get("seed", [])) + discovered) if p not in blocklist]
    new_paths = [p for p in discovered if p not in conf.get("seed", []) and p not in blocklist]
    if new_paths:
        print(f"  families.json に無いページを見つけました: {', '.join(new_paths)}")
    print(f"  監視対象 {len(paths)} ページ")

    state = load_state()
    previous = state["skus"]
    baseline = not previous
    if baseline:
        print("  前回の記録がありません。今回は基準を作るだけで、変動は記録しません。")

    skus: list[dict] = []
    events: list[dict] = []
    families: list[dict] = []
    failed: list[str] = []
    carried = 0
    visited: set[str] = set()
    taken: set[str] = set()

    for path in paths:
        prefix, slug = path.split("/", 1)
        label = family_label(slug, labels)
        category = CATEGORY_BY_PREFIX.get(prefix, "その他")
        url = STORE_ROOT + path

        time.sleep(FETCH_INTERVAL)
        html, final_url = fetch(url)

        # 旧スラッグが新モデルのページへ転送されることがある。
        # 同じページを2つの製品として並べないよう、転送先で見た覚えがあれば飛ばす。
        if final_url and final_url in visited:
            print(f"  － {label}: {final_url.rsplit('/', 1)[-1]} と同じページなので飛ばします")
            continue
        if final_url:
            visited.add(final_url)

        found = parse_family(path, html) if html else []
        # 別ページに同じSKUが載っている場合も、先に出たほうだけを残す。
        found = [s for s in found if s["id"] not in taken]
        taken.update(s["id"] for s in found)

        if not found:
            # 取れなかったページは無かったことにする。ここで消してしまうと
            # 一時的な失敗が「全モデル取り扱い終了」に化けてしまう。
            keep = [s for s in previous.values() if s.get("family") == slug]
            if keep:
                skus.extend(keep)
                carried += len(keep)
                families.append(state["families"].get(slug) or {
                    "id": slug, "label": label, "category": category, "url": url,
                })
            failed.append(path)
            print(f"  × {label}（前回の {len(keep)} 件をそのまま残します）")
            continue

        current, family_events = diff_family(slug, label, category, url, found, previous, today, baseline)
        skus.extend(current)
        events.extend(family_events)
        families.append({"id": slug, "label": label, "category": category, "url": url})

        changed = [e for e in family_events if e["type"] in ("price_up", "price_down")]
        mark = f" ← {len(changed)}件の価格変動" if changed else ""
        print(f"  ○ {label}: {len(current)}件{mark}")

    if not skus:
        print("1件も取れませんでした。前回のファイルを壊さないよう、書き込みを中止します。")
        return 1

    # 取得に失敗したページが多すぎるときは、その回の判定自体が怪しい。
    if len(failed) > len(paths) / 2:
        print(f"半数以上（{len(failed)}/{len(paths)}）のページを取得できませんでした。書き込みを中止します。")
        return 1

    all_events = prune_events(state["events"] + events)
    def order(category: str) -> int:
        return CATEGORY_ORDER.index(category) if category in CATEGORY_ORDER else len(CATEGORY_ORDER)

    skus.sort(key=lambda s: (order(s["category"]), s["family_label"], s["price"]))
    families.sort(key=lambda f: (order(f["category"]), f["label"]))

    out = {
        "updated": now.isoformat(timespec="seconds"),
        "source": "Apple Store（日本）",
        "families": families,
        "skus": skus,
        "events": all_events,
    }
    os.makedirs(os.path.dirname(OUTPUT_PATH), exist_ok=True)
    with open(OUTPUT_PATH, "w", encoding="utf-8") as f:
        json.dump(out, f, ensure_ascii=False, indent=1)

    ups = sum(1 for e in events if e["type"] == "price_up")
    downs = sum(1 for e in events if e["type"] == "price_down")
    news = sum(1 for e in events if e["type"] == "new")
    gones = sum(1 for e in events if e["type"] == "gone")
    print(f"\n{len(skus)}件のSKUを記録しました"
          + (f"（うち {carried} 件は前回のまま）" if carried else "")
          + f"。値上げ{ups} 値下げ{downs} 新登場{news} 終了{gones}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
