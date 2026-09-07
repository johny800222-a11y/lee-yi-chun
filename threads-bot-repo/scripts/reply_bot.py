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
from datetime import datetime, timezone
from pathlib import Path
import requests

GRAPH_API_BASE = "https://graph.threads.net/v1.0"
ANTHROPIC_API_BASE = "https://api.anthropic.com/v1/messages"
TELEGRAM_API_BASE = "https://api.telegram.org/bot{token}/{method}"

CONTENT_DIR = Path(__file__).resolve().parent / "content"
STATE_PATH = CONTENT_DIR / "reply_state.json"

MAX_NEW_DRAFTS_PER_RUN = 5
MAX_RECENT_POSTS = 10

GROWTH_KEYWORDS = ["互追", "海巡", "流量密碼"]
MAX_GROWTH_PER_DAY = 10
GROWTH_SEARCH_LIMIT = 10

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

GROWTH_PERSONA_SYSTEM_PROMPT = (
    "你是「宅男阿翔｜開箱測評」，一個剛開始經營 Threads、想要漲粉互相認識的台灣上班族人設帳號，"
    "平常說話走道地台灣口語、親切自然。以下是幾則你平常發文的例子，感受一下語氣：\n\n"
    + "\n---\n".join(PERSONA_EXAMPLES)
    + "\n\n現在你看到別人發的一篇貼文，內容跟「互追」「海巡」「漲粉」有關，"
    "你想在底下留言，跟對方打個招呼、順便讓對方注意到你，之後你會自己手動追蹤對方。"
    "規則：\n"
    "1. 一定要用繁體中文、台灣道地口語，不要用中國大陸用語\n"
    "2. 簡短自然，1 句話就好，像真人隨手留言，不要長篇大論\n"
    "3. 絕對不要用『已追』『互追不退』這種罐頭式、一看就是機器人的制式留言\n"
    "4. 針對這篇貼文實際寫的內容具體回應一下（例如提到的目標、心情），不要講空泛場面話\n"
    "5. 可以自然帶出『也來我這邊看看』『互相支持一下』這種語氣，但不要生硬置入\n"
    "6. emoji 最多一個或不用\n"
    "7. 只輸出留言的文字本身，不要加任何說明、引號或前綴"
)


class BotError(Exception):
    pass


def default_state():
    return {
        "seen_reply_ids": [],
        "pending": {},
        "next_pid": 1,
        "telegram_offset": 0,
        "growth": {"seen_post_ids": [], "pending": {}, "next_gid": 1, "date": "", "count_today": 0},
    }


def load_state():
    if STATE_PATH.exists():
        state = json.loads(STATE_PATH.read_text(encoding="utf-8"))
        state.setdefault("growth", default_state()["growth"])
        return state
    return default_state()


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


def keyword_search(keyword, access_token, limit=GROWTH_SEARCH_LIMIT):
    data = _json_or_raise(
        requests.get(
            f"{GRAPH_API_BASE}/keyword_search",
            params={
                "q": keyword,
                "search_type": "RECENT",
                "fields": "id,text,permalink,username,timestamp,is_reply",
                "limit": limit,
                "access_token": access_token,
            },
            timeout=30,
        ),
        f"keyword search for {keyword}",
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


def draft_growth_comment_with_haiku(post_text, author):
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
            "system": GROWTH_PERSONA_SYSTEM_PROMPT,
            "messages": [
                {
                    "role": "user",
                    "content": f"{author} 發的貼文內容：「{post_text}」\n\n請草擬一則留言。",
                }
            ],
        },
        timeout=30,
    )
    if resp.status_code != 200:
        raise BotError(f"Haiku growth draft failed: {resp.status_code} {resp.text}")
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


