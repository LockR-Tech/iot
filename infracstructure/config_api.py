from fastapi import FastAPI, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles
from fastapi.responses import FileResponse
from pydantic import BaseModel
from typing import Optional
import uvicorn
import threading
import os
from pathlib import Path
from utils.logger import get_logger
from config.settings import settings
from infracstructure.service_panel import ServicePanel, create_service_router, local_ip

logger = get_logger("ConfigAPI")

class MQTTConfigUpdate(BaseModel):
    broker: str
    port: int
    username: Optional[str] = None
    password: Optional[str] = None
    useTls: bool = True

def create_config_app(db_manager, cabinet_state, hardware=None, lids=None):
    app = FastAPI(title="AISL IoT Config API")
    
    # Add CORS middleware
    app.add_middleware(
        CORSMiddleware,
        allow_origins=["*"],  # Trong môi trường local cho phép tất cả
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
    )

    lids = lids or {}
    lid = lids.get(1)   # /hardware/lid/* điều khiển trục 1; mọi trục ở /service
    _get_local_ip = local_ip

    @app.get("/system/info")
    @app.get("/system/info/")
    async def get_system_info():
        loc = cabinet_state.location
        return {
            "macAddress": settings.MAC_ADDRESS,
            "version": settings.FIRMWARE_VERSION,
            "status": "online",
            "ipAddress": _get_local_ip(),
            "location": loc,
            "cabinetCount": len(cabinet_state.all_cabinets),
        }

    @app.get("/system/state")
    async def get_system_state():
        """Trả về toàn bộ trạng thái: location, cabinets."""
        return {
            "location": cabinet_state.location,
            "cabinets": cabinet_state.all_cabinets,
            "isConfigured": cabinet_state.is_configured,
        }

    @app.get("/logs/recent")
    async def get_recent_logs(limit: int = 50):
        """Lấy các MQTT log gần nhất."""
        try:
            logs = db_manager.get_recent_logs(limit=limit)
            return logs
        except Exception as e:
            raise HTTPException(status_code=500, detail=str(e))

    @app.get("/health")
    async def health_check():
        return {"status": "ok"}

    @app.get("/config/mqtt")
    async def get_mqtt_config():
        config = db_manager.get_mqtt_config()
        if not config:
            return {
                "broker": "localhost",
                "port": 1883,
                "username": "",
                "password": "",
                "useTls": True,
                "isDefault": True
            }
        return config

    @app.get("/config/system")
    async def get_system_config():
        return {
            "max_cabinets": db_manager.get_system_setting("max_cabinets", settings.MAX_CABINETS)
        }

    @app.post("/config/mqtt")
    async def update_mqtt_config(config: MQTTConfigUpdate):
        try:
            db_manager.save_mqtt_config(config.model_dump())
            logger.info(f"MQTT config updated via API: {config.broker}:{config.port}")
            return {"status": "success", "message": "MQTT config updated. Please restart the service to apply."}
        except Exception as e:
            logger.error(f"Failed to update MQTT config: {e}")
            raise HTTPException(status_code=500, detail=str(e))

    @app.post("/config/system")
    async def update_system_config(config: dict):
        try:
            for key, value in config.items():
                db_manager.save_system_setting(key, value)
            # Re-load settings
            settings.update_system_config(db_manager)
            logger.info(f"System config updated via API: {config}")
            return {"status": "success", "message": "System config updated."}
        except Exception as e:
            logger.error(f"Failed to update system config: {e}")
            raise HTTPException(status_code=500, detail=str(e))

    @app.post("/setup/clear")
    async def clear_setup():
        try:
            cabinet_state.clear()
            logger.info("Cabinet setup cleared via API")
            return {"status": "success", "message": "Cabinet setup cleared successfully."}
        except Exception as e:
            logger.error(f"Failed to clear setup: {e}")
            raise HTTPException(status_code=500, detail=str(e))

    # --- TEST API (Bypass hardware) ---
    class TestOpenOTPRequest(BaseModel):
        otp: str
        lockerId: Optional[str] = None
        boxId: Optional[str] = None

    @app.post("/test/open-otp")
    async def test_open_otp(req: TestOpenOTPRequest):
        logger.warning(f"🚀 MOCK API: Đã nhận mã OTP '{req.otp}' từ mobile để mở tủ (Không dùng linh kiện).")
        # Giả lập logic kiểm tra và mở khóa
        return {
            "success": True,
            "message": f"Giả lập mở tủ thành công với mã OTP: {req.otp}",
            "hardwareSimulated": True
        }

    # ─── Phần cứng: trạng thái cửa + nắp trượt ───
    # API này nghe 0.0.0.0 và không xác thực, nên lệnh chạy động cơ chỉ nhận từ
    # chính Pi (127.0.0.1) — từ laptop thì đi qua tunnel `ssh -L 8000:127.0.0.1:8000`.
    def _require_local(request: Request):
        host = request.client.host if request.client else ""
        if host not in ("127.0.0.1", "::1", "localhost"):
            raise HTTPException(status_code=403, detail="Chỉ gọi được từ chính Pi (dùng SSH tunnel).")

    @app.get("/hardware/status")
    async def get_hardware_status():
        doors = None
        if hardware is not None and hasattr(hardware, "door_states"):
            doors = [{"slot": i, "closed": closed} for i, closed in enumerate(hardware.door_states())]
        return {
            "backend": type(hardware).__name__ if hardware is not None else None,
            "connected": hardware.is_connected() if hardware is not None else False,
            "doors": doors,
            "lid": lid.status() if lid is not None else None,
            "lids": {axis: l.status() for axis, l in lids.items()},
        }

    @app.post("/hardware/lid/{action}")
    def control_lid(action: str, request: Request):
        # `def` thường (không async): FastAPI chạy trong threadpool, nên lệnh `stop`
        # vẫn vào được trong lúc `open`/`close` đang chạy động cơ.
        _require_local(request)
        if lid is None:
            raise HTTPException(status_code=404, detail="Nắp trượt chưa bật (LID_ENABLED=true, HARDWARE_BACKEND=gpio).")
        actions = {"open": lid.open, "close": lid.close, "home": lid.home, "stop": lid.stop}
        if action not in actions:
            raise HTTPException(status_code=400, detail=f"action phải là một trong {sorted(actions)}")
        logger.warning(f"Lid {action} requested via local API")
        return actions[action]()

    # ─── Bảng điều khiển kỹ thuật (/service) ───
    from hardware.factory import save_lid_tuning
    panel = ServicePanel(hardware, lids, cabinet_state, settings,
                         save_tuning=lambda: save_lid_tuning(settings.LID_TUNING_FILE, lids))
    app.include_router(create_service_router(panel))

    # ─── Static UI Dashboard ───
    _ui_dir = Path(__file__).parent.parent / "ui" / "dist"
    if _ui_dir.exists():
        app.mount("/ui", StaticFiles(directory=str(_ui_dir), html=True), name="ui")
        logger.info(f"Dashboard UI mounted at /ui (dir={_ui_dir})")
    else:
        logger.warning(f"UI directory not found at {_ui_dir}. Make sure you ran 'npm run build' inside ui/.")

    return app

def start_config_api(db_manager, cabinet_state, port: int = 8000, hardware=None, lids=None):
    app = create_config_app(db_manager, cabinet_state, hardware=hardware, lids=lids)
    
    def run():
        logger.info(f"Starting Local Config API on port {port}")
        uvicorn.run(app, host="0.0.0.0", port=port, log_level="info")

    api_thread = threading.Thread(target=run, daemon=True, name="ConfigAPIThread")
    api_thread.start()
    return api_thread
