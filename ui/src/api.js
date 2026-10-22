/**
 * api.js – HTTP calls to:
 *   BACKEND_API  : laundry-locker-microservices (orders, auth, lockers)
 *   LOCAL_API    : smart-laundry-locker-iot FastAPI (system state, MQTT logs)
 */

// docker-compose maps the gateway to host port 18080 (API_GATEWAY_PORT).
//
// VITE_API_URL='' (chuỗi rỗng, không phải unset) => dùng đường dẫn tương đối '/api/...',
// đi qua proxy '/api' của chính Vite dev server (xem vite.config.js) thay vì gọi thẳng từ
// trình duyệt. Bắt buộc khi kiosk chạy trên localhost và trỏ về API production: gọi thẳng
// cross-origin từ trình duyệt bị CORS chặn (production không whitelist localhost), còn
// Vite proxy chuyển tiếp phía server nên trình duyệt chỉ thấy same-origin, không dính CORS.
// `||` sẽ coi '' là falsy nên phải so sánh với undefined tường minh.
// Mặc định dùng chuỗi rỗng '' => dùng đường dẫn tương đối '/api/...',
// đi qua proxy '/api' của Vite dev server (xem vite.config.js) trỏ về backend production https://api.locker-drone.tech.
// Tránh lỗi CORS và không phụ thuộc vào gateway local port 18080 khi test Kiosk.
const BACKEND = import.meta.env.VITE_API_URL || '';
const LOCAL   = import.meta.env.VITE_LOCAL_API_URL || 'http://localhost:8000';

async function req(base, method, path, body = null, token = null, headers = null) {
  const url = `${base}${path}`;
  const opts = {
    method,
    headers: { 'Content-Type': 'application/json', ...headers },
  };
  if (token) opts.headers['Authorization'] = `Bearer ${token}`;
  if (body)  opts.body = JSON.stringify(body);

  try {
    const res  = await fetch(url, opts);
    const data = await res.json();
    return data;
  } catch (err) {
    console.error(`[API] ❌ ${method} ${url}`, err);
    throw err;
  }
}

// Shorthand helpers
const be  = (m, p, b, t, h) => req(BACKEND, m, p, b, t, h);
const loc = (m, p, b, t) => req(LOCAL,   m, p, b, t);

// ─── Auth ────────────────────────────────────────────────────
export const sendOtp           = (email)              => be('POST', '/api/auth/email/send-otp', { email });
export const verifyOtp         = (email, otp)         => be('POST', '/api/auth/email/verify-otp', { email, otp });
export const phoneLogin        = (idToken)            => be('POST', '/api/auth/firebase', { idToken });
export const kioskQuickRegister= (tempToken)          => be('POST', '/api/auth/kiosk/quick-register', { tempToken });
// Đăng nhập kiosk bằng QR: khách quét bằng app rồi xác nhận, kiosk hỏi trạng thái tới khi nhận token.
// pollToken chỉ đi trong header, không bao giờ hiện lên màn hình (chụp QR không cướp được phiên).
const qrPollHeader = (pollToken) => ({ 'X-Kiosk-Poll-Token': pollToken });
export const createKioskQrSession = (lockerId)               => be('POST', '/api/auth/kiosk/qr-sessions', { lockerId });
export const pollKioskQrSession   = (sessionId, pollToken)   => be('GET', `/api/auth/kiosk/qr-sessions/${encodeURIComponent(sessionId)}`, null, null, qrPollHeader(pollToken));
export const cancelKioskQrSession = (sessionId, pollToken)   => be('POST', `/api/auth/kiosk/qr-sessions/${encodeURIComponent(sessionId)}/cancel`, null, null, qrPollHeader(pollToken));

// ─── Promotions ──────────────────────────────────────────────
// Phản hồi: { code, valid, reason, promotion }. Gửi lockerId để mã chỉ áp dụng ở tủ khác bị từ chối,
// orderTotal để backend kiểm đơn tối thiểu.
export const validatePromotionCode = (code, token, lockerId, orderTotal) => {
  const q = new URLSearchParams();
  if (lockerId != null) q.set('lockerId', lockerId);
  if (orderTotal != null) q.set('orderTotal', orderTotal);
  const qs = q.toString();
  return be('GET', `/api/promotions/validate/${encodeURIComponent(code.toUpperCase())}${qs ? `?${qs}` : ''}`, null, token);
};
// Quy tắc nghiệp vụ công khai admin cấu hình (giá thuê, số giờ gợi ý…): { "<key>": giá trị }.
export const getPublicSettings = (scope)               => be('GET', `/api/settings/${scope}/public`);

