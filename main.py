import time
import signal
import sys
import threading
from config.settings import settings
from infracstructure.mqtt_client import MQTTClientWrapper
from infracstructure.serial_manager import SerialManager
from infracstructure.cabinet_state import CabinetState
from hardware.rpi_locker import HardwareController
from infracstructure.database import DatabaseManager
from services.locker_service import LockerService
from services.heartbeat_service import HeartbeatService
from services.discovery_service import DiscoveryService
from utils.logger import get_logger

logger = get_logger("Main")

# ─── Graceful shutdown ───
_running = True


def _signal_handler(sig, frame):
    global _running
    logger.info(f"Received signal {sig}, shutting down...")
    _running = False


signal.signal(signal.SIGINT, _signal_handler)
signal.signal(signal.SIGTERM, _signal_handler)


def main():
    import logging
    from utils.logger import set_global_log_level
    # Default level is INFO for startup, we'll quiet it down later
    set_global_log_level(logging.INFO)

    logger.info("=" * 60)
    logger.info("  AISL Smart Locker System – Starting...")
    logger.info("=" * 60)
    logger.info(f"  MAC Address : {settings.MAC_ADDRESS}")
    logger.info(f"  Firmware    : {settings.FIRMWARE_VERSION}")
    logger.info("=" * 60)


    logger.info(f"   MQTT Broker : {settings.MQTT_BROKER}:{settings.MQTT_PORT_SSL if settings.MQTT_USE_TLS else settings.MQTT_PORT} ({settings.MQTT_TRANSPORT})")
    logger.info(f"   Serial Port : {settings.SERIAL_PORT}")

    # 2. Khởi tạo tầng Hardware (GPIO simulation)
    hw_controller = HardwareController()
    logger.info("[2/7] Hardware controller initialized")

    # 3. Khởi tạo phần cứng điều khiển khoá
    # SIMULATION=true cho phép chạy trên PC không có phần cứng (mặc định false).
    # HARDWARE_BACKEND=gpio: relay + cảm biến nối thẳng GPIO của Pi; mặc định rs485 (Arduino).
    # Hai loại có cùng giao diện nên các service bên dưới không phân biệt.
    import os
    simulation = os.getenv("SIMULATION", "false").lower() == "true"
    lids = {}   # {số trục: LidController}
    if settings.HARDWARE_BACKEND == "gpio" and not simulation:
        from hardware.factory import create_gpio_hardware
        serial_manager, lids = create_gpio_hardware(settings)
        logger.info(f"[3/7] GPIO hardware initialized (lid axes: {sorted(lids) or 'off'})")
    else:
        serial_manager = SerialManager(
            port=settings.SERIAL_PORT,
            baud_rate=settings.SERIAL_BAUD_RATE,
            simulation=simulation
        )
        logger.info("[3/7] Serial manager initialized")

    # 4. Khởi tạo Database Manager
    db_manager = DatabaseManager(settings.DATABASE_PATH)
    logger.info(f"[4/7] Database manager ready")

    # 4.1 Cập nhật settings từ DB (nếu có)
    settings.update_system_config(db_manager)
    mqtt_db_config = db_manager.get_mqtt_config()
    if mqtt_db_config:
        settings.update_mqtt_config(mqtt_db_config)
        logger.info("[4.1/7] MQTT settings overridden from Database")

    # 5. Khởi tạo Cabinet State
    # Tủ dự phòng từ LOCKER_ID (.env) khi Pi chưa được admin gán vào tủ nào (ADR-0008)
    cabinet_state = CabinetState(db_manager=db_manager,
                                 fallback_locker_id=settings.LOCKER_ID,
                                 fallback_slave_id=settings.GPIO_SLAVE_ID)
    if cabinet_state.is_configured:
        cabs = cabinet_state.all_cabinets
        source = "admin" if cabinet_state.provisioned_cabinets else "LOCKER_ID trong .env"
        logger.info(f"[5/7] Serving {len(cabs)} locker(s) ({source})")
        for i, c in enumerate(cabs):
            logger.info(f"   {i+1}. lockerId={c['id']} {c['name']} (slaveId={c.get('slaveId', 1)})")
    else:
        logger.info("[5/7] System state: NOT CONFIGURED — chờ admin gán Pi vào tủ (hoặc đặt LOCKER_ID)")

    # 4.2 Chạy Config API server (Local)
    from infracstructure.config_api import start_config_api
    start_config_api(db_manager, cabinet_state, port=8000,
                     hardware=serial_manager, lids=lids)
    logger.info("[4.2/7] Local Config API started on port 8000")

    # 6. Khởi tạo MQTT Client & Heartbeat (chưa connect)
    mqtt_wrapper = MQTTClientWrapper(on_message_callback=None)
    
    heartbeat_service = HeartbeatService(
        mqtt_client=mqtt_wrapper,
        cabinet_state=cabinet_state,
        interval=cabinet_state.heartbeat_interval if cabinet_state.is_configured else 60,
        hardware=serial_manager,
    )

    # 8. Khởi tạo Discovery Service
    discovery_service = DiscoveryService(mqtt_wrapper, serial_manager, cabinet_state=cabinet_state)
    logger.info("[6/7] Components initialized (Heartbeat, MQTT Client, Discovery)")

    # 7. Khởi tạo Locker Service
    locker_service = LockerService(
        mqtt_client=mqtt_wrapper,
        hardware_controller=hw_controller,
        serial_manager=serial_manager,
        cabinet_state=cabinet_state,
        heartbeat_service=heartbeat_service,
        db_manager=db_manager,
        discovery_service=discovery_service,
    )
    logger.info("[7/7] Services ready (Locker, Heartbeat, Discovery)")

    serial_manager.on_door_event = locker_service.handle_door_event
    serial_manager.on_reconnect = discovery_service.discover_and_report
    mqtt_wrapper.callback = locker_service.handle_incoming_message
    # Mỗi lần (kết nối lại) broker: báo discovery + heartbeat ngay để backend biết Pi đang online
    # và phục vụ tủ nào (heartbeat QoS 0 gửi lúc chưa kết nối thì mất, chờ tới 60 s).
    def _on_mqtt_connected():
        discovery_service.discover_and_report()
        heartbeat_service.publish_now()
    mqtt_wrapper.on_connected = _on_mqtt_connected

    # Start services at INFO level
    logger.info("System initializing...")

    # 8. Kết nối MQTT & start — topic lệnh được nhớ và subscribe lại sau mỗi lần kết nối
    locker_service.refresh_subscriptions()
    mqtt_wrapper.start()
    heartbeat_service.start()
    
    logger.warning("✅ System is READY (Logged at WARNING level)")

    # Giữ chương trình chạy
    try:
        while _running:
            time.sleep(1)
    except KeyboardInterrupt:
        pass
    finally:
        logger.info("Shutting down...")
        heartbeat_service.stop()
        locker_service.shutdown()
        for lid in lids.values():
            lid.shutdown()
        serial_manager.close()
        mqtt_wrapper.stop()
        logger.info("Goodbye!")


if __name__ == "__main__":
    main()
