import { createContext, useContext, useMemo, useSyncExternalStore } from 'react';
import { getLang, setLang, onLangChange, translate, localeOf, formatMoney } from './core.js';

export { getLang, setLang, tStatic, formatMoney, localeOf } from './core.js';

const LangContext = createContext(null);

function bundle(lang) {
  return {
    lang,
    setLang,
    t: (key, params) => translate(lang, key, params),
    locale: localeOf(lang),
    money: (amount) => formatMoney(amount, lang),
  };
}

/** Bọc ứng dụng: mọi component dùng useT() vẽ lại khi đổi ngôn ngữ. */
export function LangProvider({ children }) {
  const lang = useSyncExternalStore(onLangChange, getLang, getLang);
  const value = useMemo(() => bundle(lang), [lang]);
  return <LangContext.Provider value={value}>{children}</LangContext.Provider>;
}

/** `{ t, lang, setLang, locale, money }` — `t(key, { tên: giá trị })`. Ngoài LangProvider thì đọc ngôn ngữ hiện tại. */
export function useT() {
  return useContext(LangContext) ?? bundle(getLang());
}