def send_growth_approval_request(gid, author, post_text, permalink, draft):
    chat_id = os.environ.get("TELEGRAM_CHAT_ID") or _raise("missing env var TELEGRAM_CHAT_ID")
    text = (
        f"🔍 海巡發現一篇互追／漲粉貼文（來自 {author}）：\n{post_text}\n\n"
        f"🔗 {permalink}\n\n"
        f"✍️ 阿翔草擬留言：\n{draft}\n\n"
        "核准的話會自動幫你留言，留言後請記得手動點連結追蹤對方（目前 API 無法自動追蹤）。是否核准？"
    )
    telegram_call(
        "sendMessage",
        {
            "chat_id": chat_id,
            "text": text,
            "reply_markup": {
                "inline_keyboard": [
                    [
                        {"text": "✅ 核准留言", "callback_data": f"gar:{gid}"},
                        {"text": "❌ 略過", "callback_data": f"grj:{gid}"},
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
        action, key = data.split(":", 1)

        if action in ("ar", "rj"):
            pending = state["pending"].get(key)
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
                state["pending"].pop(key, None)
            else:
                answer_callback(cq["id"], "已取消")
                if chat_id and message_id:
                    edit_message(chat_id, message_id, f"❌ 已取消這則回覆：\n{pending['draft']}")
                state["pending"].pop(key, None)

        elif action in ("gar", "grj"):
            pending = state["growth"]["pending"].get(key)
            if not pending:
                answer_callback(cq["id"], "這則已經處理過了")
                continue
            if action == "gar":
                try:
                    publish_reply(user_id, access_token, pending["draft"], pending["post_id"])
                    answer_callback(cq["id"], "已留言！記得手動追蹤對方")
                    if chat_id and message_id:
                        edit_message(
                            chat_id,
                            message_id,
                            f"✅ 已留言：\n{pending['draft']}\n\n記得手動點連結追蹤對方：\n{pending['permalink']}",
                        )
                except BotError as e:
                    answer_callback(cq["id"], "留言失敗")
                    if chat_id and message_id:
                        edit_message(chat_id, message_id, f"⚠️ 留言失敗：{e}\n\n草稿內容：\n{pending['draft']}")
                state["growth"]["pending"].pop(key, None)
            else:
                answer_callback(cq["id"], "已略過")
                if chat_id and message_id:
                    edit_message(chat_id, message_id, f"❌ 已略過這篇：\n{pending['draft']}")
                state["growth"]["pending"].pop(key, None)


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


def draft_growth_patrol(state, user_id, access_token, own_username):
    growth = state["growth"]
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    if growth.get("date") != today:
        growth["date"] = today
        growth["count_today"] = 0

    if growth["count_today"] >= MAX_GROWTH_PER_DAY:
        print(f"growth patrol: already hit today's cap ({MAX_GROWTH_PER_DAY}), skipping")
        return

    seen = set(growth["seen_post_ids"])
    for keyword in GROWTH_KEYWORDS:
        if growth["count_today"] >= MAX_GROWTH_PER_DAY:
            break
        try:
            posts = keyword_search(keyword, access_token)
        except BotError as e:
            print(f"(warn) keyword search failed for {keyword}: {e}")
            continue
        for post in posts:
            if growth["count_today"] >= MAX_GROWTH_PER_DAY:
                break
            pid = post.get("id")
            if not pid or pid in seen:
                continue
            username = (post.get("username") or "").strip()
            text = (post.get("text") or "").strip()
            permalink = post.get("permalink") or ""
            if not text or username == own_username:
                seen.add(pid)
                growth["seen_post_ids"].append(pid)
                continue
            try:
                draft = draft_growth_comment_with_haiku(text, username or "網友")
            except BotError as e:
                print(f"(warn) failed to draft growth comment for {pid}: {e}")
                continue
            gid = f"g{growth['next_gid']}"
            growth["next_gid"] += 1
            growth["pending"][gid] = {
                "post_id": pid,
                "username": username,
                "post_text": text,
                "permalink": permalink,
                "draft": draft,
            }
            try:
                send_growth_approval_request(gid, username or "網友", text, permalink, draft)
            except BotError as e:
                print(f"(warn) failed to send telegram approval for growth post {pid}: {e}")
                growth["pending"].pop(gid, None)
                continue
            seen.add(pid)
            growth["seen_post_ids"].append(pid)
            growth["count_today"] += 1
            print(f"drafted growth comment for {pid} (keyword={keyword}, from {username}): {draft}")


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

    try:
        draft_growth_patrol(state, user_id, access_token, own_username)
    except BotError as e:
        print(f"(warn) draft_growth_patrol failed: {e}")

    save_state(state)
    print(
        f"done. pending reply approvals: {len(state['pending'])}, total seen replies: {len(state['seen_reply_ids'])}, "
        f"pending growth approvals: {len(state['growth']['pending'])}, growth comments today: {state['growth']['count_today']}"
    )


if __name__ == "__main__":
    main()
