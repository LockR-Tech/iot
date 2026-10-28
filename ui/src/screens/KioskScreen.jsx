import { useState, useEffect, useCallback, useRef } from 'react';
import * as api from '../api';
import { setupRecaptcha, sendPhoneOtp } from '../firebase';
import {
  Lock, Package, Smartphone, Mail, ArrowLeft, MapPin,
  CheckCircle, KeyRound, Hash, User, CreditCard,
  Unlock, Home, Loader2, Circle, ClipboardList,
  Wifi, ChevronRight, ShieldCheck, X,
  Clock, LogOut, Users, AlertTriangle, Plane, Luggage, QrCode, RefreshCw, FlaskConical
} from 'lucide-react';
import { QRCodeSVG } from 'qrcode.react';
import boxStack3d from '../assets/illustrations/box_stack_3d.png';
import airDelivery3d from '../assets/illustrations/air_delivery_3d.png';
import mobilePay3d from '../assets/illustrations/mobile_pay_3d.png';
import deliveryAnimation from '../assets/illustrations/delivery_animation.json';
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
    setScreen('home'); setHistory(['home']);
    if (cdRef.current) clearInterval(cdRef.current);
  }, []);

  const showSuccess = useCallback((title, msg, extra = null) => {
    setSuccessTitle(title);
    setSuccessMsg(msg);
    setSuccessExtra(extra);
    go('success');
    let sec = AUTO_HOME_SEC;
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
      {screen === 'home' && <HomeScreen go={go} lockerInfo={lockerInfo} activeLockerId={activeLockerId} setActiveLockerId={setActiveLockerId} allLockers={allLockers} />}
      {screen === 'login' && <LoginScreen go={go} goHome={goHome} email={email} setEmail={setEmail} phone={phone} setPhone={setPhone} loginMethod={loginMethod} setLoginMethod={setLoginMethod} activeLockerId={activeLockerId} setJwt={setJwt} setUserName={setUserName} />}
      {screen === 'otp' && <OtpScreen go={go} back={back} email={email} setJwt={setJwt} setTempToken={setTempToken} setUserName={setUserName} />}
      {screen === 'phone-otp' && <PhoneOtpScreen go={go} back={back} phone={phone} setJwt={setJwt} setTempToken={setTempToken} setUserName={setUserName} />}
      {screen === 'register' && <RegisterScreen go={go} goHome={goHome} tempToken={tempToken} setJwt={setJwt} setUserName={setUserName} />}
      {screen === 'boxes' && <BoxSelectionScreen go={go} goHome={goHome} jwt={jwt} userName={userName} selectedBox={selectedBox} setSelectedBox={setSelectedBox} lockerInfo={lockerInfo} activeLockerId={activeLockerId} />}
      {screen === 'order-info' && <OrderInfoScreen go={go} back={back} jwt={jwt} selectedBox={selectedBox} setOrderId={setOrderId} setOrderPin={setOrderPin} setOrderCode={setOrderCode} setTotalPrice={setTotalPrice} activeLockerId={activeLockerId} />}
      {screen === 'payment' && <PaymentScreen go={go} goHome={goHome} jwt={jwt} orderId={orderId} orderPin={orderPin} orderCode={orderCode} totalPrice={totalPrice} selectedBox={selectedBox} showSuccess={showSuccess} activeLockerId={activeLockerId} />}
      {screen === 'staff' && <StaffScreen goHome={goHome} showSuccess={showSuccess} lockerInfo={lockerInfo} activeLockerId={activeLockerId} />}
      {screen === 'success' && <SuccessScreen goHome={goHome} title={successTitle} msg={successMsg} extra={successExtra} countdown={countdown} />}
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
function HomeScreen({ go, lockerInfo, activeLockerId, setActiveLockerId, allLockers }) {
  return (
    <div className="screen screen-home-split">
      <div className="home-left">
        <div className="kiosk-hero-3d">
          <div className="hero-brand-badge">
            <span className="dot green"></span> Hệ thống thông minh 24/7
          </div>
          <div className="hero-3d-visual">
            <img src={airDelivery3d} alt="Smart Delivery" className="hero-3d-img" />
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
              <img src={airDelivery3d} alt="Mở tủ" />
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
          <div className="card-3d-action secondary" onClick={() => go('login')}>
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
// LOGIN (Phone / Email OTP / Quét QR bằng app)
// ============================================
function LoginScreen({ go, goHome, email, setEmail, phone, setPhone, loginMethod, setLoginMethod, activeLockerId, setJwt, setUserName }) {
  const [loading, setLoading] = useState(false);
  const [msg, setMsg] = useState('');
  const recaptchaReady = useRef(false);

  useEffect(() => {
    if (loginMethod === 'phone' && !recaptchaReady.current) {
      try {
        setupRecaptcha('phone-send-btn');
        recaptchaReady.current = true;
      } catch { /* button may not be in DOM yet */ }
    }
  }, [loginMethod]);

  useEffect(() => {
    if (loginMethod !== 'phone') recaptchaReady.current = false;
  }, [loginMethod]);

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
      if (!recaptchaReady.current) {
        setupRecaptcha('phone-send-btn');
        recaptchaReady.current = true;
      }
      let formatted = phone.trim();
      if (formatted.startsWith('0')) formatted = '+84' + formatted.slice(1);
      else if (!formatted.startsWith('+')) formatted = '+84' + formatted;

      const confirmation = await sendPhoneOtp(formatted);
      window.confirmationResult = confirmation;
      go('phone-otp');
    } catch (err) {
      console.error('Firebase phone OTP error:', err);
      setMsg(err.message || 'Lỗi gửi OTP. Vui lòng thử lại.');
      recaptchaReady.current = false;
    }
    setLoading(false);
  };

  // Giống đăng nhập OTP: giữ access token + tên khách rồi sang bước chọn ô.
  const handleQrLoggedIn = (data) => {
    setJwt(data.accessToken);
    setUserName(data.user?.fullName || data.user?.phoneNumber || 'Khách');
    go('boxes');
  };

  return (
    <div className="screen">
      <Header onBack={goHome} title="Nhập thông tin" />
      <div className="login-tabs">
        <div className={`login-tab ${loginMethod === 'phone' ? 'active' : ''}`} onClick={() => { setLoginMethod('phone'); setMsg(''); }}>
          <Smartphone size={16} /> Số điện thoại
        </div>
        <div className={`login-tab ${loginMethod === 'email' ? 'active' : ''}`} onClick={() => { setLoginMethod('email'); setMsg(''); }}>
          <Mail size={16} /> Email
        </div>
        <div className={`login-tab ${loginMethod === 'qr' ? 'active' : ''}`} onClick={() => { setLoginMethod('qr'); setMsg(''); }}>
          <QrCode size={16} /> Quét QR bằng app
        </div>
      </div>

      {loginMethod === 'qr' ? (
        <QrLoginPanel lockerId={activeLockerId} onLoggedIn={handleQrLoggedIn} />
      ) : loginMethod === 'phone' ? (
        <>
          <p className="subtitle">Nhập số điện thoại để nhận mã OTP xác thực</p>
          <div className="form-group">
            <label>Số điện thoại</label>
            <input className="input" type="tel" value={phone} onChange={e => setPhone(e.target.value.replace(/[^0-9+]/g, ''))}
              placeholder="0901234567" inputMode="tel" autoComplete="tel"
              onKeyDown={e => e.key === 'Enter' && handleSendPhone()} />
          </div>
          <Btn id="phone-send-btn" onClick={handleSendPhone} loading={loading}><Smartphone size={18} /> Gửi mã OTP</Btn>
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
// QR LOGIN (khách quét bằng app Lock.R)
// ============================================
// Kiosk tạo phiên → hiện QR (chỉ chứa mã phiên + mã xác nhận, đều công khai) → khách quét bằng app và
// xác nhận → kiosk hỏi trạng thái bằng pollToken tới khi nhận token (server trả đúng một lần) rồi đi tiếp
// như đăng nhập OTP. pollToken chỉ nằm trong biến của effect: không vào state, không hiện, không log.
const QR_DEFAULT_TTL_MS = 120000;           // hợp đồng: phiên sống 120 s
const QR_MAX_TTL_MS = 10 * 60 * 1000;
const QR_EXPIRY_GRACE_MS = 5000;            // quá hạn mà chưa nghe server báo EXPIRED (mất mạng) thì tự dừng

function QrLoginPanel({ lockerId, onLoggedIn }) {
  const [attempt, setAttempt] = useState(0);         // "Tạo mã mới" tăng số này để tạo phiên khác
  const [phase, setPhase] = useState('creating');    // creating | pending | ended
  const [qr, setQr] = useState(null);                // { content, sessionId, code, deadline } — phần công khai
  const [msg, setMsg] = useState('');
  const [now, setNow] = useState(() => Date.now());
  const onLoggedInRef = useRef(onLoggedIn);

  useEffect(() => { onLoggedInRef.current = onLoggedIn; }, [onLoggedIn]);

  useEffect(() => {
    let alive = true;
    let timer = null;
    let session = null;     // { sessionId, pollToken } của phiên đang chờ; null = không còn gì để huỷ

    const end = (text) => {
      session = null;
      setPhase('ended');
      setMsg(text);
    };

    // Hỏi tuần tự (xong lượt này mới hẹn lượt sau) để không có hai lượt cùng lúc tranh nhau token một lần.
    const poll = async (pollMs, deadline) => {
      let res = null;
      try {
        res = await api.pollKioskQrSession(session.sessionId, session.pollToken);
      } catch { /* mạng chập chờn: hỏi lại lượt sau */ }
      if (!alive || !session) return;
      const d = res?.data;
      const status = res?.success ? d?.status : (res?.code === 'KIOSK_QR_INVALID' ? 'INVALID' : null);
      if (status === 'CONFIRMED' && d.accessToken) {
        session = null;                     // đã nhận token — rời màn không huỷ phiên nữa
        onLoggedInRef.current(d);
        return;
      }
      if (status === 'EXPIRED' || status === 'INVALID') { end('Mã QR đã hết hạn.'); return; }
      if (status === 'CANCELLED') { end('Phiên đăng nhập đã bị huỷ.'); return; }
      if (status === 'CONSUMED' || status === 'CONFIRMED') { end('Phiên này đã được sử dụng.'); return; }
      if (Date.now() > deadline + QR_EXPIRY_GRACE_MS) {
        api.cancelKioskQrSession(session.sessionId, session.pollToken).catch(() => {});
        end(res ? 'Mã QR đã hết hạn.' : 'Mất kết nối server, mã QR đã hết hạn.');
        return;
      }
      timer = setTimeout(() => poll(pollMs, deadline), pollMs);
    };

    (async () => {
      let res = null;
      try { res = await api.createKioskQrSession(lockerId); } catch { /* báo lỗi bên dưới */ }
      const d = res?.success ? res.data : null;
      if (!alive) {
        // Khách đã rời màn trong lúc chờ tạo phiên: huỷ luôn phiên vừa tạo
        if (d?.sessionId && d?.pollToken) api.cancelKioskQrSession(d.sessionId, d.pollToken).catch(() => {});
        return;
      }
      if (!d?.sessionId || !d?.pollToken || !d?.qrContent) {
        setPhase('ended');
        setMsg(!res ? 'Lỗi kết nối server'
          : res.code === 'KIOSK_QR_RATE_LIMITED' ? 'Tạo mã quá nhiều lần. Vui lòng thử lại sau ít phút.'
            : (res.message || 'Không tạo được mã QR'));
        return;
      }
      // Đếm ngược theo đồng hồ kiosk; đồng hồ lệch xa (thời hạn vô lý) thì dùng thời hạn mặc định.
      let ttl = Date.parse(d.expiresAt) - Date.now();
      if (!Number.isFinite(ttl) || ttl <= 0 || ttl > QR_MAX_TTL_MS) ttl = QR_DEFAULT_TTL_MS;
      const deadline = Date.now() + ttl;
      const pollMs = Math.max(1000, Number(d.pollIntervalMs) || 2000);
      session = { sessionId: d.sessionId, pollToken: d.pollToken };
      setQr({ content: d.qrContent, sessionId: d.sessionId, code: d.confirmCode, deadline });
      setNow(Date.now());
      setPhase('pending');
      timer = setTimeout(() => poll(pollMs, deadline), pollMs);
    })();

    return () => {
      alive = false;
      clearTimeout(timer);
      // Rời bước này (quay lại, đổi tab, về trang chủ) khi phiên còn chờ: huỷ để mã không dùng được nữa
      if (session) api.cancelKioskQrSession(session.sessionId, session.pollToken).catch(() => {});
    };
  }, [lockerId, attempt]);

  useEffect(() => {
    if (phase !== 'pending') return undefined;
    const id = setInterval(() => setNow(Date.now()), 1000);
    return () => clearInterval(id);
  }, [phase]);

  const retry = () => {
    setPhase('creating'); setMsg(''); setQr(null);
    setAttempt(a => a + 1);
  };

  const left = qr ? Math.max(0, Math.ceil((qr.deadline - now) / 1000)) : 0;
  const mmss = `${Math.floor(left / 60)}:${String(left % 60).padStart(2, '0')}`;

  return (
    <div className="qr-login">
      <div className={`qr-login-code ${phase === 'pending' && qr ? '' : 'idle'}`}>
        {phase === 'pending' && qr ? (
          <QRCodeSVG value={qr.content} size={200} level="M" marginSize={2} />
        ) : phase === 'creating' ? (
          <><Loader2 size={32} style={{ animation: 'spin 0.6s linear infinite' }} /><span>Đang tạo mã...</span></>
        ) : (
          <><QrCode size={48} /><span>Mã không còn hiệu lực</span></>
        )}
      </div>

      <div className="qr-login-info">
        <h3>Đăng nhập nhanh bằng app Lock.R</h3>
        <ol className="qr-login-steps">
          <li>Mở app Lock.R trên điện thoại và chọn <strong>quét mã QR</strong></li>
          <li>Quét mã bên cạnh rồi bấm <strong>xác nhận đăng nhập kiosk</strong></li>
          <li>Kiosk tự chuyển sang bước chọn ô tủ</li>
        </ol>

        {phase === 'pending' && qr && (
          <>
            <div className={`qr-login-timer ${left <= 20 ? 'warn' : ''}`}>
              <Clock size={16} /> Mã hết hạn sau <strong>{mmss}</strong>
            </div>
            <p className="polling-status"><Loader2 size={14} style={{ animation: 'spin 0.6s linear infinite' }} />Đang chờ xác nhận trên điện thoại...</p>
            <div className="qr-login-manual">
              Không quét được? Nhập tay trong app: mã phiên <code>{qr.sessionId}</code>
              {qr.code && <> · mã xác nhận <code>{qr.code}</code></>}
            </div>
          </>
        )}

        {phase === 'ended' && (
          <>
            <Msg type="error" text={msg} />
            <Btn onClick={retry}><RefreshCw size={18} /> Tạo mã mới</Btn>
          </>
        )}
      </div>
    </div>
  );
}

// ============================================
// PHONE OTP (Firebase verification)
// ============================================
function PhoneOtpScreen({ go, back, phone, setJwt, setTempToken, setUserName }) {
  const [otp, setOtp] = useState('');
  const [loading, setLoading] = useState(false);
  const [msg, setMsg] = useState('');

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
          go('boxes');
        }
      } else {
        setMsg(res.message || 'Lỗi đăng nhập');
      }
    } catch (err) {
      console.error('Phone OTP verify error:', err);
      if (err.code === 'auth/invalid-verification-code') {
        setMsg('Mã OTP không đúng. Vui lòng thử lại.');
      } else if (err.code === 'auth/code-expired') {
        setMsg('Mã OTP đã hết hạn. Vui lòng quay lại và gửi lại.');
      } else {
        setMsg(err.message || 'Lỗi xác thực');
      }
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
      {msg && <Msg type="error" text={msg} />}
    </div>
  );
}

// ============================================
// OTP
// ============================================
function OtpScreen({ go, back, email, setJwt, setTempToken, setUserName }) {
  const [otp, setOtp] = useState('');
  const [loading, setLoading] = useState(false);
  const [msg, setMsg] = useState('');

  const handleVerify = async () => {
    if (otp.length !== 6) { setMsg('Vui lòng nhập đủ 6 số OTP'); return; }
    setLoading(true); setMsg('');
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
          go('boxes');
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
      {msg && <Msg type="error" text={msg} />}
    </div>
  );
}

// ============================================
// REGISTER (Kiosk Quick Register — API 1.4)
// ============================================
function RegisterScreen({ go, goHome, tempToken, setJwt, setUserName }) {
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
          go('boxes');
        } else {
          setMsg(res.message || 'Lỗi đăng ký nhanh');
        }
      } catch {
        setMsg('Lỗi kết nối server');
      }
      setLoading(false);
    })();
  }, [tempToken, setJwt, setUserName, go]);

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
  }
  return { cols, place };
}

