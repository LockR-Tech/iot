import { useEffect, useRef, useState } from 'react';
import { ArrowBigUp, ChevronDown, CornerDownLeft, Delete } from 'lucide-react';

// Bàn phím ảo trong trang cho màn cảm ứng của tủ.
// Chromium chạy --kiosk (toàn màn hình) nên labwc ẩn squeekboard của hệ điều hành — không có
// bàn phím này thì khách không gõ được số điện thoại, email, OTP.
// Hiện khi máy có màn cảm ứng, hoặc ô nhập được focus ngay sau một lần chạm. Không dựa riêng vào
// pointerType: autotouch của Raspberry Pi OS hay bật mouseEmulation, khi đó chạm tới trang thành
// click chuột. Máy dev không cảm ứng thì không thấy bàn phím. URL ?osk=1 ép luôn hiện, ?osk=0 tắt hẳn.
const OSK_PARAM = new URLSearchParams(window.location.search).get('osk');
const HAS_TOUCHSCREEN = navigator.maxTouchPoints > 0;

const TEXT_TYPES = ['text', 'tel', 'email', 'search', 'url', 'password', 'number'];
const valueSetter = Object.getOwnPropertyDescriptor(HTMLInputElement.prototype, 'value').set;

function isTextField(el) {
  return el instanceof HTMLInputElement && TEXT_TYPES.includes(el.type) && !el.readOnly && !el.disabled;
}

function layoutFor(el) {
  if (el.type === 'tel' || el.inputMode === 'tel') return 'tel';
  if (el.type === 'number' || el.inputMode === 'numeric' || el.inputMode === 'decimal') return 'numeric';
  if (el.type === 'email' || el.inputMode === 'email') return 'email';
  return 'text';
}

// Ghi giá trị bằng setter gốc rồi bắn sự kiện `input` để onChange của React chạy như gõ phím thật.
function edit(el, fn) {
  let start = null;
  let end = null;
  try { start = el.selectionStart; end = el.selectionEnd; } catch { /* type=email không có selection */ }
  if (start == null || end == null) start = end = el.value.length;
  const [next, caret] = fn(el.value, start, end);
  valueSetter.call(el, next);
  el.dispatchEvent(new Event('input', { bubbles: true }));
  try { el.setSelectionRange(caret, caret); } catch { /* như trên */ }
}

function insertText(el, text) {
  edit(el, (v, s, e) => {
    const room = el.maxLength > 0 ? el.maxLength - (v.length - (e - s)) : text.length;
    const t = text.slice(0, Math.max(0, room));
    return [v.slice(0, s) + t + v.slice(e), s + t.length];
  });
}

function backspace(el) {
  edit(el, (v, s, e) => {
    if (s !== e) return [v.slice(0, s) + v.slice(e), s];
    if (s === 0) return [v, 0];
    return [v.slice(0, s - 1) + v.slice(s), s - 1];
  });
}

function clearAll(el) {
  edit(el, () => ['', 0]);
}

// Enter ở mọi ô của kiosk là "gửi"; gửi xong ẩn bàn phím để thấy thông báo lỗi/kết quả nằm dưới nút.
function pressEnter(el) {
  el.dispatchEvent(new KeyboardEvent('keydown', { key: 'Enter', code: 'Enter', keyCode: 13, bubbles: true, cancelable: true }));
  el.blur();
}

const LETTER_ROWS = [
  ['1', '2', '3', '4', '5', '6', '7', '8', '9', '0'],
  ['q', 'w', 'e', 'r', 't', 'y', 'u', 'i', 'o', 'p'],
  ['a', 's', 'd', 'f', 'g', 'h', 'j', 'k', 'l'],
];

