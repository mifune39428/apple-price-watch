# Apple 価格ウォッチ

Apple Store（日本）に並んでいる全モデルの価格を定期的に取り直し、
**値上げ・値下げ・新登場・取り扱い終了**を自動で見つけて記録する。

公開サイト: https://mifune39428.github.io/apple-price-watch/

## 何を見ているか

`https://www.apple.com/jp/shop/buy-*/…` の購入ページに埋め込まれている
`window.PRODUCT_SELECTION_BOOTSTRAP` の JSON を読み、構成（SKU）ごとの価格を拾っている。
LLM も API キーも使わない。Apple 公式が自分のページを描くのに使っているデータなので、
表示価格とずれない。

- 見ているのは各ページの**標準構成**の価格（BTO で盛った構成は対象外）。
- iPhone は**SIM フリー版だけ**。キャリア版は同じ端末が3回出てくるうえ、
  各社の割引で値段が動くので、値上げ・値下げの判定が濁る。
- 監視対象は `families.json` の `seed` と、**Apple Store トップから自動で見つけたページ**の合算。
  新しい製品ラインが増えても `families.json` を書き足さずに追随できる。

## 使う

```bash
python3 collect.py     # 価格を取り直して docs/prices.json を更新
```

`更新.command` をダブルクリックすると、収集してから GitHub へ push まで行う。
GitHub Actions（`.github/workflows/update.yml`）が3時間ごとに同じことをする。

## 出てくるファイル

`docs/prices.json` ひとつだけ。サイト（`docs/index.html`）はこれを読んで描く。

```
updated   最終更新
families  監視している製品ライン
skus      いま売られている構成と価格（first_seen / price_since 付き）
events    できごと（price_up / price_down / new / gone）を365日ぶん
```

## 壊れないようにしてあるところ

- **初回は基準を作るだけ**。前回の記録が無い状態で走らせると全モデルが「新登場」に
  なってしまうので、1回目はできごとを作らない。
- **取れなかったページは前回の姿をそのまま残す**。ここで消すと、一時的な通信失敗が
  「全モデル取り扱い終了」に化ける。
- **半数以上のページが取れなかった回は書き込みごと中止**する。
- **リダイレクト先で重複を落とす**。`buy-airpods/airpods-max` は
  `airpods-max-2` に転送されるので、そのままだと同じ製品が2つ並ぶ。

## ページの型が3つある

購入ページの JSON は製品によって形が違う。`parse_family()` はこの3つを吸収している。

| 型 | 例 | 価格表の場所 | SKUとの繋ぎ |
|---|---|---|---|
| Mac 型 | MacBook Air | `mainDisplayValues.prices` | `products[].priceKey` |
| iPhone 型 | iPhone / iPad | `displayValues.prices` | `products[].fullPrice` |
| 金額キー型 | Studio Display / AirPods | `displayValues.prices`（キーが `269800_00`） | `products[].price` |

金額が入っているキーも `amount` / `amountBeforeTradeIn` / `currentPrice.raw_amount` と
まちまちなので、`price_of()` が順に試す。
表示名は寸法（色・容量・チップ）のラベルを繋いで作る。Apple Watch の色のように
ラベルが用意されていない値もあるので、その場合は値そのものを整形して使う。
