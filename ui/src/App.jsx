import { useState, useEffect } from 'react';
import { Monitor } from 'lucide-react';
import KioskScreen from './screens/KioskScreen';
import SetupScreen from './screens/SetupScreen';
import VirtualKeyboard from './components/VirtualKeyboard';
import * as api from './api';

const SETUP_POLL_MS = 5000;

/**
 * Pi chưa phục vụ tủ nào ⇒ hiện mã QR gán bộ điều khiển (F3-G12). `null` = chưa biết (không đọc được API
 * cục bộ, vd chạy kiosk trên máy dev) ⇒ giữ màn khách như trước.
 */
function usePairingState() {
  const [configured, setConfigured] = useState(null);
  const [info, setInfo] = useState(null);

  useEffect(() => {
    let ignore = false;
    const poll = async () => {
      try {
        const state = await api.getSystemState();
        if (ignore || typeof state?.isConfigured !== 'boolean') return;
        setConfigured(state.isConfigured);
        if (!state.isConfigured) {
          const system = await api.getSystemInfo();
          if (!ignore) setInfo(system);
        }
      } catch { /* API cục bộ chưa sẵn sàng — thử lại lần sau */ }
    };
    poll();
    const timer = setInterval(poll, SETUP_POLL_MS);
    return () => {
      ignore = true;
      clearInterval(timer);
    };
  }, []);

  return { configured, info };
}

export default function App() {
  const [time, setTime] = useState(new Date());
  const { configured, info } = usePairingState();

  useEffect(() => {
    const timer = setInterval(() => setTime(new Date()), 1000);
    return () => clearInterval(timer);
  }, []);

  const timeStr = time.toLocaleTimeString('vi-VN', { hour: '2-digit', minute: '2-digit' });
  const dateStr = time.toLocaleDateString('vi-VN', { day: '2-digit', month: '2-digit', year: 'numeric' });

  return (
    <div className="app-shell">
      {/* ─── Header ─── */}
      <div className="top-bar">
        <div className="top-bar-brand">
          <Monitor size={18} /> Lock.R IoT
        </div>
        <div className="top-bar-status">
          <div className="status-pill online">
            <div className="dot green"></div> Online
          </div>
          <div className="top-bar-clock">
            {timeStr} • {dateStr}
          </div>
        </div>
      </div>

      <div className="layout">
        {/* ─── Main View ─── */}
        <div className="main-content">
          {configured === false ? <SetupScreen info={info} /> : <KioskScreen />}
        </div>
      </div>

      <VirtualKeyboard />
    </div>
  );
}
