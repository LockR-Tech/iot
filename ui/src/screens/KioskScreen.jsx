import { useState, useEffect, useCallback, useRef } from 'react';
import * as api from '../api';
import { sendPhoneOtp, toE164Vn, phoneAuthMessage } from '../firebase';
import {
  Lock, Package, Smartphone, Mail, ArrowLeft, MapPin,
  CheckCircle, KeyRound, Hash, User, CreditCard,
  Unlock, Home, Loader2, Circle, ClipboardList,
  Wifi, ChevronRight, ShieldCheck, X,
  Clock, LogOut, Users, AlertTriangle, Plane, Luggage, QrCode, RefreshCw, FlaskConical,
  CalendarDays, Minus, Plus, Info, XCircle, Wrench, Send
} from 'lucide-react';
import { QRCodeSVG } from 'qrcode.react';
import boxStack3d from '../assets/illustrations/box_stack_3d.png';
import mobilePay3d from '../assets/illustrations/mobile_pay_3d.png';
import deliveryAnimation from '../assets/illustrations/delivery_animation.json';
// Cùng minh hoạ drone 3D với thẻ đơn giao hàng trong app mobile (AppLottieAssets.drone).
import drone3dAnimation from '../assets/illustrations/drone_3d.json';
import lockrLogo from '../assets/logo.png';
// Icon "Báo sự cố" 3D của app mobile (AppLottieAssets.baoSuCo).
import baoSuCo3dAnimation from '../assets/illustrations/bao_su_co_3d.json';
import LottieAnimation from '../components/LottieAnimation';


// ============================================
// Config
// ============================================
const urlParams = new URLSearchParams(window.location.search);

const LOCKER_CODE = urlParams.get('lockerCode') || import.meta.env.VITE_LOCKER_CODE || 'LOC-01-001';
const INITIAL_LOCKER_ID = parseInt(urlParams.get('lockerId') || import.meta.env.VITE_LOCKER_ID || '1', 10);
const AUTO_HOME_SEC = 20;

// ============================================
// App
// ============================================
export default function KioskScreen() {
  const [activeLockerId, setActiveLockerId] = useState(INITIAL_LOCKER_ID);
  const [allLockers, setAllLockers] = useState([]);

  const [screen, setScreen] = useState('home');
  const [history, setHistory] = useState(['home']);
  const [jwt, setJwt] = useState('');
  const [email, setEmail] = useState('');
  const [phone, setPhone] = useState('');
  const [loginMethod, setLoginMethod] = useState('phone');
  const [tempToken, setTempToken] = useState('');
  const [userName, setUserName] = useState('');
  const [selectedBox, setSelectedBox] = useState(null);
  const [orderId, setOrderId] = useState(null);
  const [orderPin, setOrderPin] = useState('');
  const [orderCode, setOrderCode] = useState('');
  const [totalPrice, setTotalPrice] = useState(0);
  const [successTitle, setSuccessTitle] = useState('');
  const [successMsg, setSuccessMsg] = useState('');
  const [successExtra, setSuccessExtra] = useState(null);
  const [countdown, setCountdown] = useState(0);
  const [lockerInfo, setLockerInfo] = useState(null);
  // Đăng nhập xong đi đâu: 'boxes' (gửi đồ / thuê tủ) hay 'report' (báo ô tủ hư).
  const [afterLogin, setAfterLogin] = useState('boxes');
  // Ô chọn sẵn khi mở màn báo hỏng từ hộp "Quản lý ô" (null = khách tự chọn trên sơ đồ).
  const [reportBoxId, setReportBoxId] = useState(null);
  const cdRef = useRef(null);

  // Fetch list of all lockers for dropdown
  useEffect(() => {
    (async () => {
      try {
        const res = await api.getAllLockers();
        if (res.success && res.data) {
          const list = res.data.content || res.data;
          setAllLockers(list);
          if (list.length > 0 && !list.find(l => l.id === activeLockerId)) {
            setActiveLockerId(list[0].id);
          }
        }
      } catch (err) { console.error('Failed to fetch lockers', err); }
    })();
  }, []);

  // Fetch locker info + layout (with periodic polling for real-time sync)
  useEffect(() => {
    let ignore = false;
    if (!activeLockerId) return;

    const fetchLockerData = async () => {
      try {
        const res = await api.getLockerById(activeLockerId, jwt || undefined);
        if (ignore || !res.success || !res.data) return;
        let boxes = [];
        try {
          const lay = await api.getLockerLayout(activeLockerId);
          if (lay.success && Array.isArray(lay.data?.cells)) boxes = lay.data.cells;
        } catch { /* layout is best-effort */ }
        if (!ignore) setLockerInfo({ ...res.data, boxes });
      } catch { /* ignore */ }
    };

    setLockerInfo(null);
    fetchLockerData();
    const intervalId = setInterval(fetchLockerData, 3000);

    return () => {
      ignore = true;
      clearInterval(intervalId);
    };
  }, [jwt, activeLockerId]);

  // Navigate
  const go = useCallback((s) => {
    console.log(`%c[NAV] → ${s}`, 'color:#c084fc;font-weight:bold');
    setScreen(s);
    setHistory(h => [...h, s]);
  }, []);

  const back = useCallback(() => {
    setHistory(h => {
      if (h.length <= 1) return h;
      const newH = h.slice(0, -1);
      setScreen(newH[newH.length - 1]);
      return newH;
    });
  }, []);

  const goHome = useCallback(() => {
    console.log('%c[NAV] → home (reset)', 'color:#c084fc;font-weight:bold');
    setJwt(''); setEmail(''); setPhone(''); setLoginMethod('phone'); setTempToken(''); setUserName('');
    setSelectedBox(null); setOrderId(null);
    setOrderPin(''); setOrderCode(''); setTotalPrice(0);
    setAfterLogin('boxes'); setReportBoxId(null);
    setScreen('home'); setHistory(['home']);
    if (cdRef.current) clearInterval(cdRef.current);
  }, []);

  const showSuccess = useCallback((title, msg, extra = null) => {
    setSuccessTitle(title);
    setSuccessMsg(msg);
    setSuccessExtra(extra);
    go('success');
    // Màn có mã mở ô: để lâu hơn cho khách kịp ghi / chụp lại mã.
    let sec = extra?.orderPin ? AUTO_HOME_SEC * 2 : AUTO_HOME_SEC;
    setCountdown(sec);
    if (cdRef.current) clearInterval(cdRef.current);
    cdRef.current = setInterval(() => {
      sec--;
      setCountdown(sec);
      if (sec <= 0) { clearInterval(cdRef.current); goHome(); }
    }, 1000);
  }, [go, goHome]);

  return (
    <div className="kiosk-wrap">
      {screen === 'home' && <HomeScreen go={go} startLogin={(next) => { setAfterLogin(next); go('login'); }} lockerInfo={lockerInfo} activeLockerId={activeLockerId} setActiveLockerId={setActiveLockerId} allLockers={allLockers} />}
      {screen === 'login' && <LoginScreen go={go} goHome={goHome} purpose={afterLogin} email={email} setEmail={setEmail} phone={phone} setPhone={setPhone} loginMethod={loginMethod} setLoginMethod={setLoginMethod} />}
      {screen === 'otp' && <OtpScreen go={go} next={afterLogin} back={back} email={email} setJwt={setJwt} setTempToken={setTempToken} setUserName={setUserName} />}
      {screen === 'phone-otp' && <PhoneOtpScreen go={go} next={afterLogin} back={back} phone={phone} setJwt={setJwt} setTempToken={setTempToken} setUserName={setUserName} />}
      {screen === 'register' && <RegisterScreen go={go} next={afterLogin} goHome={goHome} tempToken={tempToken} setJwt={setJwt} setUserName={setUserName} />}
      {screen === 'boxes' && <BoxSelectionScreen go={go} goHome={goHome} jwt={jwt} userName={userName} selectedBox={selectedBox} setSelectedBox={setSelectedBox} lockerInfo={lockerInfo} activeLockerId={activeLockerId} onReportBox={(boxId) => { setReportBoxId(boxId); go('report'); }} />}
      {screen === 'report' && <ReportScreen goHome={goHome} jwt={jwt} userName={userName} lockerInfo={lockerInfo} activeLockerId={activeLockerId} initialBoxId={reportBoxId} showSuccess={showSuccess} />}
      {screen === 'order-info' && <OrderInfoScreen go={go} back={back} jwt={jwt} selectedBox={selectedBox} setOrderId={setOrderId} setOrderPin={setOrderPin} setOrderCode={setOrderCode} setTotalPrice={setTotalPrice} activeLockerId={activeLockerId} />}
      {screen === 'payment' && <PaymentScreen go={go} goHome={goHome} jwt={jwt} orderId={orderId} orderPin={orderPin} orderCode={orderCode} totalPrice={totalPrice} selectedBox={selectedBox} showSuccess={showSuccess} activeLockerId={activeLockerId} pinContact={loginMethod === 'email' ? { method: 'email', value: email } : { method: 'phone', value: phone }} />}
      {screen === 'staff' && <StaffScreen goHome={goHome} showSuccess={showSuccess} lockerInfo={lockerInfo} activeLockerId={activeLockerId} />}
      {screen === 'success' && <SuccessScreen goHome={goHome} title={successTitle} msg={successMsg} extra={successExtra} countdown={countdown} />}
      <DialogHost />
    </div>
  );
}

// ============================================
// Shared Components
// ============================================
function Header({ onBack, title }) {
  return (
    <div className="header">
      <button className="back-btn" onClick={onBack}><ArrowLeft size={20} /></button>
      <h2>{title}</h2>
    </div>
  );
}

function Msg({ type, text }) {
  if (!text) return null;
  return <div className={`msg msg-${type}`}>{text}</div>;
}

// Hộp thoại kiosk thay alert()/confirm() của trình duyệt (hộp xám "localhost says" lạc giao diện và
// lộ địa chỉ máy). Gọi được từ bất kỳ đâu: `notify(...)` trả Promise khi bấm OK, `ask(...)` trả true/false.
let dialogListener = null;
const openDialog = (opts) => new Promise(resolve => {
  if (dialogListener) dialogListener({ ...opts, resolve });
  else resolve(opts.confirm ? window.confirm(opts.message) : (window.alert(opts.message), true));
});
const notify = (message, opts = {}) => openDialog({ message, ...opts });
const ask = (message, opts = {}) => openDialog({ message, confirm: true, ...opts });

const DIALOG_TONES = {
  info: { Icon: Info, title: 'Thông báo' },
  success: { Icon: CheckCircle, title: 'Thành công' },
  warn: { Icon: AlertTriangle, title: 'Lưu ý' },
  error: { Icon: XCircle, title: 'Có lỗi xảy ra' },
};

function DialogHost() {
  const [dialog, setDialog] = useState(null);
  useEffect(() => {
    dialogListener = setDialog;
    return () => { dialogListener = null; };
  }, []);
  if (!dialog) return null;
  const tone = DIALOG_TONES[dialog.tone] ? dialog.tone : 'info';
  const { Icon } = DIALOG_TONES[tone];
  const close = (value) => { setDialog(null); dialog.resolve(value); };
  return (
    <div className="modal-overlay kiosk-dialog-overlay" onClick={() => close(false)}>
      <div className={`kiosk-dialog tone-${tone}`} role="alertdialog" aria-modal="true" onClick={e => e.stopPropagation()}>
        <div className="kiosk-dialog-icon"><Icon size={30} /></div>
        <h3>{dialog.title || DIALOG_TONES[tone].title}</h3>
        <p>{dialog.message}</p>
        <div className="kiosk-dialog-actions">
          {dialog.confirm && (
            <button className="btn btn-secondary" onClick={() => close(false)}>{dialog.cancelText || 'Huỷ'}</button>
          )}
          <button className="btn btn-primary" autoFocus onClick={() => close(true)}>
            {dialog.confirmText || (dialog.confirm ? 'Đồng ý' : 'Đã hiểu')}
          </button>
        </div>
      </div>
    </div>
  );
}

