const router = require('express').Router();
const { Client, middleware, validateSignature } = require('@line/bot-sdk');
const dayjs = require('dayjs');
const supabase = require('../db');

const lineConfig = {
  channelSecret: process.env.LINE_CHANNEL_SECRET,
  channelAccessToken: process.env.LINE_CHANNEL_ACCESS_TOKEN
};

const client = new Client(lineConfig);

const CATEGORIES = ['餐飲', '交通', '購物', '日用品', '娛樂', '醫療', '其他'];

/*
  支援格式（LINE 訊息）：
  群 早餐 85
  萱 超市 日用品 320
  群 計程車 交通 150 2026-05-10
  幫助 / help → 顯示使用說明
*/
function parseMessage(text) {
  const t = text.trim();

  if (/^(幫助|help|說明|\?)$/i.test(t)) return { type: 'help' };

  // 匹配 person (群|萱) + 描述 + [分類] + 金額 + [日期]
  const personMatch = t.match(/^(群|萱)\s+(.+)/);
  if (!personMatch) return null;

  const person = personMatch[1];
  const rest = personMatch[2];

  // 從 rest 解析：最後一個數字當金額，前面是描述，可能含分類與日期
  const dateMatch = rest.match(/(\d{4}-\d{2}-\d{2})/);
  const amountMatch = rest.match(/(\d+(?:\.\d+)?)/g);
  if (!amountMatch) return null;

  const amount = parseFloat(amountMatch[amountMatch.length - 1]);
  const date = dateMatch ? dateMatch[1] : dayjs().format('YYYY-MM-DD');

  // 去掉金額和日期後剩下描述+分類
  let remaining = rest
    .replace(date, '')
    .replace(amount.toString(), '')
    .trim();

  let category = '其他';
  for (const cat of CATEGORIES) {
    if (remaining.includes(cat)) {
      category = cat;
      remaining = remaining.replace(cat, '').trim();
      break;
    }
  }

  // 剩下的當描述，若只剩空白則用分類替代
  const description = remaining.replace(/\s+/g, ' ').trim() || category;

  return { type: 'expense', person, category, description, amount, date };
}

function helpText() {
  return `📒 記帳小幫手使用說明

格式：[花費人] [描述] [分類(選填)] [金額] [日期(選填)]

✅ 範例：
群 早餐 85
萱 全聯 日用品 320
群 計程車 交通 150 2026-05-10

📂 分類：餐飲、交通、購物、日用品、娛樂、醫療、其他
👥 花費人：群、萱`;
}

router.post('/', async (req, res) => {
  const signature = req.headers['x-line-signature'];
  if (!validateSignature(req.body, lineConfig.channelSecret, signature)) {
    return res.status(401).send('Invalid signature');
  }

  const body = JSON.parse(req.body.toString());
  res.sendStatus(200); // 先回 200，LINE 要求 5 秒內回應

  for (const event of body.events || []) {
    if (event.type !== 'message' || event.message.type !== 'text') continue;

    const replyToken = event.replyToken;
    const text = event.message.text;
    const parsed = parseMessage(text);

    if (!parsed) {
      await client.replyMessage(replyToken, {
        type: 'text',
        text: '格式錯誤 😅\n請輸入「幫助」查看使用說明'
      });
      continue;
    }

    if (parsed.type === 'help') {
      await client.replyMessage(replyToken, { type: 'text', text: helpText() });
      continue;
    }

    const { person, category, description, amount, date } = parsed;
    const { data, error } = await supabase.from('expenses').insert([{
      date, person, category, description, amount, source: 'line'
    }]).select().single();

    if (error) {
      await client.replyMessage(replyToken, { type: 'text', text: `❌ 儲存失敗：${error.message}` });
    } else {
      await client.replyMessage(replyToken, {
        type: 'text',
        text: `✅ 已記錄！\n👤 ${person}　📂 ${category}\n📝 ${description}\n💰 $${amount}\n📅 ${date}`
      });
    }
  }
});

module.exports = router;
