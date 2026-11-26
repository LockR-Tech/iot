#!/usr/bin/env node
// Kiểm tra từ điển kiosk (không cần thư viện): `node scripts/check-i18n.mjs` hoặc `npm run check:i18n`.
// Lỗi (exit 1) khi:
//   - vi.js và en.js không cùng bộ khoá, hoặc có chuỗi rỗng;
//   - hai bản của một khoá dùng tham số {tên} khác nhau;
//   - src gọi t('…') / tStatic('…') / translate(lang, '…') với khoá không có trong vi.js;
//   - src có chuỗi '…' trông như khoá (đúng tiền tố nhóm của từ điển) mà từ điển không có.
// Khoá không thấy dùng ở đâu chỉ in cảnh báo.
import { readdirSync, readFileSync } from 'node:fs';
import { dirname, join, relative } from 'node:path';
import { fileURLToPath } from 'node:url';

const ROOT = join(dirname(fileURLToPath(import.meta.url)), '..');
const SRC = join(ROOT, 'src');
const I18N = join(SRC, 'i18n');

const { default: vi } = await import(new URL('../src/i18n/vi.js', import.meta.url));
const { default: en } = await import(new URL('../src/i18n/en.js', import.meta.url));

const errors = [];
const warnings = [];

// ─── 1. Cùng bộ khoá, không chuỗi rỗng, cùng tham số ───
const viKeys = new Set(Object.keys(vi));
const enKeys = new Set(Object.keys(en));
for (const k of viKeys) if (!enKeys.has(k)) errors.push(`en.js thiếu khoá "${k}"`);
for (const k of enKeys) if (!viKeys.has(k)) errors.push(`vi.js thiếu khoá "${k}" (có trong en.js)`);

const params = (str) => [...String(str).matchAll(/\{(\w+)\}/g)].map(m => m[1]).sort().join(',');
for (const k of viKeys) {
  for (const [name, dict] of [['vi', vi], ['en', en]]) {
    if (dict[k] != null && (typeof dict[k] !== 'string' || dict[k].trim() === '')) {
      errors.push(`${name}.js: khoá "${k}" rỗng hoặc không phải chuỗi`);
    }
  }
  if (enKeys.has(k) && params(vi[k]) !== params(en[k])) {
    errors.push(`khoá "${k}": tham số khác nhau — vi {${params(vi[k])}} / en {${params(en[k])}}`);
  }
}

// ─── 2. Khoá dùng trong src ───
function walk(dir) {
  return readdirSync(dir, { withFileTypes: true }).flatMap((e) => {
    const p = join(dir, e.name);
    if (e.isDirectory()) return p === I18N ? [] : walk(p);
    return /\.(jsx?|mjs)$/.test(e.name) ? [p] : [];
  });
}

const namespaces = new Set([...viKeys].map(k => k.split('.')[0]));
const CALL = /\b(?:t|tStatic)\(\s*(['"])([^'"\n]+)\1/g;
const TRANSLATE = /\btranslate\(\s*[^,()]+,\s*(['"])([^'"\n]+)\1/g;
const LITERAL = /(['"])([a-z][A-Za-z0-9]*(?:\.[A-Za-z0-9_]+)+)\1/g;
const used = new Set();

for (const file of walk(SRC)) {
  const rel = relative(ROOT, file).replaceAll('\\', '/');
  const text = readFileSync(file, 'utf8');
  for (const re of [CALL, TRANSLATE]) {
    for (const m of text.matchAll(re)) {
      used.add(m[2]);
      if (!viKeys.has(m[2])) errors.push(`${rel}: khoá "${m[2]}" không có trong vi.js`);
    }
  }
  for (const m of text.matchAll(LITERAL)) {
    const key = m[2];
    if (!namespaces.has(key.split('.')[0])) continue;
    used.add(key);
    if (!viKeys.has(key) && !errors.some(e => e.includes(`"${key}"`))) {
      errors.push(`${rel}: chuỗi "${key}" trông như khoá i18n nhưng không có trong vi.js`);
    }
  }
}

for (const k of viKeys) {
  const base = k.replace(/_one$/, '');
  if (!used.has(k) && !used.has(base)) warnings.push(`khoá "${k}" không thấy dùng trong src`);
}

for (const w of warnings) console.warn(`cảnh báo: ${w}`);
if (errors.length) {
  for (const e of errors) console.error(`LỖI: ${e}`);
  console.error(`\ncheck-i18n: ${errors.length} lỗi (${viKeys.size} khoá vi / ${enKeys.size} khoá en).`);
  process.exit(1);
}
console.log(`check-i18n: OK — ${viKeys.size} khoá, vi/en khớp nhau, mọi khoá dùng trong src đều có.`);
