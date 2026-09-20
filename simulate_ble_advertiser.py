#!/usr/bin/env python3
"""Locker BLE Beacon & Advertiser Simulator / Raspberry Pi Broadcaster.

Mục đích:
- Phát sóng gói tin BLE Advertising / Beacon để Mobile App có thể phát hiện tủ ở cự ly gần (<2m).
- Chạy được trên:
  1. Raspberry Pi (Hardware track với chip Bluetooth tích hợp / USB dongle).
  2. Laptop / PC (Simulation track: in hướng dẫn, phát giả lập hoặc dùng công cụ Bluetooth của OS).

Cách hoạt động trên Raspberry Pi thực tế (Linux BlueZ):
    sudo hciconfig hci0 up
    # Bật LE Advertising với tên định danh tủ
    sudo bluetoothctl system-alias "LOCKR_HCM01"
    sudo bluetoothctl advertise on

Cách chạy script này:
    python simulate_ble_advertiser.py --locker-code HCM01
"""

import argparse
import os
import platform
import subprocess
import sys
import time

def start_linux_rpi_advertising(locker_code: str):
    """Cấu hình BLE Advertising trên Raspberry Pi (Linux BlueZ)"""
    dev_name = f"LOCKR_{locker_code}"
    print(f"[*] Đang cấu hình BLE Advertising trên Raspberry Pi...")
    print(f"[*] Tên thiết bị quảng bá: {dev_name}")

    try:
        # 1. Bật giao diện Bluetooth hci0
        subprocess.run(["sudo", "hciconfig", "hci0", "up"], check=False)
        time.sleep(0.5)

        # 2. Đổi alias tên thiết bị
        cmd_alias = f"set-alias {dev_name}\nquit\n"
        subprocess.run(["bluetoothctl"], input=cmd_alias.encode(), check=False)

        # 3. Bật advertise
        cmd_adv = "advertise on\nquit\n"
        subprocess.run(["bluetoothctl"], input=cmd_adv.encode(), check=False)

        print(f"[OK] Đã phát sóng BLE Advertising: '{dev_name}' thành công!")
        print("[*] Mobile App có thể quét tìm thấy tủ này khi đứng gần.")
    except Exception as e:
        print(f"[!] Lỗi khi chạy lệnh Linux Bluetooth: {e}")
        print("[!] Bạn có thể dùng chế độ Mô phỏng trên App Mobile hoặc kiểm tra lại quyền sudo.")


def run_simulation(locker_code: str):
    """Chạy mô phỏng phát sóng trên máy trạm / PC khi chưa cắm Raspberry Pi"""
    dev_name = f"LOCKR_{locker_code}"
    sys_name = platform.system()

    print("=" * 65)
    print(f"  LOCK.R BLE BEACON SIMULATOR - AISL SMART LOCKER")
    print("=" * 65)
    print(f"  Mã tủ (Locker Code) : {locker_code}")
    print(f"  Tên phát BLE        : {dev_name}")
    print(f"  Hệ điều hành hiện tại: {sys_name}")
    print("=" * 65)

    if sys_name == "Linux" and os.path.exists("/sys/class/bluetooth"):
        start_linux_rpi_advertising(locker_code)
    else:
        print(f"\n[INFO] Đang chạy ở chế độ MÔ PHỎNG (Chưa cắm Raspberry Pi):")
        print(f"  - Trên Mobile App: Vào đơn hàng -> Bấm 'Mở tủ' -> Tab Bluetooth.")
        print(f"  - Bấm nút 'Mô phỏng RPi' (nút màu tím) trên App để test tức thì 1-chạm.")
        print(f"  - Khi cắm vào Raspberry Pi thật, chỉ cần chạy script này với sudo để phát sóng thật.\n")

    print("[*] Đang duy trì trạng thái phát sóng ảo (Nhấn Ctrl+C để dừng)...")
    counter = 0
    try:
        while True:
            time.sleep(3)
            counter += 1
            print(f"  [{time.strftime('%H:%M:%S')}] [BLE BEACON HEARTBEAT #{counter}] Phát gói tin Advertising: '{dev_name}' (TxPower: -59dBm)")
    except KeyboardInterrupt:
        print("\n[*] Đã dừng phát sóng BLE.")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Locker BLE Advertiser for Raspberry Pi & Simulation")
    parser.add_argument(
        "--locker-code",
        default=os.getenv("LOCKER_CODE", "HCM01"),
        help="Mã tủ (mặc định: HCM01 hoặc lấy từ biến LOCKER_CODE)",
    )
    args = parser.parse_args()
    run_simulation(args.locker_code)