// Mã mở ô của lượt thuê + lời nhắc giữ mã. Thuê bằng email: backend gửi ngay thư có đủ thông tin lượt
// thuê lúc đơn được thanh toán (OrderService#notifyRenterOrderPaid). Thuê bằng SĐT: không có thư nên
// nhắc khách ghi lại mã, mã vẫn xem lại được trong app.
function PinNotice({ pin, contact, compact = false }) {
  if (!pin) return null;
  const byEmail = contact?.method === 'email' && contact.value;
  return (
    <div className={`pin-notice ${compact ? 'compact' : ''}`}>
      <div className="pin-notice-label"><KeyRound size={14} /> Mã mở ô tủ của bạn</div>
      <div className="pin-notice-code">{pin}</div>
      <div className={`pin-notice-badge ${byEmail ? 'email' : 'phone'}`}>
        {byEmail ? <Mail size={18} /> : <AlertTriangle size={18} />}
        <span>
          {byEmail ? (
            <>Mã mở ô và toàn bộ thông tin thuê đã được gửi về email <strong>{contact.value}</strong>. Nếu chưa thấy, hãy kiểm tra cả mục Spam.</>
          ) : (
            <><strong>Lưu ý: nhớ mã này để mở lại ô tủ.</strong> Hãy ghi lại hoặc chụp màn hình — mã dùng để mở ô suốt thời gian thuê. Bạn cũng xem lại mã trong mục Đơn tủ của app Lock.R{contact?.value ? <> (đăng nhập bằng số <strong>{contact.value}</strong>)</> : null}.</>
          )}
        </span>
      </div>
    </div>
  );
}

function Btn({ children, onClick, loading, disabled, variant = 'primary', style, id }) {
  return (
    <button id={id} className={`btn btn-${variant}`} onClick={onClick} disabled={loading || disabled} style={style}>
      {loading ? <><Loader2 size={18} className="spinner" style={{ border: 'none', animation: 'spin 0.6s linear infinite' }} /> Đang xử lý...</> : children}
    </button>
  );
}

// ============================================
// HOME
// ============================================
function HomeScreen({ go, startLogin, lockerInfo, activeLockerId, setActiveLockerId, allLockers }) {
  return (
    <div className="screen screen-home-split">
      <div className="home-left">
        <div className="kiosk-hero-3d">
          <div className="hero-brand-badge">
            <span className="dot green"></span> Hệ thống thông minh 24/7
          </div>
          <div className="hero-3d-visual">
            <img src={lockrLogo} alt="Lock.R" className="hero-logo-img" />
          </div>
          <div style={{ textAlign: 'center' }}>
            <h1 style={{ fontSize: 36, fontWeight: 900, letterSpacing: -0.5, margin: '0 0 8px 0', background: 'linear-gradient(135deg, #0f172a 0%, #2563eb 100%)', WebkitBackgroundClip: 'text', WebkitTextFillColor: 'transparent' }}>
              Lock.R
            </h1>
            <p style={{ fontSize: 14, color: 'var(--text-secondary)', margin: 0, fontWeight: 500, maxWidth: 300, lineHeight: 1.5 }}>
              Hệ thống tủ thông minh tự động — Giao nhận bưu kiện &amp; lưu trữ đồ đạc tiện lợi
            </p>
          </div>
        </div>
        <div className="footer">Powered by Lock.R IoT System</div>

        {/* Báo ô tủ hư: nút tròn 3D như nút "Báo sự cố" trên app (không chiếm chỗ của 2 thẻ chính).
            Cần đăng nhập vì backend ghi người báo (REPORTER_REQUIRED). */}
        <button className="home-report-fab" onClick={() => startLogin('report')} aria-label="Báo ô tủ hư">
          <span className="home-report-fab-circle">
            <LottieAnimation animationData={baoSuCo3dAnimation} className="home-report-fab-lottie" />
          </span>
          <span className="home-report-fab-label">Báo sự cố</span>
        </button>
      </div>

      <div className="home-right">
        <div className="home-config">
          <MapPin size={18} color="var(--accent)" />
          <select
            value={activeLockerId}
            onChange={e => setActiveLockerId(parseInt(e.target.value, 10))}
            className="locker-select"
          >
            {allLockers.map(loc => (
              <option key={loc.id} value={loc.id}>
                {loc.name} - {loc.code}
              </option>
            ))}
          </select>
        </div>

        {lockerInfo && (
          <div className="locker-info-card">
            <div className="locker-name">
              {lockerInfo.storeName || lockerInfo.name}
            </div>
            <div className="locker-address">{lockerInfo.address}</div>
            {lockerInfo.totalBoxes != null && (
              <div className="locker-stats">
                <span className="stat-available"><CheckCircle size={14} style={{ marginRight: 4, verticalAlign: -2 }} />{lockerInfo.availableBoxes ?? '—'} Trống</span>
                <span className="stat-total">/ {lockerInfo.totalBoxes} ô</span>
              </div>
            )}
          </div>
        )}

        <div className="home-status">
          <Wifi size={16} />
          Kiosk sẵn sàng phục vụ
        </div>

        <div className="kiosk-action-cards">
          {/* Card 1: Mở tủ */}
          <div className="card-3d-action primary" onClick={() => go('staff')}>
            <div className="card-3d-thumb">
              <LottieAnimation animationData={drone3dAnimation} className="card-3d-lottie" />
            </div>
            <div className="card-3d-body">
              <div className="card-3d-title">
                Mở Tủ / Nhận Đồ <ChevronRight size={18} color="var(--accent)" />
              </div>
              <div className="card-3d-subtitle">
                Nhập mã OTP nhận hàng hoặc Quét mã QR bưu kiện
              </div>
              <div className="card-3d-badge blue">
                <KeyRound size={12} /> Mã PIN / Quét QR
              </div>
            </div>
          </div>

          {/* Card 2: Gửi đồ / Thuê tủ */}
          <div className="card-3d-action secondary" onClick={() => startLogin('boxes')}>
            <div className="card-3d-thumb">
              <img src={boxStack3d} alt="Gửi đồ" />
            </div>
            <div className="card-3d-body">
              <div className="card-3d-title">
                Gửi Đồ / Thuê Tủ <ChevronRight size={18} color="#059669" />
              </div>
              <div className="card-3d-subtitle">
                Lưu trữ hành lý, gửi hàng trực tiếp tại Kiosk
              </div>
              <div className="card-3d-badge green">
                <Package size={12} /> {lockerInfo?.availableBoxes ?? 'Nhiều'} ô trống sẵn sàng
              </div>
            </div>
          </div>

        </div>
      </div>
    </div>
  );
}


// ============================================
// LOGIN (Phone / Email OTP)
// ============================================
function LoginScreen({ go, goHome, purpose = 'boxes', email, setEmail, phone, setPhone, loginMethod, setLoginMethod }) {
  const [loading, setLoading] = useState(false);
  const [msg, setMsg] = useState('');

  const handleSendEmail = async () => {
    if (!email || !email.includes('@')) { setMsg('Vui lòng nhập email hợp lệ'); return; }
    setLoading(true); setMsg('');
    try {
      console.log('%c[LOGIN] Sending email OTP to:', 'color:#fbbf24', email);
      const res = await api.sendOtp(email);
      if (res.success) go('otp');
      else setMsg(res.message || 'Lỗi gửi OTP');
    } catch { setMsg('Lỗi kết nối server'); }
    setLoading(false);
  };

  const handleSendPhone = async () => {
    if (!phone || phone.length < 9) { setMsg('Vui lòng nhập số điện thoại hợp lệ'); return; }
    setLoading(true); setMsg('');
    try {
      window.confirmationResult = await sendPhoneOtp(toE164Vn(phone));
      go('phone-otp');
    } catch (err) {
      console.error('Firebase phone OTP error:', err);
      setMsg(phoneAuthMessage(err));
    }
    setLoading(false);
  };

  return (
    <div className="screen">
      <Header onBack={goHome} title={purpose === 'report' ? 'Đăng nhập để báo ô tủ hư' : 'Nhập thông tin'} />
      {purpose === 'report' && (
        <p className="login-purpose"><Wrench size={15} /> Xác thực số điện thoại hoặc email để kỹ thuật viên liên hệ lại khi cần.</p>
      )}
      <div className="login-tabs">
        <div className={`login-tab ${loginMethod === 'phone' ? 'active' : ''}`} onClick={() => { setLoginMethod('phone'); setMsg(''); }}>
          <Smartphone size={16} /> Số điện thoại
        </div>
        <div className={`login-tab ${loginMethod === 'email' ? 'active' : ''}`} onClick={() => { setLoginMethod('email'); setMsg(''); }}>
          <Mail size={16} /> Email
        </div>
      </div>

      {loginMethod === 'phone' ? (
        <>
          <p className="subtitle">Nhập số điện thoại để nhận mã OTP xác thực</p>
          <div className="form-group">
            <label>Số điện thoại</label>
            <input className="input" type="tel" value={phone} onChange={e => setPhone(e.target.value.replace(/[^0-9+]/g, ''))}
              placeholder="0901234567" inputMode="tel" autoComplete="tel"
              onKeyDown={e => e.key === 'Enter' && handleSendPhone()} />
          </div>
          <Btn onClick={handleSendPhone} loading={loading}><Smartphone size={18} /> Gửi mã OTP</Btn>
        </>
      ) : (
        <>
          <p className="subtitle">Nhập email để nhận mã OTP xác thực</p>
          <div className="form-group">
            <label>Email</label>
            <input className="input" type="email" value={email} onChange={e => setEmail(e.target.value)}
              placeholder="example@gmail.com" autoComplete="email" onKeyDown={e => e.key === 'Enter' && handleSendEmail()} />
          </div>
          <Btn onClick={handleSendEmail} loading={loading}><Mail size={18} /> Gửi mã OTP</Btn>
        </>
      )}
      {msg && <Msg type="error" text={msg} />}
    </div>
  );
}

// ============================================
// GỬI LẠI MÃ OTP — dùng chung cho OTP số điện thoại và email
// ============================================
const RESEND_COOLDOWN_SEC = 60;

/**
 * Nút "Gửi lại mã" có đếm ngược: mã vừa gửi xong thì phải chờ RESEND_COOLDOWN_SEC giây mới gửi lại được
 * (tránh spam SMS/email và bị Firebase chặn too-many-requests). onResend() trả true nếu gửi thành công.
 */
function ResendCode({ onResend }) {
  const [left, setLeft] = useState(RESEND_COOLDOWN_SEC);
  const [sending, setSending] = useState(false);

  useEffect(() => {
    if (left <= 0) return undefined;
    const t = setTimeout(() => setLeft(s => s - 1), 1000);
    return () => clearTimeout(t);
  }, [left]);

  const resend = async () => {
    setSending(true);
    const ok = await onResend();
    setSending(false);
    if (ok) setLeft(RESEND_COOLDOWN_SEC);
  };

  return (
    <Btn variant="secondary" onClick={resend} loading={sending} disabled={left > 0} style={{ marginTop: 12 }}>
      <RefreshCw size={18} /> {left > 0 ? `Gửi lại mã sau ${left}s` : 'Gửi lại mã'}
    </Btn>
  );
}

