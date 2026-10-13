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
} catch (err) {
  console.warn('[firebase] Chưa cấu hình Firebase Web app — đăng nhập bằng số điện thoại sẽ không dùng được:', err.message);
}

const NOT_CONFIGURED_MSG = 'Đăng nhập bằng số điện thoại chưa khả dụng trên kiosk này. Vui lòng dùng Email.';

export function setupRecaptcha(buttonId) {
  if (!auth) throw new Error(NOT_CONFIGURED_MSG);
  if (window.recaptchaVerifier) {
    window.recaptchaVerifier.clear();
    window.recaptchaVerifier = null;
  }
  window.recaptchaVerifier = new RecaptchaVerifier(auth, buttonId, {
    size: 'invisible',
    callback: () => {},
    'expired-callback': () => { window.recaptchaVerifier = null; },
  });
  return window.recaptchaVerifier;
}

export async function sendPhoneOtp(phoneNumber) {
  if (!auth) throw new Error(NOT_CONFIGURED_MSG);
  const verifier = window.recaptchaVerifier;
  if (!verifier) throw new Error('reCAPTCHA chưa được khởi tạo');
  return signInWithPhoneNumber(auth, phoneNumber, verifier);
}

export { auth, RecaptchaVerifier, signInWithPhoneNumber };
