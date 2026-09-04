import argparse, json, os, random, sys, time
from pathlib import Path
import requests

GRAPH_API_BASE = "https://graph.threads.net/v1.0"
CONTENT_DIR = Path(__file__).resolve().parent / "content"

HASHTAGS = {"overtime": "#社畜日常 #加班人生 #台北上班族 #下班後", "food": "#台灣美食 #巷弄美食 #在地小吃", "surprise": "#生活小確幸 #日常驚喜 #台灣日常", "weekend": "#週末日常 #台灣生活 #耍廢日常", "workmeltdown": "#職場日常 #上班族日常 #療癒美食", "latenight": "#深夜獨白 #夜貓子 #台灣日常"}

class ThreadsPublishError(Exception): pass

def _raise(msg): raise ThreadsPublishError(msg)

def _json_or_raise(resp, ctx): return resp.json() if resp.status_code == 200 else _raise(ctx + " failed: " + str(resp.status_code) + " " + resp.text)

def get_access_token(): return os.environ.get("THREADS_USER_ACCESS_TOKEN") or _raise("missing env var THREADS_USER_ACCESS_TOKEN")

def get_threads_user_id(access_token): return _json_or_raise(requests.get(GRAPH_API_BASE + "/me", params={"fields": "id,username", "access_token": access_token}, timeout=30), "get user info")["id"]

def keyword_image_url(keywords): return "https://loremflickr.com/1080/1080/" + keywords + "?lock=" + str(random.randint(1, 999999))

def create_media_container(user_id, access_token, text, link_attachment=None, image_url=None): return _json_or_raise(requests.post(GRAPH_API_BASE + "/" + user_id + "/threads", params={k: v for k, v in {"text": text, "access_token": access_token, "media_type": "IMAGE" if image_url else "TEXT", "image_url": image_url, "link_attachment": link_attachment}.items() if v is not None}, timeout=30), "create media container")["id"]

def publish_container(user_id, access_token, creation_id): return _json_or_raise(requests.post(GRAPH_API_BASE + "/" + user_id + "/threads_publish", params={"creation_id": creation_id, "access_token": access_token}, timeout=30), "publish post")["id"]

def load_daily_life_item(post_type): return random.choice(json.loads((CONTENT_DIR / "daily_life_posts.json").read_text(encoding="utf-8")).get(post_type) or _raise("unknown post type: " + post_type))

def load_product_item(index): return json.loads((CONTENT_DIR / "product_posts.json").read_text(encoding="utf-8"))[index]

parser = argparse.ArgumentParser()
parser.add_argument("--type", required=True, choices=["overtime", "food", "surprise", "weekend", "workmeltdown", "latenight", "product"])
parser.add_argument("--index", type=int, default=None)
parser.add_argument("--dry-run", action="store_true")
parser.add_argument("--no-image", action="store_true")
args = parser.parse_args()

item = load_product_item(args.index) if args.type == "product" and args.index is not None else None
daily_item = load_daily_life_item(args.type) if item is None else None
base_text = item["text"] if item else daily_item["text"]
text = base_text if item else base_text + "\n\n" + HASHTAGS.get(args.type, "")
link = item.get("link") if item else None
image_url = None if (item is not None or args.no_image) else keyword_image_url(daily_item.get("image_keywords") or "taiwan,daily-life")

print("post text:")
print(text)
if image_url: print("image: " + image_url)
if args.dry_run: print("(dry-run, not published)"); sys.exit(0)

access_token = get_access_token()
user_id = get_threads_user_id(access_token)
creation_id = create_media_container(user_id, access_token, text, link_attachment=link, image_url=image_url)
time.sleep(5)
post_id = publish_container(user_id, access_token, creation_id)
print("published, post_id=" + post_id)
