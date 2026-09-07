"""
Threads 留言自動回覆機器人（阿翔｜宅男阿翔開箱測評）

流程：
1. 檢查 Telegram 是否有人按了之前送出的「核准／取消」按鈕 -> 核准就真的發布回覆
2. 掃描最近的貼文，抓出還沒處理過的新留言 -> 用 Claude Haiku 用阿翔的口吻草擬回覆
   -> 傳到 Telegram 給使用者看，附上「核准／取消」按鈕
3. 把處理進度（哪些留言看過了、還在等核准的草稿）存回 reply_state.json，
   讓下一次執行（GitHub Actions 排程）還記得進度

這支程式本身不會「自動」把回覆貼上 Threads —— 一定要使用者在 Telegram 按核准才會真的發布。
"""
import json, os, sys, time
from pathlib import Path
import requests

GRAPH_API_BASE = "https://graph.threads.net/v1.0"
ANTHROPIC_API_BASE = "https://api.anthropic.com/v1/messages"
TELEGRAM_API_BASE = "https://api.telegram.org/bot{token}/{method}"

CONTENT_DIR = Path(__file__).resolve().parent / "content"
STATE_PATH = CONTENT_DIR / "reply_state.json"

MAX_NEW_DRAFTS_PER_RUN = 5
MAX_RECENT_POSTS = 10

PERSONA_EXAMPLES = [
    "又加班到十點多\n捷運上整節車廂剩我一個\n這種時間點的月台超級安靜\n只想趕快回家躺平",
    "巷口那間麵攤吃了五年\n老闆還是記得我不要蔥\n這種被記住的感覺比料多還爽\n在地美食就是這樣",
    "等紅燈的時候旁邊機車籃裡有隻貓一直看我\n對到眼那瞬間覺得今天值得了\n快樂真的不用理由\n貓咪萬歲",
    "難得的假日什麼行程都沒排\n就這樣躺到中午才起床\n什麼都不做也是一種休息\n耍廢萬歲",
]

PERSONA_SYSTEM_PROMPT = (
    "你是「宅男阿翔｜開箱測評」，一個經營 Threads 的台灣上班族人設帳號，平常發文走道地台灣口語、"
    "生活化、有點自嘲又溫暖的風格。以下是幾則你平常發文的例子，感受一下語氣：\n\n"
    + "\n---\n".join(PERSONA_EXAMPLES)
    + "\n\n現在有人在你的貼文底下留言，請你用同樣的口吻草擬一則「回覆」。"
    "規則：\n"
    "1. 一定要用繁體中文、台灣道地口語，不要用中國大陸用語\n"
    "2. 簡短，1~2 句話就好，不要長篇大論\n"
    "3. 不要加 hashtag，emoji 最多用一個或完全不用\n"
    "4. 語氣自然、像在跟朋友聊天，不要客套或業配感\n"
    "5. 針對留言的內容具體回應，不要講空泛的場面話\n"
    "6. 只輸出回覆的文字本身，不要加任何說明、引號或前綴"
)


class BotError(Exception):
    pass


def load_state():
    if STATE_PATH.exists():
        return json.loads(STATE_PATH.read_text(encoding="utf-8"))
    return {"seen_reply_ids": [], "pending": {}, "next_pid": 1, "telegram_offset": 0}


