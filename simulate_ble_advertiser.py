#!/usr/bin/env python3
"""Locker BLE Beacon & Advertiser Simulator / Raspberry Pi Broadcaster.

Mục đích:
- Phát sóng gói tin BLE Advertising / Beacon để Mobile App có thể phát hiện tủ ở cự ly gần (<2m).
- Chạy được trên:
  1. Raspberry Pi (Hardware track với chip Bluetooth tích hợp / USB dongle).
  2. Laptop / PC (Simulation track: in hướng dẫn, phát giả lập hoặc dùng công cụ Bluetooth của OS).

Cách chạy:
    python simulate_ble_advertiser.py --locker-code CAB-TU01
"""

import argparse
import os
import platform
import sys
import time
from services.ble_service import BleAdvertiserService


def run_advertiser(locker_code: str):
    dev_name = f"LOCKR_{locker_code}"
    sys_name = platform.system()

    print("=" * 65)
    print("  LOCK.R BLE BEACON BROADCASTER & SIMULATOR")
    print("=" * 65)
    print(f"  Mã tủ (Locker Code)   : {locker_code}")
    print(f"  Tên phát BLE          : {dev_name}")
    print(f"  Hệ điều hành hiện tại : {sys_name}")
    print("=" * 65)

    service = BleAdvertiserService(device_name=dev_name)
    service.start()

    print("\n[*] Đang duy trì trạng thái phát sóng BLE (Nhấn Ctrl+C để dừng)...")
    print("  - Trên Mobile App: Vào đơn hàng -> Bấm 'Mở tủ' -> Tab Bluetooth.")
    print("  - Khi đứng gần (<2m), App sẽ tự động nhận diện và hiện nút 'Mở ô ngay (1-chạm)'.\n")

    counter = 0
    try:
        while True:
            time.sleep(3)
            counter += 1
            print(f"  [{time.strftime('%H:%M:%S')}] [BLE BEACON HEARTBEAT #{counter}] Đang phát sóng: '{dev_name}' (TxPower: -59dBm)")
    except KeyboardInterrupt:
        print("\n[*] Đang dừng phát sóng BLE...")
        service.stop()
        print("[*] Đã dừng hoàn tất.")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Locker BLE Advertiser for Raspberry Pi & Simulation")
    parser.add_argument(
        "--locker-code",
        default=os.getenv("LOCKER_CODE", "CAB-TU01"),
        help="Mã tủ (mặc định: CAB-TU01 hoặc lấy từ biến LOCKER_CODE)",
    )
    args = parser.parse_args()
    run_advertiser(args.locker_code)
