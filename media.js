// Velora: фото через Google, при ошибке через Pollinations; видео через Pollinations.
// Подключение: const { makeMedia } = require('./media');
// Переменные на Render: GOOGLE_API_KEY, POLLINATIONS_KEY, (необязательно) GOOGLE_IMAGE_MODEL, MEDIA_DIR
const fs = require('fs'), path = require('path');
const MEDIA_DIR = process.env.MEDIA_DIR || path.join(__dirname, 'media');
fs.mkdirSync(MEDIA_DIR, { recursive: true });

const SIZES = { '1:1': [1024, 1024], '3:4': [768, 1024], '16:9': [1280, 720], '9:16': [720, 1280] };
const GKEY = () => process.env.GOOGLE_API_KEY, PKEY = () => process.env.POLLINATIONS_KEY;
const GMODEL = () => process.env.GOOGLE_IMAGE_MODEL || 'gemini-2.5-flash-image';

function save(id, ext, buf) { const n = id + '.' + ext; fs.writeFileSync(path.join(MEDIA_DIR, n), buf); return '/media/' + n; }
async function ok(r, who) { if (!r.ok) throw new Error(who + ' ' + r.status + ' ' + (await r.text()).slice(0, 200)); return r; }

async function googleImage(id, { prompt, aspect, image }) {
  if (!GKEY()) throw new Error('нет GOOGLE_API_KEY');
  const parts = [{ text: prompt }];
  const m = image && /^data:(image\/[\w+.-]+);base64,(.+)$/.exec(image);
  if (m) parts.unshift({ inlineData: { mimeType: m[1], data: m[2] } });          // правка фото
  const r = await ok(await fetch(
    `https://generativelanguage.googleapis.com/v1beta/models/${GMODEL()}:generateContent`,
    { method: 'POST', headers: { 'Content-Type': 'application/json', 'x-goog-api-key': GKEY() },
      body: JSON.stringify({ contents: [{ parts }],
        generationConfig: { responseModalities: ['IMAGE'], imageConfig: { aspectRatio: aspect || '3:4' } } }) }), 'Google');
  const j = await r.json();
  const p = ((j.candidates || [])[0] || {}).content;
  const img = p && p.parts.map(x => x.inlineData || x.inline_data).find(Boolean);
  if (!img) throw new Error('Google не вернул картинку');
  const mime = img.mimeType || img.mime_type || 'image/png';
  return save(id, mime.includes('jpeg') ? 'jpg' : 'png', Buffer.from(img.data, 'base64'));
}

async function pollImage(id, { prompt, aspect }) {
  const [w, h] = SIZES[aspect] || SIZES['3:4'];
  const q = new URLSearchParams({ width: w, height: h, nologo: 'true', seed: Math.floor(Math.random() * 1e6) });
  const r = await ok(await fetch(`https://gen.pollinations.ai/image/${encodeURIComponent(prompt)}?${q}`,
    { headers: PKEY() ? { Authorization: 'Bearer ' + PKEY() } : {} }), 'Pollinations');
  return save(id, 'jpg', Buffer.from(await r.arrayBuffer()));
}

async function pollVideo(id, { prompt, aspect, duration }) {
  if (!PKEY()) throw new Error('нет POLLINATIONS_KEY');
  const q = new URLSearchParams({ aspectRatio: aspect || '9:16', duration: String(duration || 4) });
  const r = await ok(await fetch(`https://gen.pollinations.ai/video/${encodeURIComponent(prompt)}?${q}`,
    { headers: { Authorization: 'Bearer ' + PKEY() } }), 'Pollinations видео');
  return save(id, 'mp4', Buffer.from(await r.arrayBuffer()));
}

// job: { id, kind: 'image'|'edit'|'video', prompt, aspect, duration, image }
async function makeMedia(job) {
  if (job.kind === 'video') return { kind: 'video', url: await pollVideo(job.id, job) };
  try { return { kind: 'image', url: await googleImage(job.id, job) }; }
  catch (e) {
    console.error('Google фото не вышло, иду в Pollinations:', e.message);
    if (job.kind === 'edit') throw e;                    // правку Pollinations не умеет
    return { kind: 'image', url: await pollImage(job.id, job) };
  }
}
module.exports = { makeMedia };
