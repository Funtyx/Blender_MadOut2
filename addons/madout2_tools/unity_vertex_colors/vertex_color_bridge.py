"""Local file bridge. Receiving is explicit, targets the active object, never saves .blend."""
import json
import tempfile
import time
import uuid
from pathlib import Path
import bpy

DEFAULT_FOLDER = Path(tempfile.gettempdir()) / "MadoutVertexPaintBridge"
_folder = DEFAULT_FOLDER
_session = ""
_window = None
_status = "Мост выключен"


def atomic_json(path, value):
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps(value, ensure_ascii=False), encoding="utf-8")
    temporary.replace(path)


def active_object():
    if _window is None:
        return None
    return _window.view_layer.objects.active


def status():
    return _status


def enabled():
    return bool(_session)


def start(context):
    global _session, _window, _status, _folder
    stop()
    addon = context.preferences.addons.get(__name__.partition(".")[0])
    configured = addon.preferences.exchange_folder if addon is not None else ""
    _folder = Path(bpy.path.abspath(configured)).expanduser() if configured else DEFAULT_FOLDER
    _folder.mkdir(parents=True, exist_ok=True)
    _session = uuid.uuid4().hex
    _window = context.window
    _status = "Жду передачу из Unity"
    tick()
    if _session and not bpy.app.timers.is_registered(tick):
        bpy.app.timers.register(tick, first_interval=0.5, persistent=False)


def stop():
    global _session, _window, _status
    if bpy.app.timers.is_registered(tick):
        bpy.app.timers.unregister(tick)
    heartbeat = _folder / "receiver.json"
    try:
        if heartbeat.exists() and json.loads(heartbeat.read_text())['session_id'] == _session:
            heartbeat.unlink()
    except (OSError, ValueError, KeyError):
        pass
    _session, _window = "", None
    _status = "Мост выключен"


def tick():
    global _status
    if not _session:
        return None
    try:
        if _window is None or _window not in list(bpy.context.window_manager.windows):
            stop()
            return None
        obj = active_object()
        valid = obj is not None and obj.type == 'MESH' and obj.mode in {'OBJECT', 'VERTEX_PAINT'}
        token = str(obj.as_pointer()) if valid else ""
        _folder.mkdir(parents=True, exist_ok=True)
        DEFAULT_FOLDER.mkdir(parents=True, exist_ok=True)
        atomic_json(DEFAULT_FOLDER / "receiver_location.json", {"folder": str(_folder), "session_id": _session})
        atomic_json(_folder / "receiver.json", {
            "session_id": _session, "heartbeat_unix_ms": int(time.time()*1000),
            "target_token": token, "target_name": obj.name if valid else "",
        })
        for request in sorted(_folder.glob("*.request.json")):
            data = json.loads(request.read_text(encoding="utf-8"))
            if data.get('receiver_session') != _session:
                continue
            request_id = data.get('request_id', '')
            if not request_id or request.name != request_id + '.request.json':
                continue
            acknowledgement = _folder / (request_id + '.ack.json')
            if acknowledgement.exists():
                continue
            try:
                if int(time.time()*1000)-data.get('sent_unix_ms', 0) > 60000:
                    raise ValueError("Запрос устарел. Отправьте цвета из Unity ещё раз")
                if not valid:
                    raise ValueError("Выберите Mesh в Object Mode или Vertex Paint")
                if data.get('target_token') != token:
                    raise ValueError("Выделение Blender изменилось после отправки. Отправьте ещё раз")
                area = next((area for area in _window.screen.areas if area.type == 'VIEW_3D'), None)
                if area is None:
                    raise ValueError("Откройте 3D View для приёма цветов")
                region = next(region for region in area.regions if region.type == 'WINDOW')
                with bpy.context.temp_override(window=_window, area=area, region=region):
                    result = bpy.ops.madout.import_vertex_colors('EXEC_DEFAULT', filepath=str(request))
                if result != {'FINISHED'}:
                    raise ValueError("Blender отменил импорт: проверьте совместимость меша")
                _status = "Принято: " + obj.name
                atomic_json(acknowledgement, {"status": "applied", "message": _status, "request_id": request_id})
            except Exception as exception:
                _status = str(exception)
                atomic_json(acknowledgement, {"status": "error", "message": _status, "request_id": request_id})
            break
        for area in _window.screen.areas:
            if area.type == 'VIEW_3D':
                area.tag_redraw()
    except Exception as exception:
        _status = "Ошибка моста: " + str(exception)
    return 0.5
