const router = require('express').Router();
const axios = require('axios');
const crypto = require('crypto');
const dayjs = require('dayjs');
const supabase = require('../db');

const EINVOICE_API = 'https://api.einvoice.nat.gov.tw/PB2CAPIVAN/invServ/InvServ';

function buildInvoiceParams(action, extra = {}) {
  const params = {
    version: '0.5',
    action,
    appID: process.env.EINVOICE_APP_ID,
    timeStamp: Math.floor(Date.now() / 1000).toString(),
    ...extra
  };
  // 簽章：依財政部規範 HMAC-SHA256
  const sortedKeys = Object.keys(params).sort();
  const queryString = sortedKeys.map(k => `${k}=${params[k]}`).join('&');
  params.signature = crypto
    .createHmac('sha256', process.env.EINVOICE_API_KEY)
    .update(queryString)
    .digest('base64');
  return new URLSearchParams(params).toString();
}

// 解析財政部民國年月 → 西元
function parseInvoiceDate(rocDateStr) {
  // 格式 1130501 → 民國113年05月01日
  const year = parseInt(rocDateStr.substring(0, 3)) + 1911;
  const month = rocDateStr.substring(3, 5);
  const day = rocDateStr.substring(5, 7);
  return `${year}-${month}-${day}`;
}

function guessCategory(title = '') {
  if (/超市|全聯|家樂福|大潤發|量販|便利|711|全家|萊爾富|ok便利/.test(title)) return '日用品';
  if (/餐|食|早餐|午餐|晚餐|飲|麵|飯|cafe|咖啡|小吃|火鍋|燒烤|便當|壽司/.test(title)) return '餐飲';
  if (/交通|捷運|公車|計程車|uber|油站|停車|高鐵|台鐵|客運/.test(title)) return '交通';
  if (/藥局|診所|醫院|藥妝|康是美|屈臣氏/.test(title)) return '醫療';
  if (/電影|KTV|遊樂|健身|娛樂|遊戲/.test(title)) return '娛樂';
  if (/momo|蝦皮|pchome|蘋果|3c|電器/.test(title)) return '購物';
  return '其他';
}

// POST /api/invoice/sync  body: { person: '群' | '萱', months: 1 }
router.post('/sync', async (req, res) => {
  const { person, months = 1 } = req.body;
  if (!['群', '萱'].includes(person)) {
    return res.status(400).json({ error: '花費人必須是「群」或「萱」' });
  }

  const carrierBarcode = process.env[`CARRIER_BARCODE_${person}`];
  if (!carrierBarcode) {
    return res.status(400).json({ error: `未設定 ${person} 的條碼載具` });
  }

  const endDate = dayjs().format('YYYY-MM-DD');
  const startDate = dayjs().subtract(months, 'month').format('YYYY-MM-DD');

  try {
    // Step 1: 取得載具明細
    const headerParams = buildInvoiceParams('carrierInvChk', {
      cardType: '3J0002',
      cardNo: carrierBarcode,
      expTimeStamp: Math.floor(Date.now() / 1000 + 3600).toString(),
      startDate: startDate.replace(/-/g, '/'),
      endDate: endDate.replace(/-/g, '/'),
      onlyWinningInv: 'N',
      uuid: crypto.randomUUID().replace(/-/g, '')
    });

    const headerRes = await axios.post(EINVOICE_API, headerParams, {
      headers: { 'Content-Type': 'application/x-www-form-urlencoded' }
    });

    const invoiceList = headerRes.data?.details || [];
    const inserted = [];

    for (const inv of invoiceList) {
      const invNo = inv.invNum;
      const invDate = parseInvoiceDate(inv.invDate);
      const amount = parseFloat(inv.amount || 0);
      const sellerName = inv.sellerName || '';

      // 避免重複匯入
      const { data: existing } = await supabase
        .from('expenses')
        .select('id')
        .eq('invoice_no', invNo)
        .single();

      if (existing) continue;

      const { data } = await supabase.from('expenses').insert([{
        date: invDate,
        person,
        category: guessCategory(sellerName.toLowerCase()),
        description: sellerName,
        amount,
        source: 'invoice',
        invoice_no: invNo
      }]).select().single();

      if (data) inserted.push(data);
    }

    res.json({ synced: inserted.length, invoices: inserted });
  } catch (err) {
    console.error('Invoice sync error:', err.message);
    res.status(500).json({ error: err.message });
  }
});

module.exports = router;
