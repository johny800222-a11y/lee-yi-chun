const router = require('express').Router();
const supabase = require('../db');
const dayjs = require('dayjs');

// GET /api/expenses?year=2026&month=5
router.get('/', async (req, res) => {
  const { year, month } = req.query;
  const start = dayjs(`${year}-${month}-01`).format('YYYY-MM-DD');
  const end = dayjs(start).endOf('month').format('YYYY-MM-DD');

  const { data, error } = await supabase
    .from('expenses')
    .select('*')
    .gte('date', start)
    .lte('date', end)
    .order('date', { ascending: false });

  if (error) return res.status(500).json({ error: error.message });
  res.json(data);
});

// POST /api/expenses
router.post('/', async (req, res) => {
  const { date, person, category, description, amount, source } = req.body;

  if (!['群', '萱'].includes(person)) {
    return res.status(400).json({ error: '花費人必須是「群」或「萱」' });
  }
  if (!amount || isNaN(amount) || Number(amount) <= 0) {
    return res.status(400).json({ error: '金額格式錯誤' });
  }

  const { data, error } = await supabase.from('expenses').insert([{
    date: date || dayjs().format('YYYY-MM-DD'),
    person,
    category: category || '其他',
    description: description || '',
    amount: Number(amount),
    source: source || 'manual'
  }]).select().single();

  if (error) return res.status(500).json({ error: error.message });
  res.json(data);
});

// DELETE /api/expenses/:id
router.delete('/:id', async (req, res) => {
  const { error } = await supabase
    .from('expenses')
    .delete()
    .eq('id', req.params.id);

  if (error) return res.status(500).json({ error: error.message });
  res.json({ ok: true });
});

// PUT /api/expenses/:id
router.put('/:id', async (req, res) => {
  const { date, person, category, description, amount } = req.body;
  const { data, error } = await supabase
    .from('expenses')
    .update({ date, person, category, description, amount: Number(amount) })
    .eq('id', req.params.id)
    .select().single();

  if (error) return res.status(500).json({ error: error.message });
  res.json(data);
});

module.exports = router;
