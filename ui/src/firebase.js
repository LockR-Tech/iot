import { initializeApp } from 'firebase/app';
import { getAuth, RecaptchaVerifier, signInWithPhoneNumber } from 'firebase/auth';

const firebaseConfig = {
  apiKey: import.meta.env.VITE_FIREBASE_API_KEY,
  authDomain: import.meta.env.VITE_FIREBASE_AUTH_DOMAIN,
  projectId: import.meta.env.VITE_FIREBASE_PROJECT_ID,
  storageBucket: import.meta.env.VITE_FIREBASE_STORAGE_BUCKET,
  messagingSenderId: import.meta.env.VITE_FIREBASE_MESSAGING_SENDER_ID,
  appId: import.meta.env.VITE_FIREBASE_APP_ID,
  measurementId: import.meta.env.VITE_FIREBASE_MEASUREMENT_ID,
};

// Cấu hình lấy từ Web app "LockR Web" của project lockr-drone-delivery (cùng project với app
// mobile và khoá FIREBASE_CREDENTIALS_JSON của backend), đặt trong ui/.env(.local) — giá trị công
// khai, xem docs 04-engineering/cau-hinh-dich-vu-ngoai.md. Kiosk mở ở http://127.0.0.1:3002 nên
// 127.0.0.1 phải nằm trong Authorized domains của Firebase Auth.
// Máy chưa đặt VITE_FIREBASE_API_KEY thì initializeApp/getAuth ném lỗi ngay — nếu để lỗi đó văng ra scope
// module thì cả kiosk trắng trang, kể cả 2 luồng không hề cần Firebase (mã nhân viên, OTP
// email). Bắt lỗi ở đây; chỉ báo lỗi rõ ràng khi người dùng thật sự bấm "Số điện thoại".
let auth = null;
try {
  const app = initializeApp(firebaseConfig);
  auth = getAuth(app);
  // SMS OTP của Firebase là mẫu cố định của Google, không sửa được câu chữ — chỉ chọn được ngôn ngữ.
  // 'vi' ⇒ SMS tiếng Việt kèm tên hiển thị của project (Firebase Console → Project settings →
  // Public-facing name = "Lock.R").
  auth.languageCode = 'vi';
} catch (err) {
  console.warn('[firebase] Chưa cấu hình Firebase Web app — đăng nhập bằng số điện thoại sẽ không dùng được:', err.message);
}

const NOT_CONFIGURED_MSG = 'Đăng nhập bằng số điện thoại chưa khả dụng trên kiosk này. Vui lòng dùng Email.';

// reCAPTCHA ẩn gắn vào một div riêng (#recaptcha-container trong index.html, không bao giờ bị React gỡ),
// KHÔNG gắn vào nút "Gửi mã": gắn vào nút thì vừa reCAPTCHA vừa onClick cùng chạy, nút còn bị khoá và đổi
// nội dung ngay khi bấm — mã reCAPTCHA bị chạy lại / mất ngữ cảnh và Firebase từ chối (auth/invalid-app-credential).
// Mỗi lần gửi mã tạo verifier mới và dọn verifier cũ, nên bấm gửi lại sau lỗi không dùng lại mã reCAPTCHA đã hỏng.
// Verifier mới luôn vẽ vào MỘT THẺ CON MỚI: grecaptcha nhớ thẻ đã từng vẽ, vẽ lại vào đúng thẻ cũ (kể cả sau
// clear() và xoá innerHTML) ném "reCAPTCHA has already been rendered in this element".
const RECAPTCHA_CONTAINER = 'recaptcha-container';

function clearRecaptcha() {
  if (window.recaptchaVerifier) {
    try { window.recaptchaVerifier.clear(); } catch { /* widget đã bị gỡ */ }
    window.recaptchaVerifier = null;
  }
  const host = document.getElementById(RECAPTCHA_CONTAINER);
  if (host) host.replaceChildren();
}

function freshRecaptchaElement() {
  let host = document.getElementById(RECAPTCHA_CONTAINER);
  if (!host) {   // phòng khi index.html thiếu vùng chứa
    host = document.createElement('div');
    host.id = RECAPTCHA_CONTAINER;
    document.body.appendChild(host);
  }
  const el = document.createElement('div');
  host.appendChild(el);
  return el;
}

export function setupRecaptcha() {
  if (!auth) throw new Error(NOT_CONFIGURED_MSG);
  clearRecaptcha();
  window.recaptchaVerifier = new RecaptchaVerifier(auth, freshRecaptchaElement(), { size: 'invisible' });
  return window.recaptchaVerifier;
}

/** Gửi (hoặc gửi lại) SMS OTP. Trả ConfirmationResult để xác nhận mã. */
export async function sendPhoneOtp(phoneNumber) {
  if (!auth) throw new Error(NOT_CONFIGURED_MSG);
  const verifier = setupRecaptcha();
  try {
    return await signInWithPhoneNumber(auth, phoneNumber, verifier);
  } catch (err) {
    clearRecaptcha();
    throw err;
  }
}

/** 0901234567 / 901234567 / +84901234567 → +84901234567 */
export function toE164Vn(phone) {
  const p = (phone || '').trim();
  if (p.startsWith('0')) return '+84' + p.slice(1);
  if (!p.startsWith('+')) return '+84' + p;
  return p;
}

/** Thông báo tiếng Việt cho lỗi Firebase Auth thường gặp khi gửi / xác nhận OTP qua SMS. */
export function phoneAuthMessage(err) {
  switch (err?.code) {
    case 'auth/invalid-app-credential':
    case 'auth/captcha-check-failed':
    case 'auth/missing-app-credential':
      return 'Không xác minh được kiosk (reCAPTCHA). Vui lòng bấm gửi lại sau vài giây.';
    case 'auth/too-many-requests':
      return 'Số điện thoại này đã yêu cầu quá nhiều mã. Vui lòng thử lại sau ít phút hoặc dùng Email.';
    case 'auth/quota-exceeded':
      return 'Hệ thống tạm hết lượt gửi SMS. Vui lòng dùng Email.';
    case 'auth/invalid-phone-number':
    case 'auth/missing-phone-number':
      return 'Số điện thoại không hợp lệ.';
    case 'auth/network-request-failed':
      return 'Mất kết nối mạng. Vui lòng thử lại.';
    case 'auth/invalid-verification-code':
      return 'Mã OTP không đúng. Vui lòng nhập lại hoặc bấm "Gửi lại mã".';
    case 'auth/code-expired':
      return 'Mã OTP đã hết hạn. Vui lòng bấm "Gửi lại mã".';
    default:
      return err?.message || 'Lỗi gửi OTP. Vui lòng thử lại.';
  }
}

export { auth, RecaptchaVerifier, signInWithPhoneNumber };