function BoxSelectionScreen({ go, goHome, jwt, userName, selectedBox, setSelectedBox, lockerInfo, activeLockerId }) {
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
        alert('Đơn này không còn mã PIN hoạt động. Vui lòng thao tác lại trên mobile app.');
        return;
      }
      const unlockRes = await api.unlockBox(
        activeLockerId,
        pinCode,
        manageBox.box.boxId,
        'PICKUP',
      );
      if (unlockRes.success && unlockRes.data?.accepted) {
        alert(unlockRes.data?.message || 'Hộp đã được mở. Vui lòng đóng cửa khi lấy đồ xong.');
      } else {
        alert(unlockRes.data?.message || unlockRes.message || 'Lỗi mở hộp');
      }
    } catch (e) {
      alert('Lỗi kết nối máy chủ');
    } finally {
      setManageLoading(false);
    }
  };

  const endManageBox = async () => {
    const order = manageBox.order;
    if (String(order.type || '').toUpperCase() !== 'RENTAL') {
      alert('Chỉ đơn thuê ô mới kết thúc được tại đây.');
      return;
    }
    if (!window.confirm('Bạn đã lấy hết đồ và đóng cửa ô? Kết thúc thuê sẽ trả ô lại.')) return;
    setManageLoading(true);
    try {
      const res = await api.endRental(jwt, order.id);
      if (res.success) {
        alert('Đã kết thúc thuê. Ô đã được trả lại.');
        setManageBox(null);
        await fetchData();
      } else {
        alert(res.data?.message || res.message || 'Không kết thúc thuê được');
      }
    } catch (e) {
      alert('Lỗi kết nối máy chủ');
    } finally {
      setManageLoading(false);
    }
  };

  const notImplemented = () => {
    alert('Tính năng này trên Kiosk đang được hoàn thiện. Vui lòng sử dụng Mobile App để tiếp tục thao tác.');
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
  const availableCount = boxes.filter(b => b.status === 'AVAILABLE' && b.cellType !== 'DRONE').length;
  const layout = boxPlacement(displayBoxes);

  return (
    <div className={`screen ${layout ? 'screen-boxes' : ''}`}>
      <Header onBack={goHome} title="Chọn ô tủ" />
      {userName && <div className="user-info"><User size={16} /> {userName}</div>}
      {lockerInfo && (
        <div className="locker-info-card">
          <div className="locker-name">
            <MapPin size={16} color="var(--accent)" />
            {lockerInfo.storeName || lockerInfo.name}
          </div>
          <div className="locker-address">
            {lockerInfo.address} • Tủ: {lockerInfo.code}
          </div>
          {lockerInfo.totalBoxes != null && (
            <div className="locker-stats">
              <span className="stat-available">
                <CheckCircle size={14} style={{ marginRight: 4, verticalAlign: -2 }} />
                {availableCount} ô trống
              </span>
              <span className="stat-total">/ {lockerInfo.totalBoxes} ô</span>
            </div>
          )}
        </div>
      )}
      {!lockerInfo && (
        <p className="subtitle">Chọn 1 ô tủ trống để gửi đồ</p>
      )}

      {loading && <p style={{ textAlign: 'center', color: 'var(--text-muted)', padding: 24 }}><Loader2 size={20} style={{ animation: 'spin 0.6s linear infinite', verticalAlign: -4, marginRight: 8 }} />Đang tải...</p>}
      {msg && !loading && <Msg type="error" text={msg} />}

      <div className={`box-grid ${layout ? 'by-layout' : ''}`} style={layout ? { '--cols': layout.cols } : undefined}>
        {displayBoxes.map(box => {
          const myOrder = myOrders.find(o =>
            isActiveManageableOrder(o)
            && (o.sendBoxId === box.boxId || o.receiveBoxId === box.boxId)
          );
          
          let statusText = 'Trống';
          let boxClass = 'available';
          const isDrone = box.cellType === 'DRONE' || box.type === 'DRONE';
          const isAvail = box.status === 'AVAILABLE' && !isDrone;
          
          if (myOrder) {
            statusText = 'Ô của bạn';
            boxClass = 'yours';
          } else if (isDrone) {
            statusText = 'Drone';
            boxClass = 'drone';
          } else {
            const isRented = box.status === 'RENTED' || box.status === 'OCCUPIED' || box.status === 'STORING';
            const isBooked = box.status === 'BOOKED' || box.status === 'RESERVED';
            const isOffline = box.status === 'OFFLINE' || box.status === 'MAINTENANCE' || box.status === 'FAULT';
            
            if (isRented) { statusText = 'Đang dùng'; boxClass = 'rented'; }
            else if (isBooked) { statusText = 'Đã đặt'; boxClass = 'booked'; }
            else if (isOffline) { statusText = 'Lỗi'; boxClass = 'offline'; }
          }

          const sel = selectedBox?.boxId === box.boxId;
          const BoxIcon = isDrone ? Plane : 
                          ['LUGGAGE', 'XL'].includes(box.cellType) || box.type === 'LUGGAGE' ? Luggage : Package;

          return (
            <button key={box.boxId} 
              onClick={() => handleBoxClick(box, isAvail, myOrder)}
              disabled={!isAvail && !myOrder}
              style={layout?.place.get(box.boxId)}
              className={`box-item ${boxClass} ${sel ? 'selected' : ''}`}>
              <div className="box-icon"><BoxIcon size={28} /></div>
              <div className="box-number">Ô {box.boxNumber}</div>
              <div className="box-status">{statusText}</div>
            </button>
          );
        })}
      </div>

      <Btn onClick={() => go('order-info')} disabled={!selectedBox}>
        Tiếp tục <ChevronRight size={18} /> Đặt tủ
      </Btn>

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
               <Btn onClick={notImplemented} variant="secondary" disabled={manageLoading}><AlertTriangle size={18}/> Báo ô lỗi</Btn>
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
  const [hours, setHours] = useState(1);

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
  const [hoursOptions, setHoursOptions] = useState([1, 2, 3, 4, 6, 8, 12, 24]);
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
      const raw = s['app.order.rental-quick-hours'];
      const quick = (Array.isArray(raw) ? raw : String(raw ?? '').split(','))
        .map(Number).filter(h => Number.isInteger(h) && h > 0);
      if (quick.length) {
        setHoursOptions(quick);
        setHours(h => (quick.includes(h) ? h : quick[0]));
      }
    }).catch(() => { /* giữ mặc định */ });
    return () => { alive = false; };
  }, []);

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

  return (
    <div className="screen">
      <Header onBack={back} title="Thông tin đặt tủ" />

      {/* Số giờ thuê */}
      <div className="form-group">
        <label style={{ fontWeight: 600, marginBottom: 8, display: 'block' }}>Thời gian thuê tủ</label>
        <div style={{ display: 'flex', gap: 8, flexWrap: 'wrap' }}>
          {hoursOptions.map(h => (
            <button
              key={h}
              onClick={() => setHours(h)}
              style={{
                padding: '10px 18px',
                borderRadius: 12,
                border: `2px solid ${hours === h ? 'var(--accent)' : 'var(--border)'}`,
                backgroundColor: hours === h ? 'var(--accent)' : 'transparent',
                color: hours === h ? '#fff' : 'var(--text-primary)',
                fontWeight: hours === h ? 700 : 400,
                cursor: 'pointer',
                fontSize: 14,
                transition: 'all 0.15s',
              }}
            >
              {h}h
            </button>
          ))}
        </div>
        <div style={{ marginTop: 8, fontSize: 13, color: 'var(--text-secondary)' }}>
          Giá: {fmt(HOURLY_RATE)}/giờ{DAILY_RATE > 0 && <> · {fmt(DAILY_RATE)}/ngày</>} — Tổng {hours} giờ = <strong style={{ color: 'var(--accent)' }}>{fmt(subtotal)}</strong>
        </div>
      </div>

      {/* Ghi chú */}
      <div className="form-group">
        <label>Ghi chú (tùy chọn)</label>
        <input className="input" value={note} onChange={e => setNote(e.target.value)} placeholder="Ví dụ: Quần áo, cần giữ cẩn thận..." />
      </div>

      {/* Mã giảm giá */}
      <div className="divider" style={{ marginTop: 20 }}>Mã giảm giá/Ưu đãi</div>
      <div className="form-group">
        <div style={{ display: 'flex', gap: 8 }}>
          <input className="input" value={promoCode} onChange={e => {
            setPromoCode(e.target.value.toUpperCase());
            if (promoApplied || promoError) { setPromoApplied(false); setPromoError(''); setPromoDiscount(0); setPromoDetail(null); }
          }} placeholder="Nhập mã khuyến mãi" style={{ flex: 1, textTransform: 'uppercase' }} disabled={promoApplied || promoLoading} />
          {!promoApplied ? (
            <button className="btn btn-primary" onClick={handleApplyPromo} disabled={!promoCode.trim() || promoLoading} style={{ width: 'auto', padding: '0 20px', borderRadius: 12 }}>
              {promoLoading ? <Loader2 size={18} className="spinner" /> : 'Áp dụng'}
            </button>
          ) : (
            <button className="btn btn-secondary" onClick={() => { setPromoCode(''); setPromoApplied(false); setPromoDiscount(0); setPromoDetail(null); }} style={{ width: 'auto', padding: '0 20px', borderRadius: 12, backgroundColor: '#f1f5f9', color: '#64748b' }}>
              <X size={18} /> Gỡ bỏ
            </button>
          )}
        </div>
        {promoError && <div style={{ color: '#ef4444', fontSize: 13, marginTop: 8, display: 'flex', alignItems: 'center', gap: 4 }}><Circle size={12} fill="#ef4444" color="#ef4444" /> {promoError}</div>}
        {promoApplied && promoDetail && (
          <div style={{ marginTop: 12, padding: 12, backgroundColor: '#f0fdf4', border: '1px solid #bbf7d0', borderRadius: 12 }}>
            <div style={{ color: '#166534', fontWeight: 600, fontSize: 14 }}>{promoDetail.title || promoDetail.name}</div>
            {promoBelowMin
              ? <div style={{ color: '#b45309', fontSize: 13, marginTop: 4 }}>Cần đơn tối thiểu {fmt(promoMinOrder)} để dùng mã này — tăng số giờ thuê hoặc gỡ mã.</div>
              : <div style={{ color: '#22c55e', fontSize: 13, marginTop: 4 }}>Đã giảm: {fmt(promoDiscount)}</div>}
          </div>
        )}
      </div>

      {/* Tổng tiền */}
      <div className="order-sum" style={{ marginTop: 24 }}>
        <div className="order-row"><span style={{ color: 'var(--text-secondary)' }}>Thuê tủ {hours} giờ:</span> <strong>{fmt(subtotal)}</strong></div>
        {promoApplied && promoDiscount > 0 && (
          <div className="order-row"><span style={{ color: '#22c55e' }}>Khuyến mãi:</span> <strong style={{ color: '#ef4444' }}>-{fmt(promoDiscount)}</strong></div>
        )}
        <div className="order-row" style={{ marginTop: 12, paddingTop: 12, borderTop: '1px solid var(--border)' }}>
          <span style={{ fontSize: 16, fontWeight: 700, color: 'var(--text-primary)' }}>Tổng cộng:</span>
          <strong style={{ fontSize: 20, color: 'var(--accent)' }}>{fmt(total)}</strong>
        </div>
      </div>

      <Btn onClick={handleCreate} loading={loading} style={{ marginTop: 24 }}><ClipboardList size={18} /> Đặt tủ ngay</Btn>
      {msg && <Msg type="error" text={msg} />}
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

function PaymentScreen({ go, goHome, jwt, orderId, orderPin, orderCode, totalPrice, selectedBox, showSuccess, activeLockerId }) {
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
        dropDoneMessage={`Đã bắt đầu thuê. Dùng mã PIN ${orderPin} để mở lại ô trong thời gian thuê.`} />
    );
  }

  if (paidConfirmed) {
    return (
      <div className="screen">
        <Header onBack={goHome} title="Thanh toán" />
        <div style={{ maxWidth: 450, margin: '0 auto', width: '100%', display: 'flex', flexDirection: 'column', gap: 12 }}>
          <Msg type="success" text={`Đã nhận thanh toán cho đơn ${orderCode || `#${orderId}`}.`} />
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
function AfterOpenPanel({ nextStep, accessCode, lockerId, boxLabel, showSuccess, goHome, dropDoneMessage }) {
  const [loading, setLoading] = useState('');
  const [msg, setMsg] = useState('');

  const run = async (key, action, title, message) => {
    setLoading(key); setMsg('');
    try {
      const res = await action(lockerId, accessCode);
      if (res.success && res.data?.accepted) {
        showSuccess(title, message || res.data.message, { boxNumber: boxLabel });
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
          <Btn onClick={() => run('drop', api.confirmDropWithCode, 'Đã bỏ hàng!', dropDoneMessage)} loading={loading === 'drop'}>
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

  return (
    <div className="screen">
      <Header onBack={goHome} title={`Ô Tủ #${boxLabel}`} />
      <p className="subtitle" style={{ textAlign: 'center' }}>
        Ô đã mở. Bạn vẫn đang thuê ô này — lấy hoặc cất đồ rồi đóng cửa.
      </p>
      <div style={panel}>
        <Btn
          onClick={() => showSuccess('Đã mở khóa!', 'Nhớ đóng cửa ô. Mã PIN vẫn dùng được tới khi hết hạn thuê.', { boxNumber: boxLabel })}
          disabled={!!loading}>
          <Unlock size={18} /> Đóng cửa, tiếp tục thuê
        </Btn>
        <Btn variant="secondary" onClick={() => run('end', api.endRentalWithCode, 'Đã kết thúc thuê!')} loading={loading === 'end'}>
          <LogOut size={18} /> Lấy hết đồ &amp; kết thúc thuê
        </Btn>
        {msg && <Msg type="error" text={msg} />}
      </div>
    </div>
  );
}

const needsFollowUp = (nextStep) => nextStep === 'CONFIRM_DROP' || nextStep === 'RENTAL_ACCESS';

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

  const submitCode = async () => {
    const accessCode = code.trim();
    if (accessCode.length < 6) {
      setMsg('Vui lòng nhập mã hợp lệ');
      return;
    }

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
            setCode(e.target.value);
            setMsg('');
            setRetryable(false);
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
        <Btn onClick={submitCode} loading={loading} disabled={code.length < 6}>
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
  return (
    <div className="screen success-screen" style={{ justifyContent: 'center', alignItems: 'center', textAlign: 'center', background: 'radial-gradient(circle at 50% 30%, rgba(52, 211, 153, 0.12) 0%, var(--bg) 70%)' }}>
      <div className="lottie-success-wrap">
        <LottieAnimation animationData={deliveryAnimation} className="lottie-player-inner" />
      </div>
      
      <h2 style={{ fontSize: 28, fontWeight: 800, color: 'var(--text-primary)', marginBottom: 8 }}>{title}</h2>
      <p style={{ fontSize: 15, color: 'var(--text-secondary)', maxWidth: 420, lineHeight: 1.5 }}>{msg}</p>

      {extra && (
        <div className="extra-card" style={{ 
          marginTop: 24, width: '100%', maxWidth: 440, padding: '20px 24px',
          background: '#fff', borderRadius: 20, border: '1px solid var(--border)',
          boxShadow: '0 16px 40px rgba(0,0,0,0.06)', gap: 14
        }}>
          {extra.orderCode && (
            <div className="extra-row" style={{ justifyContent: 'space-between', borderBottom: '1px dashed var(--border)', paddingBottom: 10 }}>
              <span style={{ color: 'var(--text-muted)' }}>Mã đơn hàng:</span>
              <strong style={{ fontSize: 15 }}>{extra.orderCode}</strong>
            </div>
          )}
          {extra.boxNumber && (
            <div className="extra-row" style={{ justifyContent: 'space-between', borderBottom: '1px dashed var(--border)', paddingBottom: 10 }}>
              <span style={{ color: 'var(--text-muted)' }}>Ô tủ đã mở:</span>
              <strong style={{ fontSize: 24, color: 'var(--success)', fontWeight: 900 }}>Ô #{extra.boxNumber}</strong>
            </div>
          )}
          {extra.orderPin && (
            <div className="extra-row" style={{ justifyContent: 'space-between', flexDirection: 'column', gap: 6, paddingTop: 6 }}>
              <span style={{ color: 'var(--text-muted)', fontSize: 13 }}>Mã PIN để mở lại tủ khi nhận đồ:</span>
              <div style={{ 
                background: 'var(--accent-dim)', padding: '10px 20px', 
                borderRadius: 12, border: '2px dashed rgba(59, 130, 246, 0.4)',
                fontSize: 26, fontWeight: 800, color: 'var(--accent)', letterSpacing: 4
              }}>
                {extra.orderPin}
              </div>
            </div>
          )}
        </div>
      )}

      <div style={{ marginTop: 28, display: 'flex', flexDirection: 'column', alignItems: 'center', gap: 12, width: '100%', maxWidth: 320 }}>
        <Btn onClick={goHome} variant="primary" style={{ height: 52, fontSize: 17 }}><Home size={20} /> Về trang chủ</Btn>
        <p className="countdown" style={{ fontSize: 13 }}>Tự động đóng sau <strong>{countdown}</strong> giây</p>
      </div>
    </div>
  );
}