export default function VirtualKeyboard() {
  const [target, setTarget] = useState(null);
  const [shift, setShift] = useState(false);
  const kbRef = useRef(null);
  const lastPointer = useRef('');

  useEffect(() => {
    if (OSK_PARAM === '0') return undefined;
    const onPointerDown = e => { lastPointer.current = e.pointerType; };
    const onFocusIn = e => {
      if (!isTextField(e.target)) return;
      setTarget(OSK_PARAM === '1' || HAS_TOUCHSCREEN || lastPointer.current === 'touch' ? e.target : null);
      setShift(false);
    };
    const onFocusOut = e => {
      // Chạm phím làm ô nhập mất focus thì trả focus lại, bàn phím không tắt.
      if (e.relatedTarget && kbRef.current?.contains(e.relatedTarget)) {
        e.target.focus({ preventScroll: true });
        return;
      }
      // Chờ focus chuyển xong: sang ô nhập khác thì focusin đã đặt target mới.
      setTimeout(() => { if (!isTextField(document.activeElement)) setTarget(null); }, 0);
    };
    document.addEventListener('pointerdown', onPointerDown, true);
    document.addEventListener('focusin', onFocusIn);
    document.addEventListener('focusout', onFocusOut);
    return () => {
      document.removeEventListener('pointerdown', onPointerDown, true);
      document.removeEventListener('focusin', onFocusIn);
      document.removeEventListener('focusout', onFocusOut);
    };
  }, []);

  useEffect(() => {
    if (!target) return undefined;
    document.body.classList.add('osk-open');
    // Màn hình đổi (ô nhập bị gỡ khỏi DOM) mà không có focusout thì vẫn tắt bàn phím.
    const observer = new MutationObserver(() => { if (!target.isConnected) setTarget(null); });
    observer.observe(document.body, { childList: true, subtree: true });
    // Cuộn ô nhập lên trên mép bàn phím; chạy lại sau hiệu ứng trượt vào của .screen (0,4 s).
    const reveal = () => {
      const scroller = target.closest('.screen');
      if (!scroller || !kbRef.current) return;
      const overlap = target.getBoundingClientRect().bottom + 16 - kbRef.current.getBoundingClientRect().top;
      if (overlap > 0) scroller.scrollTop += overlap;
    };
    const raf = requestAnimationFrame(reveal);
    const timer = setTimeout(reveal, 450);
    return () => {
      document.body.classList.remove('osk-open');
      observer.disconnect();
      cancelAnimationFrame(raf);
      clearTimeout(timer);
    };
  }, [target]);

  if (!target) return null;

  const layout = layoutFor(target);

  // Gõ ngay lúc chạm xuống; preventDefault để nút không cướp focus của ô nhập.
  const key = (content, action, className = '') => (
    <button
      type="button"
      tabIndex={-1}
      className={`osk-key ${className}`}
      onPointerDown={e => { e.preventDefault(); action(target); }}
      onMouseDown={e => e.preventDefault()}
    >
      {content}
    </button>
  );
  const char = (c, className) => {
    const out = shift ? c.toUpperCase() : c;
    return key(out, el => { insertText(el, out); setShift(false); }, className);
  };
  const hideKey = key(<ChevronDown size={22} />, el => el.blur(), 'fn');
  const enterKey = cls => key(<><CornerDownLeft size={20} /> OK</>, pressEnter, `enter ${cls}`);

  if (layout === 'tel' || layout === 'numeric') {
    return (
      <div className="osk osk-numeric" ref={kbRef}>
        <div className="osk-numpad">
          {['1', '2', '3', '4', '5', '6', '7', '8', '9'].map(c => <span key={c}>{char(c)}</span>)}
          <span>{layout === 'tel' ? char('+') : key('Xóa', clearAll, 'fn')}</span>
          <span>{char('0')}</span>
          <span>{key(<Delete size={22} />, backspace, 'fn')}</span>
        </div>
        <div className="osk-side">
          {enterKey('tall')}
          {hideKey}
        </div>
      </div>
    );
  }

  // Mỗi hàng đủ 10 đơn vị để phím các hàng rộng bằng nhau.
  const lastRow = layout === 'email'
    ? ['z', 'x', 'c', 'v', 'b', 'n', 'm', '@', '.']
    : ['z', 'x', 'c', 'v', 'b', 'n', 'm', '-', '.'];
  return (
    <div className="osk osk-text" ref={kbRef}>
      {LETTER_ROWS.map((row, i) => (
        <div className="osk-row" key={i}>
          {row.map(c => <span key={c}>{char(c)}</span>)}
          {i === 2 && <span>{key(<Delete size={22} />, backspace, 'fn')}</span>}
        </div>
      ))}
      <div className="osk-row">
        <span>{key(<ArrowBigUp size={22} />, () => setShift(s => !s), `fn ${shift ? 'on' : ''}`)}</span>
        {lastRow.map(c => <span key={c}>{char(c)}</span>)}
      </div>
      <div className="osk-row">
        {layout === 'email'
          ? [['_', ''], ['-', ''], ['.com', 'w2'], ['@gmail.com', 'w3']].map(([c, w]) => (
            <span key={c} className={w}>{key(c, el => insertText(el, c))}</span>
          ))
          : <span className="w7">{key('Khoảng trắng', el => insertText(el, ' '))}</span>}
        <span className="w2">{enterKey('')}</span>
        <span>{hideKey}</span>
      </div>
    </div>
  );
}