// ============================================
// PHONE OTP (Firebase verification)
// ============================================
function PhoneOtpScreen({ go, next = 'boxes', back, phone, setJwt, setTempToken, setUserName }) {
  const [otp, setOtp] = useState('');
  const [loading, setLoading] = useState(false);
  const [msg, setMsg] = useState('');
  const [info, setInfo] = useState('');

  const handleResend = async () => {
    setMsg(''); setInfo('');
    try {
      window.confirmationResult = await sendPhoneOtp(toE164Vn(phone));
      setOtp('');
      setInfo('Đã gửi mã OTP mới. Mã cũ không còn dùng được.');
      return true;
    } catch (err) {
      console.error('Firebase phone OTP resend error:', err);
      setMsg(phoneAuthMessage(err));
      return false;
    }
  };

  const handleVerify = async () => {
    if (otp.length !== 6) { setMsg('Vui lòng nhập đủ 6 số OTP'); return; }
    setLoading(true); setMsg('');
    try {
      const confirmation = window.confirmationResult;
      if (!confirmation) { setMsg('Phiên đã hết hạn. Vui lòng quay lại.'); setLoading(false); return; }

      const userCredential = await confirmation.confirm(otp);
      const idToken = await userCredential.user.getIdToken();
      console.log('%c[AUTH] Firebase phone verified, sending to backend', 'color:#fbbf24');

      const res = await api.phoneLogin(idToken);
      if (res.success && res.data) {
        if (res.data.newUser || res.data.isNewUser) {
          setTempToken(res.data.tempToken);
          go('register');
        } else {
          setJwt(res.data.accessToken);
          const name = res.data.userInfo?.fullName || phone;
          setUserName(name);
          go(next);
        }
      } else {
        setMsg(res.message || 'Lỗi đăng nhập');
      }
    } catch (err) {
      console.error('Phone OTP verify error:', err);
      setInfo('');
      setMsg(phoneAuthMessage(err));
    }
    setLoading(false);
  };

  return (
    <div className="screen">
      <Header onBack={back} title="Xác thực OTP" />
      <p className="subtitle">
        Mã OTP đã gửi đến <strong>{phone}</strong>
      </p>
      <div className="form-group">
        <label>Nhập mã OTP 6 số</label>
        <input className="input" value={otp} onChange={e => setOtp(e.target.value.replace(/\D/g, '').slice(0, 6))}
          placeholder="123456" maxLength={6} inputMode="numeric"
          style={{ textAlign: 'center', fontSize: 24, letterSpacing: 8, fontWeight: 700 }}
          onKeyDown={e => e.key === 'Enter' && handleVerify()} />
      </div>
      <Btn onClick={handleVerify} loading={loading}><CheckCircle size={18} /> Xác nhận</Btn>
      <ResendCode onResend={handleResend} />
      {msg && <Msg type="error" text={msg} />}
      {info && !msg && <Msg type="success" text={info} />}
    </div>
  );
}

// ============================================
// OTP
// ============================================
function OtpScreen({ go, next = 'boxes', back, email, setJwt, setTempToken, setUserName }) {
  const [otp, setOtp] = useState('');
  const [loading, setLoading] = useState(false);
  const [msg, setMsg] = useState('');
  const [info, setInfo] = useState('');

  const handleResend = async () => {
    setMsg(''); setInfo('');
    try {
      const res = await api.sendOtp(email);
      if (res.success) {
        setOtp('');
        setInfo('Đã gửi mã OTP mới vào email. Mã cũ không còn dùng được.');
        return true;
      }
      setMsg(res.message || 'Lỗi gửi lại OTP');
    } catch { setMsg('Lỗi kết nối server'); }
    return false;
  };

  const handleVerify = async () => {
    if (otp.length !== 6) { setMsg('Vui lòng nhập đủ 6 số OTP'); return; }
    setLoading(true); setMsg(''); setInfo('');
    try {
      const res = await api.verifyOtp(email, otp);
      if (res.success && res.data) {
        if (res.data.newUser || res.data.isNewUser) {
          setTempToken(res.data.tempToken);
          go('register');
        } else {
          setJwt(res.data.accessToken);
          const name = res.data.userInfo?.fullName || email;
          setUserName(name);
          go(next);
        }
      } else {
        setMsg(res.message || 'OTP không hợp lệ');
      }
    } catch { setMsg('Lỗi kết nối server'); }
    setLoading(false);
  };

  return (
    <div className="screen">
      <Header onBack={back} title="Xác thực OTP" />
      <p className="subtitle">
        Mã OTP đã gửi đến <strong>{email}</strong>
      </p>
      <div className="form-group">
        <label>Nhập mã OTP 6 số</label>
        <input className="input" value={otp} onChange={e => setOtp(e.target.value.replace(/\D/g, '').slice(0, 6))}
          placeholder="123456" maxLength={6} inputMode="numeric"
          style={{ textAlign: 'center', fontSize: 24, letterSpacing: 8, fontWeight: 700 }}
          onKeyDown={e => e.key === 'Enter' && handleVerify()} />
      </div>
      <Btn onClick={handleVerify} loading={loading}><CheckCircle size={18} /> Xác nhận</Btn>
      <ResendCode onResend={handleResend} />
      {msg && <Msg type="error" text={msg} />}
      {info && !msg && <Msg type="success" text={info} />}
    </div>
  );
}

// ============================================
// REGISTER (Kiosk Quick Register — API 1.4)
// ============================================
function RegisterScreen({ go, next = 'boxes', goHome, tempToken, setJwt, setUserName }) {
  const [loading, setLoading] = useState(false);
  const [msg, setMsg] = useState('');
  const reqSent = useRef(false);

  useEffect(() => {
    if (reqSent.current) return;
    reqSent.current = true;

    (async () => {
      setLoading(true); setMsg('');
      try {
        const res = await api.kioskQuickRegister(tempToken);
        if (res.success && res.data) {
          setJwt(res.data.accessToken);
          setUserName('Khách');
          go(next);
        } else {
          setMsg(res.message || 'Lỗi đăng ký nhanh');
        }
      } catch {
        setMsg('Lỗi kết nối server');
      }
      setLoading(false);
    })();
  }, [tempToken, setJwt, setUserName, go, next]);

  return (
    <div className="screen">
      <Header onBack={goHome} title="Đăng ký nhanh" />
      {loading && (
        <div style={{ textAlign: 'center', padding: 40 }}>
          <Loader2 size={36} className="spinner" style={{ border: 'none', animation: 'spin 0.6s linear infinite', color: 'var(--accent)' }} />
          <p style={{ color: 'var(--text-secondary)', marginTop: 16, fontSize: 14 }}>Đang tạo tài khoản...</p>
        </div>
      )}
      {msg && <Msg type="error" text={msg} />}
      {msg && <Btn onClick={goHome} variant="secondary" style={{ marginTop: 16 }}><ArrowLeft size={18} /> Quay lại</Btn>}
    </div>
  );
}

// ============================================
// BOX SELECTION
// ============================================
// Vị trí từng ô theo bố cục thật của tủ (rowIndex/colIndex từ /layout), giống app mobile: ô XL cao 2 hàng.
// Thiếu toạ độ hoặc hai ô trùng chỗ thì trả null, lưới tự xếp nối tiếp như cũ.
function boxPlacement(boxes) {
  if (!boxes.length || boxes.some(b => !Number.isInteger(b.rowIndex) || !Number.isInteger(b.colIndex))) return null;
  const minRow = Math.min(...boxes.map(b => b.rowIndex));
  const minCol = Math.min(...boxes.map(b => b.colIndex));
  const taken = new Set();
  const place = new Map();
  let cols = 0;
  let rows = 0;
  for (const b of boxes) {
    const row = b.rowIndex - minRow + 1;
    const col = b.colIndex - minCol + 1;
    const span = b.cellType === 'XL' ? 2 : 1;
    for (let r = row; r < row + span; r++) {
      if (taken.has(`${r}:${col}`)) return null;
      taken.add(`${r}:${col}`);
    }
    place.set(b.boxId, { gridColumn: col, gridRow: `${row} / span ${span}` });
    cols = Math.max(cols, col);
    rows = Math.max(rows, row + span - 1);
  }
  // Ô lưới không có cửa tủ (vd chỗ đặt màn hình cảm ứng của CAB-TU01) ⇒ vẽ tấm panel thay vì để trống.
  const empty = [];
  for (let r = 1; r <= rows; r++) {
    for (let c = 1; c <= cols; c++) if (!taken.has(`${r}:${c}`)) empty.push({ r, c });
  }
  // Ô drone nằm dưới bãi đáp trên nóc ⇒ vẽ dải "Bãi đáp drone" phủ đúng các cột đó.
  const droneCols = boxes.filter(b => b.cellType === 'DRONE').map(b => b.colIndex - minCol + 1);
  const pad = droneCols.length ? [Math.min(...droneCols), Math.max(...droneCols)] : null;
  return { cols, rows, place, empty, pad };
}

/** Khung tủ giống tủ thật: thân màu than chì, bãi đáp drone trên nóc, ô lưới không có cửa (chỗ màn hình) thành tấm Lock.R. */
function Cabinet({ layout, children }) {
  if (!layout) return <div className="box-grid auto">{children}</div>;
  return (
    <div className="cabinet" style={{ '--cols': layout.cols, '--rows': layout.rows }}>
      {layout.pad && (
        <div className="cabinet-roof">
          <div className="landing-pad" style={{ gridColumn: `${layout.pad[0]} / ${layout.pad[1] + 1}` }}>
            <Plane size={13} /> Bãi đáp drone
          </div>
        </div>
      )}
      <div className="cabinet-body">
        <div className="box-grid by-layout">
          {layout.empty.map(({ r, c }) => (
            <div key={`panel-${r}-${c}`} className="cabinet-panel" style={{ gridRow: r, gridColumn: c }}>
              <strong>Lock<span>.</span>R</strong>
              <small><Smartphone size={11} /> Màn hình kiosk</small>
            </div>
          ))}
          {children}
        </div>
      </div>
    </div>
  );
}

/** Nội dung một cửa ô: số ô lớn, biểu tượng loại ô, nhãn trạng thái có đèn báo. */
function DoorFace({ box, statusText, selected }) {
  const isDrone = box.cellType === 'DRONE' || box.type === 'DRONE';
  const isBig = ['LUGGAGE', 'XL'].includes(box.cellType) || box.type === 'LUGGAGE';
  const Icon = isDrone ? Plane : isBig ? Luggage : Package;
  return (
    <>
      <span className="door-head">
        <span className="box-number"><small>Ô</small>{box.boxNumber}</span>
        <Icon className="door-icon" size={20} />
      </span>
      {isBig && <span className="door-kind">Ô lớn · vali</span>}
      {isDrone && <span className="door-kind">Nhận hàng drone</span>}
      <span className="box-status"><i className="led" />{statusText}</span>
      {selected && <CheckCircle className="door-check" size={22} />}
    </>
  );
}

