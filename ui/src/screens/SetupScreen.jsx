import { QRCodeSVG } from 'qrcode.react';
import { Cpu, ScanLine, Wifi } from 'lucide-react';

/// Mã QR KTV quét trong app (Lắp đặt tủ › Gán bộ điều khiển) — backend bỏ tiền tố rồi chuẩn hoá MAC.
export const PAIRING_PREFIX = 'LOCKR-PI:';

/**
 * Màn chờ gán khi Pi chưa phục vụ tủ nào (`/system/state` → isConfigured = false).
 * KTV quét QR để gán Pi vào tủ đang lắp đặt (F3-G12); gán xong Pi nhận lệnh setup và kiosk tự chuyển
 * sang màn khách.
 */
export default function SetupScreen({ info }) {
  const mac = info?.macAddress || '';
  return (
    <div className="kiosk-wrap">
      <div className="setup-card">
        <div className="setup-head">
          <Cpu size={28} />
          <div>
            <h1>Bộ điều khiển chưa gán vào tủ</h1>
            <p>Tủ chưa nhận đơn. Kỹ thuật viên quét mã dưới đây để gán bộ điều khiển này vào tủ đang lắp đặt.</p>
          </div>
        </div>

        <div className="setup-body">
          <div className="setup-qr">
            {mac ? (
              <QRCodeSVG value={`${PAIRING_PREFIX}${mac}`} size={240} level="M" marginSize={2} />
            ) : (
              <div className="setup-qr-empty">Đang đọc địa chỉ MAC…</div>
            )}
            <div className="setup-mac">{mac || '—'}</div>
          </div>

          <ol className="setup-steps">
            <li>
              <ScanLine size={18} />
              <span>Mở app Lock.R (KTV) › <b>Lắp đặt tủ</b> › chọn hồ sơ › <b>Gán bộ điều khiển</b> › quét mã này.</span>
            </li>
            <li>
              <Wifi size={18} />
              <span>Pi phải đang có mạng. Quét không được thì chọn đúng MAC ở trên trong danh sách bộ điều khiển online.</span>
            </li>
            <li>
              <Cpu size={18} />
              <span>Gán xong, tủ tự cài sơ đồ ô và mở thử từng ô; màn hình này tự chuyển sang màn khách.</span>
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
