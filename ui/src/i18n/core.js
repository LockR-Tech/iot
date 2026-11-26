import { cloneElement, isValidElement } from 'react';
import vi from './vi.js';
import en from './en.js';

// i18n tự viết cho kiosk (không thêm thư viện: Pi chạy `npm ci` trên ổ chậm mỗi khi package-lock đổi).
// Tiếng Việt mặc định; khách chọn EN trên màn chính, goHome() trả về VI cho khách sau.
// Không lưu localStorage: Chromium tải lại trang (Pi tự cập nhật, mất điện) thì kiosk luôn mở bằng tiếng Việt.
export const DICTS = { vi, en };
export const LANGS = ['vi', 'en'];
export const DEFAULT_LANG = 'vi';

let current = DEFAULT_LANG;
const listeners = new Set();

export function getLang() {
  return current;
}

export function setLang(lang) {
  const next = LANGS.includes(lang) ? lang : DEFAULT_LANG;
  if (next === current) return;
  current = next;
  if (typeof document !== 'undefined') document.documentElement.lang = next;
  listeners.forEach(fn => fn(next));
}

/** Đăng ký nghe đổi ngôn ngữ (useSyncExternalStore, firebase.js). Trả hàm huỷ đăng ký. */
export function onLangChange(fn) {
  listeners.add(fn);
  return () => { listeners.delete(fn); };
}

// `{n}` / `{count}` là số ⇒ dùng biến thể `<key>_one` khi bằng 1 (tiếng Anh cần "1 hour" / "2 hours").
function pick(dict, key, params) {
  const n = typeof params?.count === 'number' ? params.count : params?.n;
  if (n === 1 && dict[`${key}_one`] != null) return dict[`${key}_one`];
  return dict[key];
}

/**
 * Chuỗi của `key` theo `lang`, thay `{tên}` bằng `params.tên`. Thiếu khoá ở bản tiếng Anh ⇒ lấy tiếng
 * Việt; thiếu cả hai ⇒ chuỗi rỗng (không bao giờ hiện khoá thô cho khách). Tham số là phần tử React
 * (vd `<strong>…</strong>`) ⇒ trả mảng để JSX giữ nguyên thẻ như trước khi tách chữ.
 */
export function translate(lang, key, params) {
  let text = pick(DICTS[lang] || DICTS[DEFAULT_LANG], key, params);
  if (text == null) text = pick(DICTS[DEFAULT_LANG], key, params);
  if (text == null) {
    console.warn(`[i18n] thiếu khoá "${key}"`);
    return '';
  }
  if (!params) return text;
  const parts = text.split(/\{(\w+)\}/);
  const value = (name) => (params[name] == null ? '' : params[name]);
  let rich = false;
  for (let i = 1; i < parts.length; i += 2) {
    if (isValidElement(value(parts[i]))) rich = true;
  }
  if (!rich) return parts.map((p, i) => (i % 2 ? String(value(p)) : p)).join('');
  return parts
    .map((p, i) => {
      if (!(i % 2)) return p;
      const v = value(p);
      return isValidElement(v) ? cloneElement(v, { key: `i18n-${i}` }) : String(v);
    })
    .filter(p => p !== '');
}

/** Cho code ngoài component (api.js, firebase.js, hằng số cấp module): dịch theo ngôn ngữ hiện tại. */
export function tStatic(key, params) {
  return translate(current, key, params);
}

/** Locale cho đồng hồ / ngày tháng: vi-VN hoặc en-GB (24 giờ, ngày trước tháng như ở Việt Nam). */
export function localeOf(lang) {
  return lang === 'en' ? 'en-GB' : 'vi-VN';
}

/** Số tiền VND: tiếng Việt "25.000đ" (như trước), tiếng Anh "25,000 VND". */
export function formatMoney(amount, lang = current) {
  const n = Number(amount) || 0;
  return lang === 'en'
    ? `${new Intl.NumberFormat('en-US').format(n)} VND`
    : `${new Intl.NumberFormat('vi-VN').format(n)}đ`;
}