function BoxSelectionScreen({ go, goHome, jwt, userName, selectedBox, setSelectedBox, lockerInfo, activeLockerId, onReportBox }) {
  const [boxes, setBoxes] = useState([]);
  const [myOrders, setMyOrders] = useState([]);
  const [loading, setLoading] = useState(true);
  const [msg, setMsg] = useState('');
  
  // Manage Box State
  const [manageBox, setManageBox] = useState(null); // { box, order }
  const [manageLoading, setManageLoading] = useState(false);

  const isActiveManageableOrder = (order) => {
    if (!order) return false;
    const status = String(order.status || '').toUpperCase();
    const lockerId = order.lockerId ?? order.locker?.id;
    const hasCredential = Boolean(order.pinCode);
    const isPaid = Boolean(order.isPaid || String(order.paymentStatus || '').toUpperCase() === 'PAID');
    // Đơn INITIALIZED chỉ cho phép quản lý khi ĐÃ THANH TOÁN; đơn chưa thanh toán không được coi là ô của bạn
    const validStatus = status === 'INITIALIZED' ? isPaid : ['STORING', 'RETURNED'].includes(status);
    return validStatus
      && lockerId === activeLockerId
      && hasCredential;
  };

  const fetchData = useCallback(async () => {
    setLoading(true); setMsg('');
    try {
      const [boxesRes, ordersRes, layoutRes] = await Promise.all([
        api.getAllBoxes(activeLockerId, jwt),
        jwt ? api.getMyOrders(jwt) : Promise.resolve({ success: true, data: [] }),
        api.getLockerLayout(activeLockerId)
      ]);
      
      if (boxesRes.success && Array.isArray(boxesRes.data)) {
        let mergedBoxes = boxesRes.data;
        if (layoutRes.success && Array.isArray(layoutRes.data?.cells)) {
          mergedBoxes = mergedBoxes.map(b => {
             const cell = layoutRes.data.cells.find(c => c.boxNumber === b.boxNumber);
             return { ...b, cellType: cell?.cellType, rowIndex: cell?.rowIndex, colIndex: cell?.colIndex };
          });
        }
        setBoxes(mergedBoxes);
      } else {
        setMsg(boxesRes.message || 'Lỗi tải danh sách ô tủ');
      }
      
      if (ordersRes.success && Array.isArray(ordersRes.data)) {
        setMyOrders(ordersRes.data);
      }
    } catch (e) {
      setMsg('Lỗi kết nối máy chủ');
    } finally {
      setLoading(false);
    }
  }, [activeLockerId, jwt]);

  useEffect(() => {
    let ignore = false;
    fetchData().then(() => {
      if (ignore) return;
    });
    return () => { ignore = true; };
  }, [fetchData]);

  // Handle Box Click
  const handleBoxClick = (box, isAvail, myOrder) => {
    if (myOrder) {
      setManageBox({ box, order: myOrder });
    } else if (isAvail) {
      setSelectedBox({ ...box, boxId: box.boxId ?? box.id });
    }
  };

  const unlockManageBox = async () => {
    setManageLoading(true);
    try {
      const pinCode = manageBox.order.pinCode;
      if (!pinCode) {
        notify('Đơn này không còn mã PIN hoạt động. Vui lòng thao tác lại trên app Lock.R.', { tone: 'warn' });
        return;
      }
      const unlockRes = await api.unlockBox(
        activeLockerId,
        pinCode,
        manageBox.box.boxId,
        'PICKUP',
      );
      if (unlockRes.success && unlockRes.data?.accepted) {
        notify(unlockRes.data?.message || 'Ô đã mở. Vui lòng đóng cửa khi lấy đồ xong.', { tone: 'success', title: `Đã mở ô #${manageBox.box.boxNumber}` });
      } else {
        notify(unlockRes.data?.message || unlockRes.message || 'Không mở được ô, vui lòng thử lại.', { tone: 'error' });
      }
    } catch (e) {
      notify('Không kết nối được máy chủ. Vui lòng thử lại.', { tone: 'error', title: 'Mất kết nối' });
    } finally {
      setManageLoading(false);
    }
  };

  const endManageBox = async () => {
    const order = manageBox.order;
    if (String(order.type || '').toUpperCase() !== 'RENTAL') {
      notify('Chỉ đơn thuê ô mới kết thúc được tại đây.', { tone: 'warn' });
      return;
    }
    if (!(await ask('Bạn đã lấy hết đồ và đóng cửa ô? Kết thúc thuê sẽ trả ô lại cho hệ thống.', { tone: 'warn', title: 'Kết thúc thuê?', confirmText: 'Kết thúc thuê', cancelText: 'Chưa' }))) return;
    setManageLoading(true);
    try {
      const res = await api.endRental(jwt, order.id);
      if (res.success) {
        notify('Ô đã được trả lại. Cảm ơn bạn đã dùng Lock.R!', { tone: 'success', title: 'Đã kết thúc thuê' });
        setManageBox(null);
        await fetchData();
      } else {
        notify(res.data?.message || res.message || 'Không kết thúc thuê được, vui lòng thử lại.', { tone: 'error' });
      }
    } catch (e) {
      notify('Không kết nối được máy chủ. Vui lòng thử lại.', { tone: 'error', title: 'Mất kết nối' });
    } finally {
      setManageLoading(false);
    }
  };

  const notImplemented = () => {
    notify('Gia hạn thuê và ủy quyền người khác lấy hộ hiện thực hiện trên app Lock.R — mở mục Đơn tủ, chọn đơn này.', { tone: 'info', title: 'Thực hiện trên app Lock.R' });
  };

  // Ưu tiên dữ liệu box vừa fetch trong màn hình này. `lockerInfo.boxes` từ
  // parent chỉ poll best-effort mỗi 3s, có thể chậm hơn sau khi vừa đặt tủ.
  const displayBoxes = boxes.length > 0
    ? boxes
    : (lockerInfo?.boxes?.map(b => ({
        boxId: b.id || b.boxId,
        boxNumber: b.boxNumber,
        status: b.status,
        cellType: b.cellType,
        type: b.cellType,
      })) || []);

  // Tính số ô trống hiện tại để hiển thị
  // Ô DRONE chỉ nhận hàng drone thả (backend: DRONE_CELL_RESTRICTED) nên không tính là ô trống để thuê.
  const availableCount = displayBoxes.filter(b => b.status === 'AVAILABLE' && b.cellType !== 'DRONE' && b.type !== 'DRONE').length;
  const layout = boxPlacement(displayBoxes);
  // Không có toạ độ thật thì xếp theo số ô cho dễ tìm.
  const orderedBoxes = layout ? displayBoxes : [...displayBoxes].sort((a, b) => (a.boxNumber ?? 0) - (b.boxNumber ?? 0));
  const selectedKind = selectedBox && (['LUGGAGE', 'XL'].includes(selectedBox.cellType) ? 'Ô lớn (XL)' : 'Ô tiêu chuẩn');

  return (
    <div className="screen screen-boxes">
      <Header onBack={goHome} title="Chọn ô tủ" />
      <div className="boxes-layout">
        <aside className="boxes-side">
          {userName && (
            <div className="user-chip"><User size={15} /><span>{userName}</span></div>
          )}

          {lockerInfo && (
            <div className="boxes-locker">
              <div className="boxes-locker-name">
                <MapPin size={16} color="var(--accent)" />
                <span>{lockerInfo.storeName || lockerInfo.name}</span>
              </div>
              {lockerInfo.address && <div className="boxes-locker-address">{lockerInfo.address}</div>}
              <div className="boxes-locker-meta">
                {lockerInfo.code && <span className="boxes-chip">{lockerInfo.code}</span>}
                <span className="boxes-chip green"><CheckCircle size={13} /> {availableCount} ô trống</span>
                {displayBoxes.length > 0 && <span className="boxes-chip muted">{displayBoxes.length} ô</span>}
              </div>
            </div>
          )}

          <div className="box-legend">
            <span><i className="lg available" />Trống</span>
            <span><i className="lg selected" />Đang chọn</span>
            <span><i className="lg rented" />Đang dùng</span>
            <span><i className="lg drone" />Drone</span>
            <span><i className="lg offline" />Lỗi / bảo trì</span>
          </div>

          <div className={`box-picked ${selectedBox ? 'on' : ''}`}>
            {selectedBox
              ? <><Package size={18} /><span>Đã chọn <strong>Ô {selectedBox.boxNumber}</strong> · {selectedKind}</span></>
              : <span>Chạm vào một ô <strong>Trống</strong> bên cạnh để chọn</span>}
          </div>

          <Btn onClick={() => go('order-info')} disabled={!selectedBox}>
            Tiếp tục đặt tủ <ChevronRight size={18} />
          </Btn>
        </aside>

        <section className="boxes-main">
          {loading && displayBoxes.length === 0 && <p className="boxes-loading"><Loader2 size={20} style={{ animation: 'spin 0.6s linear infinite' }} />Đang tải...</p>}
          {msg && !loading && <Msg type="error" text={msg} />}

          <Cabinet layout={layout}>
        {orderedBoxes.map(box => {
          const myOrder = myOrders.find(o =>
            isActiveManageableOrder(o)
            && (o.sendBoxId === box.boxId || o.receiveBoxId === box.boxId)
          );
          
          const status = String(box.status || '').toUpperCase();
          const isDrone = box.cellType === 'DRONE' || box.type === 'DRONE';
          const isAvail = status === 'AVAILABLE' && !isDrone;
          // Chỉ AVAILABLE mới là "Trống". Trạng thái lạ (vd OUT_OF_SERVICE) trước đây rơi về "Trống"
          // nên ô trông như chọn được mà bấm không ăn.
          let statusText = 'Không dùng được';
          let boxClass = 'offline';

          if (myOrder) {
            statusText = 'Ô của bạn';
            boxClass = 'yours';
          } else if (isDrone) {
            statusText = 'Drone';
            boxClass = 'drone';
          } else if (isAvail) {
            statusText = 'Trống';
            boxClass = 'available';
          } else if (['RENTED', 'OCCUPIED', 'STORING'].includes(status)) {
            statusText = 'Đang dùng'; boxClass = 'rented';
          } else if (['BOOKED', 'RESERVED'].includes(status)) {
            statusText = 'Đã đặt'; boxClass = 'booked';
          } else if (status === 'FAULT') {
            statusText = 'Lỗi';
          } else if (status === 'PENDING_CHECK') {
            statusText = 'Chờ kiểm tra';
          } else if (['OUT_OF_SERVICE', 'MAINTENANCE', 'OFFLINE'].includes(status)) {
            statusText = 'Bảo trì';
          }

          const sel = selectedBox?.boxId === box.boxId;

          return (
            <button key={box.boxId}
              onClick={() => handleBoxClick(box, isAvail, myOrder)}
              disabled={!isAvail && !myOrder}
              style={layout?.place.get(box.boxId)}
              className={`box-item ${boxClass} ${sel ? 'selected' : ''}`}>
              <DoorFace box={box} statusText={sel ? 'Đang chọn' : statusText} selected={sel} />
            </button>
          );
        })}
          </Cabinet>
        </section>
      </div>

      {/* MANAGE BOX MODAL */}
      {manageBox && (
        <div className="modal-overlay">
          <div className="modal-content manage-box-modal">
            <button className="close-btn" onClick={() => setManageBox(null)} disabled={manageLoading}><X size={20}/></button>
            <h3 style={{marginTop: 8, marginBottom: 12}}>Quản lý Ô tủ #{manageBox.box.boxNumber}</h3>
            <div className="qr-container">
               <QRCodeSVG value={manageBox.order.qrToken || 'qr-placeholder'} size={110} />
               <div className="pin-code-display">
                 {(manageBox.order.pinCode || '').split('').map((char, i) => <span key={i}>{char}</span>)}
                </div>
               <p style={{fontSize: 12, color: 'var(--text-muted)'}}>Mã PIN / QR mở tủ của bạn</p>
            </div>
            
            <div className="manage-actions">
               <Btn onClick={unlockManageBox} disabled={manageLoading} style={{marginBottom: 8}}><Unlock size={18}/> Mở tủ</Btn>
               <Btn onClick={notImplemented} variant="secondary" disabled={manageLoading}><Clock size={18}/> Gia hạn thuê</Btn>
               <Btn onClick={endManageBox} variant="secondary" disabled={manageLoading}><LogOut size={18}/> Kết thúc thuê & trả ô</Btn>
               <Btn onClick={notImplemented} variant="secondary" disabled={manageLoading}><Users size={18}/> Ủy quyền người khác lấy hộ</Btn>
               <Btn onClick={() => onReportBox?.(manageBox.box.boxId)} variant="secondary" disabled={manageLoading}><AlertTriangle size={18}/> Báo ô lỗi</Btn>
            </div>
          </div>
        </div>
      )}
    </div>
  );
}

