import { QRCodeSVG } from 'qrcode.react';
import { Cpu, ScanLine, Wifi } from 'lucide-react';
import { useT } from '../i18n/LangContext.jsx';

/// Mã QR KTV quét trong app (Lắp đặt tủ › Gán bộ điều khiển) — backend bỏ tiền tố rồi chuẩn hoá MAC.
export const PAIRING_PREFIX = 'LOCKR-PI:';

/**
 * Màn chờ gán khi Pi chưa phục vụ tủ nào (`/system/state` → isConfigured = false).
 * KTV quét QR để gán Pi vào tủ đang lắp đặt (F3-G12); gán xong Pi nhận lệnh setup và kiosk tự chuyển
 * sang màn khách.
 */
export default function SetupScreen({ info }) {
  const mac = info?.macAddress || '';
  const { t } = useT();
  return (
    <div className="kiosk-wrap">
      <div className="setup-card">
        <div className="setup-head">
          <Cpu size={28} />
          <div>
            <h1>{t('setup.title')}</h1>
            <p>{t('setup.desc')}</p>
          </div>
        </div>

        <div className="setup-body">
          <div className="setup-qr">
            {mac ? (
              <QRCodeSVG value={`${PAIRING_PREFIX}${mac}`} size={240} level="M" marginSize={2} />
            ) : (
              <div className="setup-qr-empty">{t('setup.readingMac')}</div>
            )}
            <div className="setup-mac">{mac || '—'}</div>
          </div>

          <ol className="setup-steps">
            <li>
              <ScanLine size={18} />
              <span>{t('setup.step1', { install: <b>{t('setup.step1Install')}</b>, bind: <b>{t('setup.step1Bind')}</b> })}</span>
            </li>
            <li>
              <Wifi size={18} />
              <span>{t('setup.step2')}</span>
            </li>
            <li>
              <Cpu size={18} />
              <span>{t('setup.step3')}</span>
            </li>
          </ol>
        </div>

        <div className="setup-foot">
          {info?.ipAddress && <span>IP {info.ipAddress}</span>}
          {info?.version && <span>Firmware {info.version}</span>}
        </div>
      </div>
    </div>
  );
}