def save_state(state):
    STATE_PATH.write_text(json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8")


def get_access_token():
    return os.environ.get("THREADS_USER_ACCESS_TOKEN") or _raise("missing env var THREADS_USER_ACCESS_TOKEN")


def _raise(msg):
    raise BotError(msg)


def _json_or_raise(resp, ctx):
    if resp.status_code != 200:
        raise BotError(f"{ctx} failed: {resp.status_code} {resp.text}")
    return resp.json()


def get_me(access_token):
    return _json_or_raise(
        requests.get(f"{GRAPH_API_BASE}/me", params={"fields": "id,username", "access_token": access_token}, timeout=30),
        "get user info",
    )


def list_recent_posts(user_id, access_token, limit=MAX_RECENT_POSTS):
    data = _json_or_raise(
        requests.get(
            f"{GRAPH_API_BASE}/{user_id}/threads",
            params={"fields": "id,timestamp", "limit": limit, "access_token": access_token},
            timeout=30,
        ),
        "list recent posts",
    )
    return data.get("data", [])


def list_replies(post_id, access_token):
    data = _json_or_raise(
        requests.get(
            f"{GRAPH_API_BASE}/{post_id}/replies",
            params={"fields": "id,text,username,timestamp", "access_token": access_token},
            timeout=30,
        ),
        f"list replies for {post_id}",
    )
    return data.get("data", [])


def create_reply_container(user_id, access_token, text, reply_to_id):
    return _json_or_raise(
        requests.post(
            f"{GRAPH_API_BASE}/{user_id}/threads",
            params={
                "text": text,
                "media_type": "TEXT",
                "reply_to_id": reply_to_id,
                "access_token": access_token,
            },
            timeout=30,
        ),
        "create reply container",
    )["id"]


def publish_container(user_id, access_token, creation_id):
    return _json_or_raise(
        requests.post(
            f"{GRAPH_API_BASE}/{user_id}/threads_publish",
            params={"creation_id": creation_id, "access_token": access_token},
            timeout=30,
        ),
        "publish reply",
    )["id"]


def publish_reply(user_id, access_token, text, reply_to_id):
    creation_id = create_reply_container(user_id, access_token, text, reply_to_id)
    time.sleep(5)
    return publish_container(user_id, access_token, creation_id)


def draft_reply_with_haiku(comment_text, commenter):
    api_key = os.environ.get("ANTHROPIC_API_KEY") or _raise("missing env var ANTHROPIC_API_KEY")
    resp = requests.post(
        ANTHROPIC_API_BASE,
        headers={
            "x-api-key": api_key,
            "anthropic-version": "2023-06-01",
            "content-type": "application/json",
        },
        json={
            "model": "claude-haiku-4-5",
            "max_tokens": 200,
            "system": PERSONA_SYSTEM_PROMPT,
            "messages": [
                {
                    "role": "user",
                    "content": f"網友 {commenter} 留言：「{comment_text}」\n\n請草擬一則回覆。",
                }
            ],
        },
        timeout=30,
    )
    if resp.status_code != 200:
        raise BotError(f"Haiku draft failed: {resp.status_code} {resp.text}")
    body = resp.json()
    return "".join(block.get("text", "") for block in body.get("content", [])).strip()


def telegram_call(method, payload):
    token = os.environ.get("TELEGRAM_BOT_TOKEN") or _raise("missing env var TELEGRAM_BOT_TOKEN")
    resp = requests.post(TELEGRAM_API_BASE.format(token=token, method=method), json=payload, timeout=30)
    if resp.status_code != 200:
        raise BotError(f"telegram {method} failed: {resp.status_code} {resp.text}")
    return resp.json()


def send_approval_request(pid, commenter, comment_text, draft):
    chat_id = os.environ.get("TELEGRAM_CHAT_ID") or _raise("missing env var TELEGRAM_CHAT_ID")
    text = (
        f"💬 新留言（來自 {commenter}）：\n{comment_text}\n\n"
        f"✍️ 阿翔草擬回覆：\n{draft}\n\n是否發布這則回覆？"
    )
    telegram_call(
        "sendMessage",
        {
            "chat_id": chat_id,
            "text": text,
            "reply_markup": {
                "inline_keyboard": [
                    [
                        {"text": "✅ 核准發布", "callback_data": f"ar:{pid}"},
                        {"text": "❌ 取消", "callback_data": f"rj:{pid}"},
                    ]
                ]
            },
        },
    )


def edit_message(chat_id, message_id, new_text):
    try:
        telegram_call("editMessageText", {"chat_id": chat_id, "message_id": message_id, "text": new_text})
    except BotError as e:
        print(f"(warn) failed to edit telegram message: {e}")


def answer_callback(callback_query_id, text):
    try:
        telegram_call("answerCallbackQuery", {"callback_query_id": callback_query_id, "text": text})
    except BotError as e:
        print(f"(warn) failed to answer callback query: {e}")


def process_telegram_actions(state, user_id, access_token):
    token = os.environ.get("TELEGRAM_BOT_TOKEN") or _raise("missing env var TELEGRAM_BOT_TOKEN")
    resp = requests.get(
        TELEGRAM_API_BASE.format(token=token, method="getUpdates"),
        params={"offset": state.get("telegram_offset", 0), "timeout": 0},
        timeout=30,
    )
    if resp.status_code != 200:
        print(f"(warn) getUpdates failed: {resp.status_code} {resp.text}")
        return
    updates = resp.json().get("result", [])
    for update in updates:
        state["telegram_offset"] = update["update_id"] + 1
        cq = update.get("callback_query")
        if not cq or "data" not in cq:
            continue
        data = cq["data"]
        message = cq.get("message") or {}
        chat_id = (message.get("chat") or {}).get("id")
        message_id = message.get("message_id")
        if ":" not in data:
            continue
        action, pid = data.split(":", 1)
        pending = state["pending"].get(pid)
        if not pending:
            answer_callback(cq["id"], "這則已經處理過了")
            continue
        if action == "ar":
            try:
                publish_reply(user_id, access_token, pending["draft"], pending["reply_id"])
                answer_callback(cq["id"], "已發布！")
                if chat_id and message_id:
                    edit_message(chat_id, message_id, f"✅ 已發布回覆：\n{pending['draft']}")
            except BotError as e:
                answer_callback(cq["id"], "發布失敗")
                if chat_id and message_id:
                    edit_message(chat_id, message_id, f"⚠️ 發布失敗：{e}\n\n草稿內容：\n{pending['draft']}")
            state["pending"].pop(pid, None)
        elif action == "rj":
            answer_callback(cq["id"], "已取消")
            if chat_id and message_id:
                edit_message(chat_id, message_id, f"❌ 已取消這則回覆：\n{pending['draft']}")
            state["pending"].pop(pid, None)


def draft_new_replies(state, user_id, access_token, own_username):
    seen = set(state["seen_reply_ids"])
    new_count = 0
    posts = list_recent_posts(user_id, access_token)
    for post in posts:
        if new_count >= MAX_NEW_DRAFTS_PER_RUN:
            break
        try:
            replies = list_replies(post["id"], access_token)
        except BotError as e:
            print(f"(warn) {e}")
            continue
        for r in replies:
            rid = r.get("id")
            if not rid or rid in seen:
                continue
            username = (r.get("username") or "").strip()
            text = (r.get("text") or "").strip()
            if not text or username == own_username:
                seen.add(rid)
                state["seen_reply_ids"].append(rid)
                continue
            if new_count >= MAX_NEW_DRAFTS_PER_RUN:
                continue
            try:
                draft = draft_reply_with_haiku(text, username or "網友")
            except BotError as e:
                print(f"(warn) failed to draft reply for {rid}: {e}")
                continue
            pid = f"p{state['next_pid']}"
            state["next_pid"] += 1
            state["pending"][pid] = {
                "reply_id": rid,
                "post_id": post["id"],
                "username": username,
                "comment": text,
                "draft": draft,
            }
            try:
                send_approval_request(pid, username or "網友", text, draft)
            except BotError as e:
                print(f"(warn) failed to send telegram approval for {rid}: {e}")
                state["pending"].pop(pid, None)
                continue
            seen.add(rid)
            state["seen_reply_ids"].append(rid)
            new_count += 1
            print(f"drafted reply for {rid} (from {username}): {draft}")


def main():
    state = load_state()
    access_token = get_access_token()
    me = get_me(access_token)
    user_id, own_username = me["id"], me.get("username", "")

    try:
        process_telegram_actions(state, user_id, access_token)
    except BotError as e:
        print(f"(warn) process_telegram_actions failed: {e}")

    try:
        draft_new_replies(state, user_id, access_token, own_username)
    except BotError as e:
        print(f"(warn) draft_new_replies failed: {e}")

    save_state(state)
    print(f"done. pending approvals: {len(state['pending'])}, total seen replies: {len(state['seen_reply_ids'])}")


if __name__ == "__main__":
    main()