// ============================================
// ORDER INFO
// ============================================
function OrderInfoScreen({ go, back, jwt, selectedBox, setOrderId, setOrderPin, setOrderCode, setTotalPrice, activeLockerId }) {
  const [note, setNote] = useState('');
  const [hours, setHours] = useState(4);

  // Promo states
  const [promoCode, setPromoCode] = useState('');
  const [promoApplied, setPromoApplied] = useState(false);
  const [promoDiscount, setPromoDiscount] = useState(0);
  const [promoLoading, setPromoLoading] = useState(false);
  const [promoError, setPromoError] = useState('');
  const [promoDetail, setPromoDetail] = useState(null);

  const [loading, setLoading] = useState(false);
  const [msg, setMsg] = useState('');

  // Giá thuê và số giờ gợi ý do admin cấu hình (scope order, công khai). Chỉ để hiển thị:
  // backend luôn tự tính tiền. API lỗi ⇒ giữ mặc định của catalog.
  const [rates, setRates] = useState({ standard: 5000, xl: 10000, standardDaily: 40000, xlDaily: 80000 });
  const [hoursOptions, setHoursOptions] = useState([2, 4, 8, 12, 24]);
  // Giới hạn giờ thuê admin cho — mặc định giống app mobile (BusinessConfig): 1–72 giờ.
  const [limits, setLimits] = useState({ min: 1, max: 72 });
  // Chọn theo ngày (giờ luôn là bội của 24) như app mobile, hay theo giờ (dưới 1 ngày).
  const [byDay, setByDay] = useState(false);
  useEffect(() => {
    let alive = true;
    api.getPublicSettings('order').then(res => {
      const s = res?.data;
      if (!alive || !res?.success || !s) return;
      // Giá ngày được phép bằng 0 (= chỉ tính theo giờ) nên không dùng `||`.
      const daily = (key, fallback) => {
        const v = Number(s[key]);
        return s[key] != null && Number.isFinite(v) && v >= 0 ? v : fallback;
      };
      setRates(r => ({
        standard: Number(s['app.order.rental-rate-standard']) || r.standard,
        xl: Number(s['app.order.rental-rate-xl']) || r.xl,
        standardDaily: daily('app.order.rental-daily-rate-standard', r.standardDaily),
        xlDaily: daily('app.order.rental-daily-rate-xl', r.xlDaily),
      }));
      const int = (key, fallback) => {
        const v = Number(s[key]);
        return Number.isInteger(v) && v >= 1 ? v : fallback;
      };
      let min = int('app.order.rental-min-hours', 1);
      let max = int('app.order.rental-max-hours', 72);
      if (min > max) { min = 1; max = 72; }
      setLimits({ min, max });
      const raw = s['app.order.rental-quick-hours'];
      const quick = (Array.isArray(raw) ? raw : String(raw ?? '').split(','))
        .map(Number).filter(h => Number.isInteger(h) && h >= min && h <= max);
      if (quick.length) setHoursOptions(quick);
      // Mở màn ở chế độ theo giờ ⇒ số giờ mặc định kẹp dưới 1 ngày như mobile.
      const hourModeMax = max >= 24 ? Math.max(min, 23) : max;
      setByDay(false);
      setHours(Math.min(Math.max(int('app.order.rental-default-hours', 4), min), hourModeMax));
    }).catch(() => { /* giữ mặc định */ });
    return () => { alive = false; };
  }, []);

  // Cùng quy tắc với rent_locker_page.dart: theo giờ chỉ tới 23 giờ (khi được thuê ≥ 1 ngày),
  // theo ngày từ 1 tới số ngày tối đa = max giờ / 24.
  const maxDays = Math.floor(limits.max / 24);
  const maxHourMode = maxDays >= 1 ? Math.max(limits.min, 23) : limits.max;
  const hourQuick = hoursOptions.filter(h => h >= limits.min && h <= maxHourMode);
  const dayQuick = [1, 2, 3, 7].filter(d => d <= maxDays);
  const days = Math.max(1, Math.ceil(hours / 24));
  const clampHourMode = (h) => Math.min(Math.max(h, limits.min), maxHourMode);
  const setDays = (d) => setHours(Math.min(Math.max(d, 1), maxDays) * 24);
  const switchMode = (toDay) => {
    if (toDay === byDay) return;
    setByDay(toDay);
    setHours(toDay ? 24 : clampHourMode(hourQuick[0] ?? limits.min));
  };

  const isXl = selectedBox?.cellType === 'XL';
  const HOURLY_RATE = isXl ? rates.xl : rates.standard;
  // Giá 24 giờ, kẹp về tối đa 24 × giá giờ — cùng công thức OrderRules.rentalPrice ở server.
  const DAILY_RATE = Math.min(isXl ? rates.xlDaily : rates.standardDaily, HOURLY_RATE * 24);
  const subtotal = DAILY_RATE > 0
    ? Math.floor(hours / 24) * DAILY_RATE + Math.min((hours % 24) * HOURLY_RATE, DAILY_RATE)
    : hours * HOURLY_RATE;

  // Giảm giá tính lại mỗi khi đổi số giờ; mã không còn đạt đơn tối thiểu thì báo và không trừ.
  const promoMinOrder = Number(promoDetail?.minOrderAmount) || 0;
  const promoBelowMin = promoApplied && promoMinOrder > 0 && subtotal < promoMinOrder;
  useEffect(() => {
    if (!promoApplied || !promoDetail || promoBelowMin) { setPromoDiscount(0); return; }
    let discount = 0;
    if (promoDetail.discountType === 'PERCENTAGE') {
      discount = Math.round(subtotal * Number(promoDetail.discountValue) / 100);
      if (promoDetail.maxDiscountAmount) discount = Math.min(discount, Number(promoDetail.maxDiscountAmount));
    } else if (promoDetail.discountType === 'FIXED_AMOUNT') {
      discount = Number(promoDetail.discountValue);
    }
    setPromoDiscount(Math.min(discount, subtotal));
  }, [promoApplied, promoDetail, promoBelowMin, subtotal]);
  const total = Math.max(0, subtotal - promoDiscount);

  const handleApplyPromo = async () => {
    const code = promoCode.trim();
    if (!code) return;

    setPromoLoading(true); setPromoError(''); setPromoApplied(false);
    setPromoDiscount(0); setPromoDetail(null);

    try {
      // Backend trả { code, valid, reason, promotion } — trước đây đọc nhầm cả gói là voucher nên mã
      // nào cũng hiện "đã áp dụng".
      const res = await api.validatePromotionCode(code, jwt, activeLockerId, subtotal);
      const result = res?.data;
      if (res?.success && result?.valid && result.promotion) {
        setPromoDetail(result.promotion);
        setPromoApplied(true);
      } else {
        setPromoError(result?.reason || res?.message || 'Mã không hợp lệ');
      }
    } catch (err) {
      setPromoError('Lỗi kiểm tra mã, vui lòng thử lại');
    }
    setPromoLoading(false);
  };

  const handleCreate = async () => {
    if (!selectedBox) { setMsg('Chưa chọn ô tủ'); return; }
    setLoading(true); setMsg('');
    try {
      // Dùng POST /api/orders/rental — gateway gắn X-User-Id từ JWT
      // → đơn hàng liên kết đúng với user, hiển thị trong "Đơn tủ" trên mobile
      const payload = {
        lockerId: activeLockerId,
        boxId: selectedBox.boxId,   // LockerBoxSummary.boxId (không phải .id)
        // Giá thuê tính theo loại ô: chỉ XL có giá riêng, còn lại tính như STANDARD.
        cellType: selectedBox.cellType === 'XL' ? 'XL' : 'STANDARD',
        hours: hours,
        note: note || undefined,
        promotionCode: (promoApplied && !promoBelowMin && promoCode.trim()) ? promoCode.trim() : undefined,
      };

      console.log('%c[ORDER] Creating RENTAL order via /api/orders/rental:', 'color:#fbbf24', payload);
      const res = await api.createRentalOrder(jwt, payload);
      if (res.success && res.data) {
        console.log('%c[ORDER] ✅ Created:', 'color:#4ade80', { id: res.data.id, pin: res.data.pinCode, code: res.data.orderCode });
        setOrderId(res.data.id);
        setOrderPin(res.data.pinCode || '');
        setOrderCode(res.data.orderCode || '');
        setTotalPrice(res.data.totalPrice ?? total);
        go('payment');
      } else {
        setMsg(res.data?.message || res.message || 'Lỗi tạo đơn hàng. Vui lòng thử lại.');
      }
    } catch (e) {
      console.error('[ORDER] Error:', e);
      setMsg('Lỗi kết nối server');
    }
    setLoading(false);
  };

  const fmt = (p) => new Intl.NumberFormat('vi-VN').format(p) + 'đ';
  // Giá từng mức giờ, cùng công thức với `subtotal` ở trên — để khách so sánh ngay trên nút.
  const priceFor = (h) => (DAILY_RATE > 0
    ? Math.floor(h / 24) * DAILY_RATE + Math.min((h % 24) * HOURLY_RATE, DAILY_RATE)
    : h * HOURLY_RATE);
  const clearPromo = () => { setPromoCode(''); setPromoApplied(false); setPromoError(''); setPromoDiscount(0); setPromoDetail(null); };

  return (
    <div className="screen screen-order">
      <Header onBack={back} title="Thông tin đặt tủ" />
      <div className="order-layout">
        <section className="order-form">
          {/* Số giờ thuê */}
          <div className="order-block">
            <div className="order-block-head">
              <div className="order-block-title"><Clock size={16} /> Thời gian thuê</div>
              {maxDays >= 1 && (
                <div className="mode-toggle" role="tablist">
                  <button className={!byDay ? 'active' : ''} onClick={() => switchMode(false)}><Clock size={15} /> Theo giờ</button>
                  <button className={byDay ? 'active' : ''} onClick={() => switchMode(true)}><CalendarDays size={15} /> Theo ngày</button>
                </div>
              )}
            </div>

            <div className="hour-options">
              {(byDay ? dayQuick.map(d => d * 24) : hourQuick).map(h => (
                <button key={h} className={`hour-option ${hours === h ? 'active' : ''}`} onClick={() => setHours(h)}>
                  <span className="hour-option-h">{byDay ? `${h / 24} ngày` : `${h} giờ`}</span>
                  <span className="hour-option-p">{fmt(priceFor(h))}</span>
                </button>
              ))}
            </div>

            <div className="duration-stepper">
              <button
                onClick={() => (byDay ? setDays(days - 1) : setHours(clampHourMode(hours - 1)))}
                disabled={byDay ? days <= 1 : hours <= limits.min}
                aria-label={byDay ? 'Bớt 1 ngày' : 'Bớt 1 giờ'}><Minus size={20} /></button>
              <div className="duration-stepper-value">
                <strong>{byDay ? `${days} ngày` : `${hours} giờ`}</strong>
                {byDay && <span>{days * 24} giờ</span>}
              </div>
              <button
                onClick={() => (byDay ? setDays(days + 1) : setHours(clampHourMode(hours + 1)))}
                disabled={byDay ? days >= maxDays : hours >= maxHourMode}
                aria-label={byDay ? 'Thêm 1 ngày' : 'Thêm 1 giờ'}><Plus size={20} /></button>
            </div>
            <div className="order-rate">
              Đơn giá {fmt(HOURLY_RATE)}/giờ{DAILY_RATE > 0 && <> · tối đa {fmt(DAILY_RATE)}/ngày</>}
            </div>
          </div>

          <div className="order-row-2">
            {/* Ghi chú */}
            <div className="order-block">
              <div className="order-block-title"><ClipboardList size={16} /> Ghi chú <span className="order-optional">tuỳ chọn</span></div>
              <input className="input" value={note} onChange={e => setNote(e.target.value)} placeholder="Ví dụ: Quần áo, cần giữ cẩn thận..." />
            </div>

            {/* Mã giảm giá */}
            <div className="order-block">
              <div className="order-block-title"><CreditCard size={16} /> Mã giảm giá <span className="order-optional">tuỳ chọn</span></div>
              <div className="promo-row">
                <input className="input" value={promoCode} onChange={e => {
                  setPromoCode(e.target.value.toUpperCase());
                  if (promoApplied || promoError) { setPromoApplied(false); setPromoError(''); setPromoDiscount(0); setPromoDetail(null); }
                }} placeholder="Nhập mã khuyến mãi" style={{ textTransform: 'uppercase' }} disabled={promoApplied || promoLoading} />
                {!promoApplied ? (
                  <button className="btn btn-primary promo-btn" onClick={handleApplyPromo} disabled={!promoCode.trim() || promoLoading}>
                    {promoLoading ? <Loader2 size={18} style={{ animation: 'spin 0.6s linear infinite' }} /> : 'Áp dụng'}
                  </button>
                ) : (
                  <button className="btn btn-secondary promo-btn" onClick={clearPromo}>
                    <X size={18} /> Gỡ mã
                  </button>
                )}
              </div>
              {promoError && <div className="promo-msg error"><AlertTriangle size={14} /> {promoError}</div>}
              {promoApplied && promoDetail && (
                <div className={`promo-msg ${promoBelowMin ? 'warn' : 'ok'}`}>
                  <CheckCircle size={14} />
                  <span>
                    <strong>{promoDetail.title || promoDetail.name}</strong>
                    {promoBelowMin
                      ? <> — cần đơn tối thiểu {fmt(promoMinOrder)}, hãy tăng số giờ thuê hoặc gỡ mã.</>
                      : <> — đã giảm {fmt(promoDiscount)}</>}
                  </span>
                </div>
              )}
            </div>
          </div>
        </section>

        <aside className="order-summary">
          <div className="order-summary-title">Tóm tắt đơn thuê</div>
          {selectedBox && (
            <div className="order-summary-box">
              <div className="order-summary-box-icon">
                {isXl ? <Luggage size={22} /> : <Package size={22} />}
              </div>
              <div>
                <div className="order-summary-box-no">Ô {selectedBox.boxNumber}</div>
                <div className="order-summary-box-kind">{isXl ? 'Ô lớn (XL)' : 'Ô tiêu chuẩn'}</div>
              </div>
            </div>
          )}
          <div className="order-summary-rows">
            <div><span>Thời gian thuê</span><strong>{byDay ? `${days} ngày (${hours} giờ)` : `${hours} giờ`}</strong></div>
            <div><span>Tiền thuê</span><strong>{fmt(subtotal)}</strong></div>
            {promoApplied && promoDiscount > 0 && (
              <div className="discount"><span>Khuyến mãi</span><strong>-{fmt(promoDiscount)}</strong></div>
            )}
          </div>
          <div className="order-summary-total">
            <span>Tổng cộng</span>
            <strong>{fmt(total)}</strong>
          </div>
          <p className="order-summary-note">Giờ thuê bắt đầu tính khi bạn xác nhận đã cất đồ vào ô.</p>
          <Btn onClick={handleCreate} loading={loading}><ShieldCheck size={18} /> Đặt tủ ngay</Btn>
          {msg && <Msg type="error" text={msg} />}
        </aside>
      </div>
    </div>
  );
}