// ─── Lockers / Boxes ─────────────────────────────────────────
export const getLockerById     = (lockerId, token)    => be('GET', `/api/lockers/${lockerId}`, null, token);
export const getAllLockers     = (token)              => be('GET', '/api/lockers', null, token);
// Layout (public GET): cells with id/boxNumber/status/cellType — the kiosk's
// source of truth for mapping the box number a customer keys in to a boxId.
export const getLockerLayout   = (lockerId)           => be('GET', `/api/lockers/${lockerId}/layout`);
export const getAvailableBoxes = (lockerId, token)    => be('GET', `/api/lockers/${lockerId}/boxes/available`, null, token);
export const getAllBoxes        = (lockerId, token)    => be('GET', `/api/lockers/${lockerId}/boxes`, null, token);

// ─── Orders ──────────────────────────────────────────────────
export const createOrder       = (token, data)        => be('POST', '/api/orders', data, token);
// createRentalOrder: dùng endpoint /api/orders/rental — gateway gắn X-User-Id từ JWT
// → đơn hàng sẽ được liên kết đúng với user, hiển thị trong mobile app
export const createRentalOrder = (token, data)        => be('POST', '/api/orders/rental', data, token);
export const confirmOrder      = (token, orderId)     => be('PUT', `/api/orders/${orderId}/confirm`, null, token);
export const getOrderStatus    = (orderId, token)     => be('GET', `/api/orders/${orderId}/status`, null, token);
export const getOrderByPin     = (pin, token)         => be('GET', `/api/orders/pin/${pin}`, null, token);
export const getMyOrders       = (token)              => be('GET', '/api/orders/my-orders', null, token);
export const cancelOrder       = (token, orderId)     => be('PUT', `/api/orders/${orderId}/cancel`, null, token);

// ─── Payments / Ví ───────────────────────────────────────────
// Cùng checkout với app mobile; số tiền server tự lấy từ đơn, kiosk không gửi.
// WALLET: trừ ví ngay (thiếu tiền ⇒ code WALLET_INSUFFICIENT). SEPAY: tạo giao dịch PENDING,
// trả qrCodeUrl (ảnh VietQR) + referenceId (nội dung chuyển khoản); tiền chỉ được ghi nhận qua webhook SePay.
export const checkoutSepay     = (token, orderId)     => be('POST', '/api/payments/checkout', { orderId, method: 'SEPAY' }, token);
export const getPayment        = (token, paymentId)   => be('GET', `/api/payments/${paymentId}`, null, token);
// Chỉ khi admin bật giả lập thanh toán (test): coi như đã nhận `amount` đồng (bỏ trống = đủ số) cho giao dịch.
export const simulatePayment   = (token, { paymentId, amount }) => be('POST', '/api/payments/simulate', { paymentId, ...(amount ? { amount } : {}) }, token);

// ─── IoT (public) ────────────────────────────────────────────
export const verifyPin         = (pinCode, boxId)     => be('POST', '/api/iot/verify-pin', { pinCode, boxId });
export const unlockBox         = (lockerId, pinCode, boxId, actionType) => be('POST', '/api/iot/unlock', { lockerId, pinCode, boxId, actionType });
export const unlockWithCode    = (lockerId, accessCode)         => be('POST', '/api/iot/unlock-with-code', { lockerId, code: accessCode });
// Sau khi ô đã mở bằng mã: xác nhận bỏ hàng / kết thúc thuê ngay tại tủ (không cần đăng nhập,
// backend chỉ nhận khi ô của đơn vừa được mở thành công).
export const confirmDropWithCode = (lockerId, accessCode)       => be('POST', '/api/iot/confirm-drop-with-code', { lockerId, code: accessCode });
export const endRentalWithCode   = (lockerId, accessCode)       => be('POST', '/api/iot/end-rental-with-code', { lockerId, code: accessCode });
// Người thuê đã đăng nhập trên kiosk kết thúc lượt thuê.
export const endRental         = (token, orderId)     => be('POST', `/api/orders/${orderId}/pickup-storage`, null, token);
export const pickupOrder       = (orderId, token)     => be('POST', '/api/iot/pickup', { orderId }, token);

// ─── Local FastAPI (IoT Gateway) ─────────────────────────────
export const getSystemInfo     = ()                   => loc('GET', '/system/info');
export const getSystemState    = ()                   => loc('GET', '/system/state');
export const getRecentLogs     = (limit = 50)         => loc('GET', `/logs/recent?limit=${limit}`);
export const getMqttConfig     = ()                   => loc('GET', '/config/mqtt');
export const saveMqttConfig    = (cfg)                => loc('POST', '/config/mqtt', cfg);
export const clearCabinetSetup = ()                   => loc('POST', '/setup/clear');
export const healthCheck       = ()                   => loc('GET', '/health');
