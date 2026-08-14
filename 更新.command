#!/bin/bash
# ダブルクリックで価格を手動チェックし、動きがあればGitHubへ反映する。
cd "$(dirname "$0")" || exit 1
echo "=== Apple 価格ウォッチ 手動更新 ==="
python3 collect.py || { echo "取得に失敗しました"; read -r -p "Enterで閉じる"; exit 1; }
if git diff --quiet docs/prices.json 2>/dev/null; then
  echo "価格に動きはありませんでした。"
else
  git add docs/prices.json
  git commit -m "価格の手動更新 $(date '+%Y-%m-%d %H:%M')" && git push && echo "公開サイトに反映しました。"
fi
read -r -p "Enterで閉じる"