// ============================================
// PAYMENT (chỉ chuyển khoản VietQR qua SePay)
// ============================================
// Chỉ mở ô khi server báo đơn đã PAID (GET /api/orders/{id}/status → isPaid): client không bao giờ
// tự coi là đã trả. Mã lỗi theo payment-service (PaymentService.checkout).
const PAY_ERROR_TEXT = {
  PAYMENT_METHOD_DISABLED: 'Thanh toán chuyển khoản đang tạm tắt. Vui lòng liên hệ hỗ trợ.',
  PAYMENT_METHOD_INVALID: 'Phương thức thanh toán không hợp lệ.',
  ORDER_LOOKUP_FAILED: 'Không lấy được thông tin đơn. Vui lòng thử lại.',
};
const payErrorText = (res, fallback) => PAY_ERROR_TEXT[res?.code] || res?.message || fallback;

const SEPAY_POLL_MS = 3000;               // hỏi trạng thái đơn mỗi 3 s
const SEPAY_WAIT_MS = 10 * 60 * 1000;     // chờ chuyển khoản tối đa 10 phút (bấm "Kiểm tra lại" để chờ tiếp)

function PaymentScreen({ go, goHome, jwt, orderId, orderPin, orderCode, totalPrice, selectedBox, showSuccess, activeLockerId, pinContact }) {
  const [loading, setLoading] = useState('');
  const [msg, setMsg] = useState('');
  const [sepay, setSepay] = useState(null);                // giao dịch VietQR đang chờ: { paymentId, referenceId, amount, qrImage, qrText, payLink }
  const [sepayPhase, setSepayPhase] = useState('waiting'); // waiting | paid | expired | failed
  const [sepayDeadline, setSepayDeadline] = useState(0);
  const [qrImgFailed, setQrImgFailed] = useState(false);
  const [now, setNow] = useState(() => Date.now());
  const [paidConfirmed, setPaidConfirmed] = useState(false);
  const [dropOpened, setDropOpened] = useState(false);
  const aliveRef = useRef(true);
  const openAfterPaidRef = useRef(null);
  // Nút "Giả lập thanh toán" (test) — chỉ hiện khi admin bật app.payment.simulation-enabled.
  const [simEnabled, setSimEnabled] = useState(false);
  const [simAmount, setSimAmount] = useState('');
  const [simulating, setSimulating] = useState(false);

  useEffect(() => {
    let alive = true;
    api.getPublicSettings('payment')
      .then(res => { if (alive) setSimEnabled(res?.data?.['app.payment.simulation-enabled'] === true); })
      .catch(() => {});
    return () => { alive = false; };
  }, []);

  // Khách rời màn (về trang chủ) giữa chừng thì không mở ô cho người đã đi.
  useEffect(() => {
    aliveRef.current = true;
    return () => { aliveRef.current = false; };
  }, []);

  const fmt = (p) => new Intl.NumberFormat('vi-VN').format(p) + 'đ';

  // Thanh toán được ghi nhận bất đồng bộ (qua sự kiện payment → order), nên phải đợi đơn
  // thật sự PAID rồi mới mở ô — backend từ chối mở ô bỏ hàng khi đơn chưa thanh toán.
  const waitUntilPaid = async (timeoutMs = 15000) => {
    const deadline = Date.now() + timeoutMs;
    while (Date.now() < deadline && aliveRef.current) {
      try {
        const res = await api.getOrderStatus(orderId, jwt);
        if (res.success && res.data?.isPaid) return true;
      } catch { /* thử lại */ }
      await new Promise(resolve => setTimeout(resolve, 1500));
    }
    return false;
  };

  // Mở ô để bỏ đồ; xác nhận "đã bỏ đồ" là bước riêng sau khi khách đặt đồ vào.
  const openForDrop = async () => {
    if (!aliveRef.current) return;
    const res = await api.unlockBox(activeLockerId, orderPin, selectedBox?.boxId, 'DROP_OFF');
    if (res.success && res.data?.accepted) {
      setDropOpened(true);
    } else {
      setMsg(res.data?.message || res.message || 'Lỗi mở tủ');
    }
  };

  // Server đã báo PAID: sang bước mở ô (mở lỗi thì còn nút "Mở ô tủ" để thử lại, không trả lại lần nữa).
  const openAfterPaid = async () => {
    if (!aliveRef.current) return;
    setPaidConfirmed(true);
    setLoading('open'); setMsg('');
    try {
      await openForDrop();
    } catch { setMsg('Lỗi kết nối'); }
    setLoading('');
  };
  openAfterPaidRef.current = openAfterPaid;

  // Sau checkout (hoặc khi server báo đơn đã được trả trước đó): đợi server ghi nhận PAID rồi mới mở ô.
  const openWhenServerPaid = async () => {
    if (await waitUntilPaid()) {
      await openAfterPaid();
    } else if (aliveRef.current) {
      setMsg('Chưa ghi nhận được thanh toán. Vui lòng thử lại sau giây lát.');
    }
  };

  const startSepay = async () => {
    setLoading('SEPAY'); setMsg('');
    try {
      const res = await api.checkoutSepay(jwt, orderId);
      if (res.success && res.data) {
        const d = res.data;
        // qrCodeUrl thường là link ảnh VietQR; nếu server chỉ trả chuỗi QR thì tự vẽ bằng qrcode.react.
        const qr = typeof d.qrCodeUrl === 'string' ? d.qrCodeUrl.trim() : '';
        const qrIsImage = /^https?:\/\//i.test(qr);
        const next = {
          paymentId: d.id,
          referenceId: d.referenceId || '',
          amount: Number(d.amount ?? totalPrice) || 0,
          qrImage: qrIsImage ? qr : '',
          qrText: qrIsImage ? '' : qr,
          payLink: typeof d.paymentUrl === 'string' ? d.paymentUrl : '',
        };
        if (!next.qrImage && !next.qrText && !next.payLink) {
          setMsg('Không tạo được mã VietQR. Vui lòng chọn cách thanh toán khác.');
        } else {
          setQrImgFailed(false);
          setSepayPhase('waiting');
          setSepayDeadline(Date.now() + SEPAY_WAIT_MS);
          setNow(Date.now());
          setSepay(next);
        }
      } else if (res.code === 'ORDER_ALREADY_PAID') {
        await openWhenServerPaid();
      } else {
        setMsg(payErrorText(res, 'Không tạo được mã VietQR. Vui lòng thử lại.'));
      }
    } catch { setMsg('Lỗi kết nối'); }
    setLoading('');
  };

  // Chỉ có một cách trả (SePay) ⇒ vào màn là tạo mã VietQR ngay, đúng một lần (StrictMode chạy effect hai lần).
  const autoStartedRef = useRef(false);
  useEffect(() => {
    if (autoStartedRef.current) return;
    autoStartedRef.current = true;
    startSepay();
  }, []); // eslint-disable-line react-hooks/exhaustive-deps

  const [canceling, setCanceling] = useState(false);

  // Khách bấm Hủy hoặc Back: hủy đơn trên backend để giải phóng ô ngay lập tức
  const handleCancelPayment = async () => {
    if (canceling) return;
    setCanceling(true);
    if (orderId) {
      try {
        await api.cancelOrder(jwt, orderId);
        console.log('%c[PAYMENT] Canceled unpaid order and released box:', 'color:#f87171', orderId);
      } catch (err) {
        console.error('Failed to cancel order on exit:', err);
      }
    }
    setCanceling(false);
    goHome();
  };

  // Coi như vừa nhận `simAmount` đồng (trống = đủ số) cho giao dịch này; vòng hỏi trạng thái tự mở ô khi PAID.
  const simulateSepay = async () => {
    if (!sepay?.paymentId || simulating) return;
    setSimulating(true); setMsg('');
    try {
      const amount = Number(simAmount) > 0 ? Number(simAmount) : undefined;
      const res = await api.simulatePayment(jwt, { paymentId: sepay.paymentId, amount });
      if (!res?.success) setMsg(payErrorText(res, 'Không giả lập được thanh toán.'));
    } catch { setMsg('Lỗi kết nối'); }
    setSimulating(false);
  };

  const recheckSepay = () => {
    setMsg('');
    setSepayDeadline(Date.now() + SEPAY_WAIT_MS);
    setNow(Date.now());
    setSepayPhase('waiting');
  };

  // Hỏi tuần tự (xong lượt này mới hẹn lượt sau). Đơn PAID ⇒ mở ô; giao dịch FAILED (chuyển thiếu tiền) ⇒ báo lỗi.
  useEffect(() => {
    if (!sepay || sepayPhase !== 'waiting') return undefined;
    let alive = true;
    let timer = null;
    const tick = async () => {
      let paid = false;
      let failed = false;
      try {
        const st = await api.getOrderStatus(orderId, jwt);
        paid = Boolean(st?.success && st.data?.isPaid);
        if (!paid && sepay.paymentId != null) {
          const p = await api.getPayment(jwt, sepay.paymentId);
          failed = Boolean(p?.success) && String(p.data?.status || '').toUpperCase() === 'FAILED';
        }
      } catch { /* mạng chập chờn: hỏi lại lượt sau */ }
      if (!alive) return;
      if (paid) { setSepayPhase('paid'); openAfterPaidRef.current?.(); return; }
      if (failed) { setSepayPhase('failed'); return; }
      if (Date.now() >= sepayDeadline) { setSepayPhase('expired'); return; }
      timer = setTimeout(tick, SEPAY_POLL_MS);
    };
    timer = setTimeout(tick, SEPAY_POLL_MS);
    return () => { alive = false; clearTimeout(timer); };
  }, [sepay, sepayPhase, sepayDeadline, orderId, jwt]);

  useEffect(() => {
    if (!sepay || sepayPhase !== 'waiting') return undefined;
    const id = setInterval(() => setNow(Date.now()), 1000);
    return () => clearInterval(id);
  }, [sepay, sepayPhase]);

  const boxNo = selectedBox?.boxNumber;

  if (dropOpened) {
    return (
      <AfterOpenPanel
        nextStep="CONFIRM_DROP"
        accessCode={orderPin}
        lockerId={activeLockerId}
        boxLabel={boxNo}
        showSuccess={showSuccess}
        goHome={goHome}
        pinNotice={<PinNotice pin={orderPin} contact={pinContact} compact />}
        successExtra={{ orderCode, orderPin, pinContact }}
        dropDoneMessage="Đã bắt đầu thuê. Dùng mã bên dưới để mở lại ô trong thời gian thuê." />
    );
  }

  if (paidConfirmed) {
    return (
      <div className="screen">
        <Header onBack={goHome} title="Thanh toán" />
        <div style={{ maxWidth: 450, margin: '0 auto', width: '100%', display: 'flex', flexDirection: 'column', gap: 12 }}>
          <Msg type="success" text={`Đã nhận thanh toán cho đơn ${orderCode || `#${orderId}`}.`} />
          <PinNotice pin={orderPin} contact={pinContact} />
          {loading === 'open' ? (
            <p className="polling-status" style={{ justifyContent: 'center', fontSize: 14 }}>
              <Loader2 size={16} style={{ animation: 'spin 0.6s linear infinite' }} />Đang mở ô tủ #{boxNo}...
            </p>
          ) : (
            <Btn onClick={openAfterPaid}><Unlock size={18} /> Mở ô tủ #{boxNo}</Btn>
          )}
          {msg && <Msg type="error" text={msg} />}
        </div>
      </div>
    );
  }

  if (sepay) {
    const left = Math.max(0, Math.ceil((sepayDeadline - now) / 1000));
    const mmss = `${Math.floor(left / 60)}:${String(left % 60).padStart(2, '0')}`;
    const showImage = Boolean(sepay.qrImage) && !qrImgFailed;
    const qrValue = showImage ? '' : (sepay.qrText || sepay.payLink);
    const viaLink = !showImage && !sepay.qrText && Boolean(sepay.payLink);

    return (
      <div className="screen">
        <Header onBack={handleCancelPayment} title="Chuyển khoản VietQR" />
        <div className="vietqr-pay">
          <div className={`vietqr-code ${showImage || qrValue ? '' : 'idle'}`}>
            {showImage ? (
              <img src={sepay.qrImage} alt="Mã VietQR" onError={() => setQrImgFailed(true)} />
            ) : qrValue ? (
              <QRCodeSVG value={qrValue} size={220} level="M" marginSize={2} />
            ) : (
              <><QrCode size={48} /><span>Không tải được mã QR</span></>
            )}
          </div>

          <div className="vietqr-info">
            <h3>{viaLink ? 'Quét bằng camera điện thoại để mở trang thanh toán' : 'Mở app ngân hàng và quét mã VietQR'}</h3>
            <div className="vietqr-field">
              <span>Số tiền</span>
              <strong>{fmt(sepay.amount)}</strong>
            </div>
            {sepay.referenceId && (
              <div className="vietqr-field">
                <span>Nội dung chuyển khoản</span>
                <code>{sepay.referenceId}</code>
              </div>
            )}
            <p className="vietqr-note">
              Giữ nguyên số tiền và nội dung chuyển khoản. Kiosk tự mở ô #{boxNo} khi hệ thống nhận được tiền — đã chuyển rồi thì đừng bấm Huỷ.
            </p>

            {sepayPhase === 'waiting' && (
              <>
                <div className={`qr-login-timer ${left <= 60 ? 'warn' : ''}`}>
                  <Clock size={16} /> Chờ thanh toán thêm <strong>{mmss}</strong>
                </div>
                <p className="polling-status"><Loader2 size={14} style={{ animation: 'spin 0.6s linear infinite' }} />Đang chờ xác nhận chuyển khoản...</p>
              </>
            )}
            {sepayPhase === 'paid' && (
              <p className="polling-status"><Loader2 size={14} style={{ animation: 'spin 0.6s linear infinite' }} />Đã nhận tiền, đang mở ô tủ...</p>
            )}
            {sepayPhase === 'expired' && <Msg type="error" text="Hết thời gian chờ. Nếu bạn đã chuyển khoản, bấm Kiểm tra lại." />}
            {sepayPhase === 'failed' && <Msg type="error" text="Giao dịch chuyển khoản không hợp lệ (số tiền không khớp với đơn). Vui lòng liên hệ hỗ trợ Lock.R." />}
            {msg && <Msg type="error" text={msg} />}

            {simEnabled && sepay.paymentId != null && sepayPhase === 'waiting' && (
              <div className="vietqr-simulate">
                <input
                  type="number"
                  inputMode="numeric"
                  min="1"
                  placeholder={String(sepay.amount)}
                  value={simAmount}
                  onChange={e => setSimAmount(e.target.value)}
                  aria-label="Số tiền giả lập"
                />
                <Btn variant="outline" onClick={simulateSepay} loading={simulating}>
                  <FlaskConical size={18} /> Giả lập thanh toán (test)
                </Btn>
              </div>
            )}

            <div className="vietqr-actions">
              {sepayPhase === 'expired' && <Btn onClick={recheckSepay}><RefreshCw size={18} /> Kiểm tra lại</Btn>}
              <Btn variant="secondary" onClick={handleCancelPayment} disabled={sepayPhase === 'paid'} loading={canceling}><X size={18} /> Huỷ &amp; Trả lại ô tủ</Btn>
            </div>
          </div>
        </div>
      </div>
    );
  }

  return (
    <div className="screen">
      <Header onBack={handleCancelPayment} title="Thanh toán" />
      <div style={{ maxWidth: 500, margin: '0 auto', width: '100%' }}>
        <div className="payment-3d-hero">
          <img src={mobilePay3d} alt="Payment" className="payment-3d-img" />
          <div>
            <h3 style={{ fontSize: 16, fontWeight: 800, margin: '0 0 4px 0', color: 'var(--text-primary)' }}>
              Xác nhận thanh toán
            </h3>
            <p style={{ fontSize: 13, color: 'var(--text-secondary)', margin: 0 }}>
              Chuyển khoản VietQR để mở ô tủ <strong>#{boxNo}</strong>
            </p>
          </div>
        </div>

        <div className="order-sum" style={{ marginBottom: 20 }}>
          <div className="order-row"><ClipboardList size={16} /> Mã đơn: <strong>{orderCode}</strong></div>
          <div className="order-row"><KeyRound size={16} /> PIN dự phòng: <strong>{orderPin}</strong></div>
          <div className="order-row"><CreditCard size={16} /> Cước phí: <strong style={{ color: 'var(--accent)', fontSize: 16 }}>{fmt(totalPrice)}</strong></div>
          {selectedBox && <div className="order-row"><Package size={16} /> Ô tủ được cấp: <strong>#{selectedBox.boxNumber}</strong></div>}
        </div>

        <div style={{ display: 'flex', flexDirection: 'column', gap: 12 }}>
          {/* Màn tự tạo mã VietQR khi mở; nút này để tạo lại khi lần tạo trước lỗi. */}
          <Btn onClick={startSepay} loading={loading === 'SEPAY'} disabled={!!loading}>
            <QrCode size={18} /> Chuyển khoản VietQR
          </Btn>
          <Btn variant="outline" onClick={handleCancelPayment} loading={canceling} style={{ borderColor: '#ef4444', color: '#ef4444' }}>
            <X size={18} /> Hủy đặt &amp; Trả lại ô tủ
          </Btn>
        </div>
        {msg && <Msg type="error" text={msg} />}
      </div>
    </div>
  );
}

// ============================================
// SAU KHI Ô MỞ BẰNG MÃ
// ============================================
// Backend trả `nextStep` khi mở ô: CONFIRM_DROP (vừa mở để bỏ hàng — hỏi đã bỏ xong chưa),
// RENTAL_ACCESS (ô đang thuê — dùng tiếp hay kết thúc thuê). Hai thao tác này chỉ dùng mã,
// backend chấp nhận vì ô của đơn vừa được mở thành công.
function AfterOpenPanel({ nextStep, accessCode, lockerId, boxLabel, showSuccess, goHome, dropDoneMessage, pinNotice, successExtra }) {
  const [loading, setLoading] = useState('');
  const [msg, setMsg] = useState('');

  const run = async (key, action, title, message, extra = null) => {
    setLoading(key); setMsg('');
    try {
      const res = await action(lockerId, accessCode);
      if (res.success && res.data?.accepted) {
        showSuccess(title, message || res.data.message, { boxNumber: boxLabel, ...extra });
      } else {
        setMsg(res.data?.message || res.message || 'Không thực hiện được, vui lòng thử lại');
      }
    } catch {
      setMsg('Lỗi kết nối server');
    }
    setLoading('');
  };

  const panel = { maxWidth: 450, margin: '0 auto', width: '100%', display: 'flex', flexDirection: 'column', gap: 12 };

  if (nextStep === 'CONFIRM_DROP') {
    return (
      <div className="screen">
        <Header onBack={goHome} title={`Ô Tủ #${boxLabel}`} />
        <p className="subtitle" style={{ textAlign: 'center' }}>
          Ô đã mở. Đặt đồ vào ô, <strong>đóng cửa</strong> rồi bấm xác nhận.
        </p>
        <div style={panel}>
          {pinNotice}
          <Btn onClick={() => run('drop', api.confirmDropWithCode, 'Đã bỏ hàng!', dropDoneMessage, successExtra)} loading={loading === 'drop'}>
            <CheckCircle size={18} /> Đã bỏ hàng xong
          </Btn>
          <Btn variant="secondary" onClick={goHome} disabled={!!loading}>
            Để sau — xác nhận trên ứng dụng
          </Btn>
          {msg && <Msg type="error" text={msg} />}
        </div>
      </div>
    );
  }

  // RENTAL_ACCESS: mở ô KHÔNG kết thúc thuê (backend chỉ kết thúc qua end-rental-with-code). Trước đây nút
  // chính là "Đóng cửa, tiếp tục thuê" (icon ổ khoá, tiêu đề "Đã mở khóa!") — khách tới lấy đồ bấm nút to
  // nhất, đơn nằm lại STORING. Giờ nút chính là kết thúc thuê, có hỏi lại; nút giữ thuê nói rõ đơn chưa xong.
  const endRental = async () => {
    if (!(await ask('Bạn đã lấy hết đồ và đóng cửa ô? Kết thúc thuê sẽ trả ô lại cho hệ thống, mã mở ô hết hiệu lực.', {
      tone: 'warn', title: 'Kết thúc thuê?', confirmText: 'Kết thúc thuê', cancelText: 'Chưa',
    }))) return;
    run('end', api.endRentalWithCode, 'Đã kết thúc thuê!', 'Đơn thuê đã hoàn tất, ô đã được trả lại. Cảm ơn bạn đã dùng Lock.R!');
  };

  return (
    <div className="screen">
      <Header onBack={goHome} title={`Ô Tủ #${boxLabel}`} />
      <p className="subtitle" style={{ textAlign: 'center' }}>
        Ô đã mở. Lấy hết đồ thì bấm <strong>kết thúc thuê</strong> để hoàn tất đơn — chỉ mở ô thôi thì đơn vẫn đang thuê.
      </p>
      <div style={panel}>
        <Btn onClick={endRental} loading={loading === 'end'}>
          <LogOut size={18} /> Lấy hết đồ &amp; kết thúc thuê
        </Btn>
        <Btn
          variant="secondary"
          onClick={() => showSuccess(
            'Vẫn đang thuê ô',
            'Đơn thuê chưa kết thúc. Nhớ đóng cửa ô; mã mở ô vẫn dùng được tới khi hết hạn thuê. Khi lấy hết đồ, nhập lại mã và chọn "kết thúc thuê".',
            { boxNumber: boxLabel },
          )}
          disabled={!!loading}>
          <Unlock size={18} /> Chỉ lấy/cất đồ — tiếp tục thuê
        </Btn>
        {msg && <Msg type="error" text={msg} />}
      </div>
    </div>
  );
}

const needsFollowUp = (nextStep) => nextStep === 'CONFIRM_DROP' || nextStep === 'RENTAL_ACCESS';

// ============================================
// REPORT (báo ô tủ hư)
// ============================================
const REPORT_REASONS = [
  'Cửa ô không mở',
  'Cửa không đóng / không khoá được',
  'Ô bị hư hỏng',
  'Ô bẩn / có mùi lạ',
  'Màn hình / bàn phím tủ lỗi',
  'Khác',
];
const WHOLE_LOCKER = 'LOCKER';

function ReportScreen({ goHome, jwt, userName, lockerInfo, activeLockerId, initialBoxId, showSuccess }) {
  const [boxes, setBoxes] = useState([]);
  const [target, setTarget] = useState(initialBoxId ?? null);   // boxId | WHOLE_LOCKER | null
  const [reason, setReason] = useState('');
  const [detail, setDetail] = useState('');
  const [loading, setLoading] = useState(true);
  const [sending, setSending] = useState(false);
  const [msg, setMsg] = useState('');

  useEffect(() => {
    let alive = true;
    api.getLockerLayout(activeLockerId)
      .then(res => {
        if (!alive) return;
        if (res.success && Array.isArray(res.data?.cells)) {
          setBoxes(res.data.cells.map(c => ({ ...c, boxId: c.id ?? c.boxId })));
        } else setMsg(res.message || 'Không tải được sơ đồ tủ');
      })
      .catch(() => { if (alive) setMsg('Không kết nối được máy chủ'); })
      .finally(() => { if (alive) setLoading(false); });
    return () => { alive = false; };
  }, [activeLockerId]);

  const layout = boxPlacement(boxes);
  const ordered = layout ? boxes : [...boxes].sort((a, b) => (a.boxNumber ?? 0) - (b.boxNumber ?? 0));
  const targetBox = target && target !== WHOLE_LOCKER ? boxes.find(b => b.boxId === target) : null;
  const where = target === WHOLE_LOCKER ? 'Cả tủ' : targetBox ? `Ô ${targetBox.boxNumber}` : null;
  const canSend = Boolean(target && reason) && !sending;

  const submit = async () => {
    if (!canSend) return;
    setSending(true); setMsg('');
    try {
      const note = detail.trim();
      const res = await api.reportLocker(jwt, activeLockerId, {
        title: `${where}: ${reason}`,
        description: note || reason,
        ...(targetBox ? { boxId: targetBox.boxId } : {}),
      });
      if (res.success && res.data) {
        showSuccess('Đã gửi báo cáo!', 'Cảm ơn bạn đã báo. Kỹ thuật viên phụ trách tủ sẽ kiểm tra sớm nhất. Bạn theo dõi phiếu trong mục Báo sự cố của app Lock.R.', {
          reportCode: res.data.id != null ? `RPT-${res.data.id}` : '',
          boxNumber: targetBox?.boxNumber,
          boxLabel: 'Ô được báo:',
        });
      } else {
        setMsg(res.data?.message || res.message || 'Không gửi được báo cáo, vui lòng thử lại.');
      }
    } catch {
      setMsg('Không kết nối được máy chủ. Vui lòng thử lại.');
    }
    setSending(false);
  };

  return (
    <div className="screen screen-boxes">
      <Header onBack={goHome} title="Báo ô tủ hư" />
      <div className="boxes-layout">
        <aside className="boxes-side">
          {userName && <div className="user-chip"><User size={15} /><span>{userName}</span></div>}

          <div className="report-step-title"><span>1</span> Chạm vào ô bị hư trên sơ đồ</div>

          <div className="report-step">
            <div className="report-step-title"><span>2</span> Vấn đề gặp phải</div>
            <div className="report-reasons">
              {REPORT_REASONS.map(r => (
                <button key={r} className={`report-reason ${reason === r ? 'active' : ''}`} onClick={() => setReason(r)}>{r}</button>
              ))}
            </div>
            {/* input (không phải textarea): bàn phím ảo của kiosk chỉ nhận <input> */}
            <input className="input report-detail" type="text" maxLength={500} value={detail}
              onChange={e => setDetail(e.target.value)} placeholder="Mô tả thêm (tuỳ chọn)" />
          </div>

          <div className={`box-picked ${where ? 'on' : ''}`}>
            {where
              ? <><AlertTriangle size={18} /><span>Báo <strong>{where}</strong>{reason ? <> · {reason}</> : ' — chọn vấn đề'}</span></>
              : <span>Chạm vào ô bị hư bên cạnh, hoặc chọn <strong>Sự cố cả tủ</strong></span>}
          </div>
          {msg && <Msg type="error" text={msg} />}
          <Btn onClick={submit} loading={sending} disabled={!canSend}><Send size={18} /> Gửi báo cáo</Btn>
        </aside>

        <section className="boxes-main">
          <div className="report-locker">
            <span><MapPin size={15} color="var(--accent)" /> {lockerInfo ? `${lockerInfo.storeName || lockerInfo.name} · ${lockerInfo.code}` : 'Sơ đồ tủ'}</span>
            <button className={`report-whole ${target === WHOLE_LOCKER ? 'active' : ''}`} onClick={() => setTarget(WHOLE_LOCKER)}>
              <Lock size={15} /> Sự cố cả tủ
            </button>
          </div>
          {loading && <p className="boxes-loading"><Loader2 size={20} style={{ animation: 'spin 0.6s linear infinite' }} />Đang tải...</p>}
          <Cabinet layout={layout}>
            {ordered.map(box => {
              const isDrone = box.cellType === 'DRONE';
              const status = String(box.status || '').toUpperCase();
              const already = ['FAULT', 'PENDING_CHECK'].includes(status);
              const sel = target === box.boxId;
              return (
                <button key={box.boxId} style={layout?.place.get(box.boxId)}
                  className={`box-item report-box ${sel ? 'selected' : ''} ${already ? 'reported' : ''}`}
                  onClick={() => setTarget(box.boxId)}>
                  <DoorFace box={box} selected={sel}
                    statusText={sel ? 'Đã chọn' : already ? 'Đã có báo lỗi' : isDrone ? 'Ô drone' : 'Chạm để chọn'} />
                </button>
              );
            })}
          </Cabinet>
        </section>
      </div>
    </div>
  );
}

// ============================================
// STAFF
// ============================================
function StaffScreen({ goHome, showSuccess, lockerInfo, activeLockerId }) {
  const [code, setCode] = useState('');
  const [loading, setLoading] = useState(false);
  const [msg, setMsg] = useState('');
  // Mã chưa được kiểm tra (hệ thống bận / mất mạng), không phải mã sai: giữ mã để khách bấm 'Thử lại'.
  const [retryable, setRetryable] = useState(false);
  const [afterOpen, setAfterOpen] = useState(null);

  // The backend answers with boxId; the kiosk knows the layout, so show the
  // human-friendly box number when it can.
  const boxNumberOf = (boxId) => {
    const box = lockerInfo?.boxes?.find(b => b.id === boxId);
    return box ? box.boxNumber : boxId;
  };

  // Chặn gửi trùng: tự gửi khi đủ 6 số + Enter của máy quét / bấm nút có thể tới cùng lúc.
  const submittingRef = useRef(false);

  const submitCode = async (typed) => {
    const accessCode = (typed ?? code).trim();
    if (accessCode.length < 6) {
      setMsg('Vui lòng nhập mã hợp lệ');
      return;
    }
    if (submittingRef.current) return;
    submittingRef.current = true;

    setLoading(true); setMsg(''); setRetryable(false);
    try {
      console.log('%c[CODE] Unlocking with access code:', 'color:#fbbf24', accessCode);

      const res = await api.unlockWithCode(activeLockerId, accessCode);

      if (res.success && res.data?.accepted && needsFollowUp(res.data?.nextStep)) {
        setAfterOpen({
          nextStep: res.data.nextStep,
          code: accessCode,
          boxLabel: res.data?.boxId ? boxNumberOf(res.data.boxId) : '',
        });
      } else if (res.success && res.data?.accepted) {
        setTimeout(() => showSuccess(
          'Đã mở khóa!',
          'Hộp đã được mở. Vui lòng đóng cửa khi xong.',
          {
            orderCode: res.data?.orderId ? `Đơn #${res.data.orderId}` : '',
            boxNumber: res.data?.boxId ? boxNumberOf(res.data.boxId) : 'Thành công'
          }
        ), 100);
      } else if (res.data?.reasonCode === 'ORDER_LOOKUP_UNAVAILABLE') {
        setRetryable(true);
        setMsg(res.data.message || 'Hệ thống đang bận, chưa kiểm tra được mã. Vui lòng thử lại.');
      } else {
        setMsg(res.data?.message || res.message || 'Mã không hợp lệ hoặc đã hết hạn');
        setTimeout(() => setCode(''), 2000);
      }
    } catch (err) {
      console.error('[CODE] Error:', err);
      setRetryable(true);
      setMsg('Không kết nối được máy chủ, chưa kiểm tra được mã. Vui lòng thử lại.');
    }
    submittingRef.current = false;
    setLoading(false);
  };

  if (afterOpen) {
    return (
      <AfterOpenPanel
        nextStep={afterOpen.nextStep}
        accessCode={afterOpen.code}
        lockerId={activeLockerId}
        boxLabel={afterOpen.boxLabel}
        showSuccess={showSuccess}
        goHome={goHome} />
    );
  }

  return (
    <div className="screen">
      <Header onBack={goHome} title="Mở Tủ (OTP / QR)" />
      <p className="subtitle" style={{ textAlign: 'center' }}>
        Nhập mã OTP từ App Mobile, mã PIN hoặc mã QR để mở tủ
      </p>
      {lockerInfo && (
        <div style={{ textAlign: 'center', marginTop: -12, marginBottom: 16, color: 'var(--text-secondary)', fontSize: 13 }}>
          Tủ hiện tại: <strong style={{ color: 'var(--accent)' }}>{lockerInfo.name || lockerInfo.storeName}</strong> ({lockerInfo.code})
        </div>
      )}

      <div className="form-group" style={{ marginTop: 24, marginBottom: 32, maxWidth: 450, marginLeft: 'auto', marginRight: 'auto', width: '100%' }}>
        <input
          className="input"
          value={code}
          onChange={e => {
            const value = e.target.value;
            setCode(value);
            setMsg('');
            setRetryable(false);
            // Gõ đủ mã OTP/PIN 6 số là tự mở, không cần bấm "Mở khóa". Mã QR 'LLQR…' của máy quét
            // không phải số nên vẫn đợi Enter như cũ.
            if (/^\d{6}$/.test(value.trim())) submitCode(value);
          }}
          // Khách chỉ tự gõ PIN 6 số (cả mã uỷ quyền); mã QR 'LLQR…' do máy quét gõ như bàn phím thật,
          // nên bàn phím ảo hiện dạng số cho dễ bấm trên màn 7".
          inputMode="numeric"
          autoComplete="off"
          placeholder="VD: 123456 hoặc LLQR..."
          style={{
            textAlign: 'center',
            fontSize: 20,
            letterSpacing: 2,
            fontWeight: 700,
            padding: '20px'
          }}
          onKeyDown={e => e.key === 'Enter' && submitCode()}
          autoFocus
        />
      </div>

      <div style={{ maxWidth: 450, margin: '0 auto', width: '100%' }}>
        <Btn onClick={() => submitCode()} loading={loading} disabled={code.length < 6}>
          {retryable
            ? <><RefreshCw size={18} /> Thử lại</>
            : <><Unlock size={18} /> Mở khóa</>}
        </Btn>
        {msg && <Msg type={retryable ? 'info' : 'error'} text={msg} />}
      </div>
    </div>
  );
}

// ============================================
// SUCCESS SCREEN (Redesigned)
// ============================================
function SuccessScreen({ goHome, title, msg, extra, countdown }) {
  const hasCard = Boolean(extra && (extra.orderCode || extra.reportCode || extra.boxNumber || extra.orderPin));
  return (
    // Có thẻ thông tin ⇒ 2 cột (màn 1024×600 không đủ cao để xếp dọc hoạt ảnh + tiêu đề + thẻ mã + nút).
    <div className={`screen success-screen ${hasCard ? 'has-card' : ''}`}>
      <div className="success-main">
        <div className="lottie-success-wrap">
          <LottieAnimation animationData={deliveryAnimation} className="lottie-player-inner" />
        </div>
        <h2 className="success-title">{title}</h2>
        <p className="success-msg">{msg}</p>
        <div className="success-actions">
          <Btn onClick={goHome} variant="primary"><Home size={20} /> Về trang chủ</Btn>
          <p className="countdown">Tự động đóng sau <strong>{countdown}</strong> giây</p>
        </div>
      </div>

      {hasCard && (
        <div className="extra-card success-card">
          {extra.orderCode && (
            <div className="success-row"><span>Mã đơn hàng</span><strong>{extra.orderCode}</strong></div>
          )}
          {extra.reportCode && (
            <div className="success-row"><span>Mã phiếu sự cố</span><strong>{extra.reportCode}</strong></div>
          )}
          {extra.boxNumber && (
            <div className="success-row"><span>{(extra.boxLabel || 'Ô tủ đã mở').replace(/:$/, '')}</span><strong className="box">Ô #{extra.boxNumber}</strong></div>
          )}
          {extra.orderPin && extra.pinContact && (
            <PinNotice pin={extra.orderPin} contact={extra.pinContact} compact />
          )}
          {extra.orderPin && !extra.pinContact && (
            <div className="success-pin">
              <span>Mã PIN để mở lại tủ khi nhận đồ</span>
              <div className="pin-notice-code">{extra.orderPin}</div>
            </div>
          )}
        </div>
      )}
    </div>
  );
}
