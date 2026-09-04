"""
Threads Graph API 自動發文（GitHub Actions 專用版本）

跑在 GitHub Actions 的 runner 上，不依賴使用者的電腦或瀏覽器，
所以就算電腦沒開機/沒連網，排程時間到還是會自動發文。

需要的 GitHub repo Secrets（Settings -> Secrets and variables -> Actions）：
  THREADS_APP_SECRET          - Meta App Secret（用於未來 token refresh）
  THREADS_USER_ACCESS_TOKEN   - johny800333 的長效存取權杖

用法：
  python3 scripts/publish.py --type overtime
  python3 scripts/publish.py --type product --index 2
"""

import argparse
import json
import os
import random
import sys
import time
from pathlib import Path

import requests

GRAPH_API_BASE = "https://graph.threads.net/v1.0"
CONTENT_DIR = Path(__file__).resolve().parent / "content"


class ThreadsPublishError(Exception):
    pass


def get_access_token() -> str:
    token = os.environ.get("THREADS_USER_ACCESS_TOKEN")
    if not token:
        raise ThreadsPublishError("缺少環境變數 THREADS_USER_ACCESS_TOKEN")
    return token


def get_threads_user_id(access_token: str) -> str:
    resp = requests.get(
        f"{GRAPH_API_BASE}/me",
        params={"fields": "id,username", "access_token": access_token},
        timeout=30,
    )
    if resp.status_code != 200:
        raise ThreadsPublishError(f"取得使用者資訊失敗: {resp.status_code} {resp.text}")
    return resp.json()["id"]


def create_media_container(user_id, access_token, text, link_attachment=None):
    params = {"text": text, "media_type": "TEXT", "access_token": access_token}
    if link_attachment:
        params["link_attachment"] = link_attachment
    resp = requests.post(f"{GRAPH_API_BASE}/{user_id}/threads", params=params, timeout=30)
    if resp.status_code != 200:
        raise ThreadsPublishError(f"建立媒體容器失敗: {resp.status_code} {resp.text}")
    return resp.json()["id"]


def publish_container(user_id, access_token, creation_id):
    resp = requests.post(
        f"{GRAPH_API_BASE}/{user_id}/threads_publish",
        params={"creation_id": creation_id, "access_token": access_token},
        timeout=30,
    )
    if resp.status_code != 200:
        raise ThreadsPublishError(f"發佈貼文失敗: {resp.status_code} {resp.text}")
    return resp.json()["id"]


def load_daily_life_text(post_type: str) -> str:
    data = json.loads((CONTENT_DIR / "daily_life_posts.json").read_text(encoding="utf-8"))
    options = data.get(post_type)
    if not options:
        raise ThreadsPublishError(f"找不到貼文類型: {post_type}")
    return random.choice(options)


def load_product_text(index: int) -> tuple[str, str | None]:
    data = json.loads((CONTENT_DIR / "product_posts.json").read_text(encoding="utf-8"))
    if index < 0 or index >= len(data):
        raise ThreadsPublishError(f"商品貼文編號超出範圍: {index}")
    item = data[index]
    return item["text"], item.get("link")


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--type", required=True, choices=["overtime", "food", "surprise", "weekend", "workmeltdown", "latenight", "product"])
    parser.add_argument("--index", type=int, default=None, help="product 類型專用：第幾篇商品貼文（0 起算）")
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()

    if args.type == "product":
        if args.index is None:
            raise SystemExit("product 類型需要 --index")
        text, link = load_product_text(args.index)
    else:
        text = load_daily_life_text(args.type)
        link = None

    print("將發佈內容：\n" + text)

    if args.dry_run:
        print("(dry-run，未實際發佈)")
        return

    access_token = get_access_token()
    user_id = get_threads_user_id(access_token)
    creation_id = create_media_container(user_id, access_token, text, link_attachment=link)
    time.sleep(5)
    post_id = publish_container(user_id, access_token, creation_id)
    print(f"已發佈，post_id={post_id}")


if __name__ == "__main__":
    try:
        main()
    except ThreadsPublishError as e:
        print(f"錯誤: {e}", file=sys.stderr)
        sys.exit(1)
