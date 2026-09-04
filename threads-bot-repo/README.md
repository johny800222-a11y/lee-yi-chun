# Threads 自動發文機器人（@johny800333）

用 GitHub Actions 排程 + Meta Threads Graph API，讓「加班日常」等 6 種日常貼文
就算你電腦沒開機、瀏覽器沒連線，也會照排程自動發到 Threads。

## 一、把這些檔案 push 上你的 repo

repo：https://github.com/johny800222-a11y/lee-yi-chun

```bash
git clone https://github.com/johny800222-a11y/lee-yi-chun.git
# 把這個資料夾裡的 scripts/、.github/、README.md 複製進 clone 下來的資料夾
cd lee-yi-chun
git add .
git commit -m "設定 Threads 自動發文排程"
git push
```

（如果這個 repo 裡已經有其他無關的內容，把 `scripts/` 和 `.github/workflows/` 這兩個資料夾放進去、不要覆蓋到既有檔案就好）

## 二、設定 GitHub Secrets（存放權杖，不會外洩）

到 repo 頁面 → Settings → Secrets and variables → Actions → New repository secret，新增兩組：

| Name | Value |
|---|---|
| `THREADS_USER_ACCESS_TOKEN` | 你剛才在 Meta 開發人員後台產生的那組長效權杖 |
| `THREADS_APP_SECRET` | `4b17eb06f20483a9af5021d9d9b1d5a3`（App Secret，備用於未來 token 刷新） |

## 三、確認排程時間（已依你的規則設定好）

| Workflow 檔案 | 貼文類型 | 排程（台灣時間） |
|---|---|---|
| post-overtime.yml | 加班日常 | 週一三五 22:30 |
| post-food.yml | 巷口小店偶遇美食 | 週二四 18:30 |
| post-surprise.yml | 路上遇到的小驚喜 | 週一三五 15:00 |
| post-weekend.yml | 週末耍廢 | 週六日 14:00 |
| post-workmeltdown.yml | 小小的職場崩潰又復活 | 週二四五 12:30 |
| post-latenight.yml | 深夜獨白 | 週二、六 23:30 |

GitHub Actions 的 cron 是 UTC 時間，檔案裡已經幫你算好轉換（台灣時間 -8 小時），你不用再手動改，除非要調整時段。

注意：GitHub Actions 的排程時間不保證分秒不差，官方說明可能會有幾分鐘到十幾分鐘的延遲，尤其平台忙碌時，這是 GitHub 平台本身的限制，不是設定錯誤。

## 四、測試（不會真的發文）

push 完成後，到 repo 的 Actions 分頁，任選一個 workflow（例如「Threads - 加班日常」），
點右上角 "Run workflow" 手動觸發一次，看 log 有沒有成功印出「已發佈，post_id=...」。

也可以先在自己電腦本機測試（不會真的發文）：
```bash
cd scripts
pip install -r requirements.txt
THREADS_USER_ACCESS_TOKEN=你的權杖 python3 publish.py --type overtime --dry-run
```

## 五、權杖到期怎麼辦

Threads 長效權杖（long-lived access token）效期約 60 天，過期後這些排程會失敗。
目前的權杖是 2026-09-04 產生的，建議在 **2026-11-03 前**手動刷新一次：

1. 回到 Meta 開發人員後台（AhXiang Content Publisher → 使用案例 → 存取 Threads API → 設定 → 用戶權杖產生器）
2. 對 johny800333 重新點「產生存取權杖」，取得新權杖
3. 到 GitHub repo 的 Secrets 頁面，把 `THREADS_USER_ACCESS_TOKEN` 更新成新的值

（未來如果想要全自動刷新，可以再另外設計一個定期呼叫 Threads `refresh_access_token` API 並自動更新 GitHub Secret 的 workflow，但這需要額外的 GitHub PAT 權限，先手動刷新即可，先求穩。）

## 六、之後要新增/修改貼文內容

編輯 `scripts/content/daily_life_posts.json`，每個類型（overtime/food/surprise/weekend/workmeltdown/latenight）
底下是一個陣列，可以放好幾篇文字，程式每次會隨機挑一篇發，避免每次都發一樣的內容。目前每個類型都只有 1 篇（跟原本草稿一樣），建議之後多補幾篇增加變化。

商品開箱貼文（`scripts/content/product_posts.json`，需要另外建立並填入 5 篇已發過的商品文案 + 分潤連結）目前沒有排程，因為這批商品已經手動發過一輪了；之後要排新商品貼文可以再跟我說，我幫你加。
