from logging import root
import sys
import os
import re
import json
import time
import socket
import platform
import subprocess
import threading
from datetime import datetime
from urllib.parse import urlparse
import urllib.request
import urllib.error

from PyQt5 import QtCore, QtGui, QtWidgets
from PyQt5.QtCore import Qt

# ─────────────────────────────────────────────────────────────
#  Settings persistence
# ─────────────────────────────────────────────────────────────
if getattr(sys, "frozen", False):
    SETTINGS_FILE = os.path.join(os.path.dirname(sys.executable), "fota_settings.json")
else:
    SETTINGS_FILE = os.path.join(os.path.dirname(__file__), "fota_settings.json")


def resource_path(relative_path):
    try:
        base_path = sys._MEIPASS
    except AttributeError:
        base_path = os.path.abspath(".")
    return os.path.join(base_path, relative_path)


def load_settings() -> dict:
    try:
        with open(SETTINGS_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {}


def save_settings(data: dict):
    try:
        with open(SETTINGS_FILE, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=2)
    except Exception:
        pass


# ─────────────────────────────────────────────────────────────
#  Theme / palette  —  a bit brighter / more "alive" than the CTk version
# ─────────────────────────────────────────────────────────────
BRAND        = "#6fae2e"
BRAND_HOVER  = "#5c9424"
BRAND_LIGHT  = "#eef6e4"

DANGER       = "#e0433a"
DANGER_HOVER = "#c73a30"

WARNING      = "#f39c12"
WARNING_HOVER = "#d68910"

INFO         = "#2f8fd6"
INFO_HOVER   = "#2477b3"

SUCCESS      = "#27ae60"
SUCCESS_HOVER = "#219150"

SLATE        = "#8592a3"
SLATE_HOVER  = "#6f7c8d"

PURPLE       = "#8e6fd1"
PURPLE_HOVER = "#7a5cc0"

BG_MAIN      = "#f4f7f2"
CARD_BG      = "#ffffff"
CARD_BORDER  = "#e4e8de"
MUTED_TEXT   = "#8a8f86"
TEXT_DARK    = "#2b2f27"

FONT_FAMILY  = "Segoe UI"
MONO_FAMILY  = "Consolas"


def qfont(family=FONT_FAMILY, size=11, weight=QtGui.QFont.Normal, italic=False):
    f = QtGui.QFont(family, size)
    f.setWeight(weight)
    f.setItalic(italic)
    return f


FONT_H1    = lambda: qfont(FONT_FAMILY, 19, QtGui.QFont.Bold)
FONT_H2    = lambda: qfont(FONT_FAMILY, 13, QtGui.QFont.Bold)
FONT_LABEL = lambda: qfont(FONT_FAMILY, 11, QtGui.QFont.DemiBold)
FONT_BODY  = lambda: qfont(FONT_FAMILY, 11)
FONT_SMALL = lambda: qfont(FONT_FAMILY, 10)
FONT_MONO  = lambda: qfont(MONO_FAMILY, 10)


# ─────────────────────────────────────────────────────────────
#  Thread → UI-thread dispatcher (mirrors Tk's `self.after(0, fn, *a)`)
# ─────────────────────────────────────────────────────────────
class _Dispatcher(QtCore.QObject):
    fire = QtCore.pyqtSignal(object, tuple, dict)

    def __init__(self):
        super().__init__()
        self.fire.connect(self._run, QtCore.Qt.QueuedConnection)

    def _run(self, fn, args, kwargs):
        fn(*args, **kwargs)


# ─────────────────────────────────────────────────────────────
#  tkinter.messagebox-compatible shim, backed by QMessageBox
# ─────────────────────────────────────────────────────────────
class mb:
    @staticmethod
    def showerror(title, message):
        box = QtWidgets.QMessageBox(QtWidgets.QMessageBox.Critical, title, message)
        box.setStyleSheet(_MSGBOX_QSS)
        box.exec_()

    @staticmethod
    def showinfo(title, message):
        box = QtWidgets.QMessageBox(QtWidgets.QMessageBox.Information, title, message)
        box.setStyleSheet(_MSGBOX_QSS)
        box.exec_()

    @staticmethod
    def askyesno(title, message):
        box = QtWidgets.QMessageBox(QtWidgets.QMessageBox.Question, title, message,
                                     QtWidgets.QMessageBox.Yes | QtWidgets.QMessageBox.No)
        box.setStyleSheet(_MSGBOX_QSS)
        return box.exec_() == QtWidgets.QMessageBox.Yes


_MSGBOX_QSS = f"""
QMessageBox {{ background-color: {CARD_BG}; }}
QMessageBox QLabel {{ color: {TEXT_DARK}; font-size: 11pt; }}
QPushButton {{
    background-color: {BRAND}; color: white; border: none;
    border-radius: 7px; padding: 7px 18px; font-weight: 600; min-width: 70px;
}}
QPushButton:hover {{ background-color: {BRAND_HOVER}; }}
"""


# ─────────────────────────────────────────────────────────────
#  Tiny StringVar shim (tkinter-style .get()/.set(), Qt-signal driven)
# ─────────────────────────────────────────────────────────────
class StringVar(QtCore.QObject):
    changed = QtCore.pyqtSignal(str)

    def __init__(self, value=""):
        super().__init__()
        self._value = value

    def get(self):
        return self._value

    def set(self, value):
        self._value = value
        self.changed.emit(value)


# ─────────────────────────────────────────────────────────────
#  IPv6 socket helpers  (unchanged from the CustomTkinter version)
# ─────────────────────────────────────────────────────────────
def resolve_ipv6_sockaddr(ip: str, port: int):
    infos = socket.getaddrinfo(ip, port, socket.AF_INET6, socket.SOCK_STREAM)
    return infos[0][4]


def make_tcp_socket(timeout=5):
    s = socket.socket(socket.AF_INET6, socket.SOCK_STREAM)
    s.setsockopt(socket.IPPROTO_IPV6, socket.IPV6_V6ONLY, 0)
    s.settimeout(timeout)
    return s


def make_udp_socket(timeout=3):
    s = socket.socket(socket.AF_INET6, socket.SOCK_DGRAM)
    s.setsockopt(socket.IPPROTO_IPV6, socket.IPV6_V6ONLY, 0)
    s.settimeout(timeout)
    return s


def validate_ipv6_format(ip: str):
    ip = (ip or "").strip()
    if not ip:
        return False, "NIC IPv6 address field is empty."
    try:
        socket.inet_pton(socket.AF_INET6, ip)
        return True, "Valid IPv6 literal address."
    except OSError:
        pass
    try:
        socket.getaddrinfo(ip, None, socket.AF_INET6)
        return True, "Hostname resolves to a valid IPv6 address."
    except socket.gaierror:
        return False, "\"" + ip + "\" is not a valid IPv6 address or resolvable hostname."
    except Exception as e:
        return False, "IPv6 validation error: " + type(e).__name__ + ": " + str(e)


# ─────────────────────────────────────────────────────────────
#  NIC Firmware reader — HDLC frame sequence (unchanged logic)
# ─────────────────────────────────────────────────────────────
def printable_ascii(data: bytes) -> str:
    return ''.join(chr(b) if 32 <= b <= 126 else '.' for b in data)


def decode_firmware_response(rx: bytes) -> dict:
    result = {
        "raw_hex":      rx.hex(' ').upper(),
        "ascii":        printable_ascii(rx),
        "imei":         None,
        "firmware":     None,
        "manufacturer": None,
    }

    matches = re.findall(rb"\d{15}", rx)
    if matches:
        result["imei"] = matches[0].decode()

    idx = rx.find(b"HW")
    if idx != -1:
        fw = bytearray()
        while idx < len(rx):
            c = rx[idx]
            if 32 <= c <= 126:
                fw.append(c)
            else:
                break
            idx += 1
        result["firmware"] = fw.decode()

    if b"Landis+Gyr" in rx:
        start = rx.find(b"Landis+Gyr")
        mfr = bytearray()
        while start < len(rx):
            c = rx[start]
            if 32 <= c <= 126:
                mfr.append(c)
            else:
                break
            start += 1
        result["manufacturer"] = mfr.decode()

    return result


def _safe_close_socket(sock):
    if sock is None:
        return
    try:
        sock.shutdown(socket.SHUT_RDWR)
    except OSError:
        pass
    try:
        sock.close()
    except Exception:
        pass


HDLC_FRAMES = [
    ("SNRM",
     "7E A0 23 00 02 04 01 FD 93 56 61 81 80 14 05 02 27 0F 06 02 27 0F 07 04 00 00 00 01 08 04 00 00 00 01 BA E4 7E"),

    ("AARQ",
     "7E A0 4F 00 02 04 01 FD 10 DA C2 E6 E6 00 60 3E A1 09 06 07 60 85 74 05 08 01 01 8A 02 07 80 8B 07 60 85 74 05 08 02 02 AC 12 80 10 31 31 31 31 31 31 31 31 31 31 31 31 31 31 31 31 BE 10 04 0E 01 00 00 00 06 5F 1F 04 00 1C FF 3F 27 0F AD 12 7E"),

    ("RLRQ",
     "7E A0 2E 00 02 04 01 FD 32 9A FB E6 E6 00 C3 01 C1 00 0F 00 00 28 00 00 FF 01 01 09 10 FC 91 AE 8C 11 D3 8D 3E 50 19 91 CF D4 30 79 AF D3 D1 7E"),

    ("NIC Firmware",
     "7E A0 1C 00 02 04 01 FD 54 5B 1C E6 E6 00 C0 01 C1 00 01 00 87 64 00 00 FF 02 00 64 FA 7E"),

    ("DISC",
     "7E A0 0A 00 02 04 01 FD 53 E0 85 7E"),
]


def hdlc_read_firmware_blocking(ip: str, port: int, log_cb):
    """Blocking HDLC read: SNRM → AARQ → RLRQ → NIC Firmware → DISC.
    Returns (ok, message, result_dict|None)."""
    sock = None
    current_frame_name = None
    try:
        sa = resolve_ipv6_sockaddr(ip, port)
        log_cb(f"HDLC  Connecting to [{sa[0]}]:{sa[1]} …")
        sock = socket.socket(socket.AF_INET6, socket.SOCK_STREAM)
        sock.setsockopt(socket.IPPROTO_IPV6, socket.IPV6_V6ONLY, 0)
        sock.settimeout(5)
        sock.connect(sa)
        log_cb("HDLC  Connected ✔")

        result = None
        for name, hexframe in HDLC_FRAMES:
            current_frame_name = name
            frame = bytes.fromhex(hexframe.replace(" ", ""))
            log_cb(f"HDLC  TX [{name}]  {len(frame)} bytes")

            try:
                sock.sendall(frame)
            except OSError as e:
                msg = f"Meter is not sending RX against {name} — TX failed ({type(e).__name__}: {e})."
                log_cb(f"HDLC  ❌ {msg}")
                _safe_close_socket(sock)
                return False, msg, None

            time.sleep(1)

            try:
                rx = sock.recv(4096)
            except socket.timeout:
                rx = b""
            except OSError as e:
                msg = f"Meter is not sending RX against {name} — connection dropped ({type(e).__name__}: {e})."
                log_cb(f"HDLC  ❌ {msg}")
                _safe_close_socket(sock)
                return False, msg, None

            if not rx:
                msg = f"Meter is not sending RX against {name}."
                log_cb(f"HDLC  ❌ {msg}")
                _safe_close_socket(sock)
                return False, msg, None

            log_cb(f"HDLC  RX [{name}]  {len(rx)} bytes  |  HEX: {rx.hex(' ').upper()[:80]}{'…' if len(rx)>40 else ''}")
            if name == "NIC Firmware":
                result = decode_firmware_response(rx)

        _safe_close_socket(sock)
        log_cb("HDLC  Session closed.")
        time.sleep(1.5)

        if result is None or not result.get("firmware"):
            return False, "HDLC sequence completed but no firmware string was returned by the NIC.", result
        return True, "OK", result
    except Exception as exc:
        _safe_close_socket(sock)
        if current_frame_name:
            msg = f"Meter is not sending RX against {current_frame_name} — {type(exc).__name__}: {exc}"
        else:
            msg = f"{type(exc).__name__}: {exc}"
        log_cb(f"HDLC  ❌ {msg}")
        return False, msg, None


def run_nic_firmware_sequence(ip: str, port: int, log_cb, done_cb):
    def _worker():
        result = None
        sock = None
        current_frame_name = None
        try:
            sa = resolve_ipv6_sockaddr(ip, port)
            log_cb(f"NIC-FW  Connecting to [{sa[0]}]:{sa[1]} …")
            sock = socket.socket(socket.AF_INET6, socket.SOCK_STREAM)
            sock.setsockopt(socket.IPPROTO_IPV6, socket.IPV6_V6ONLY, 0)
            sock.settimeout(5)
            sock.connect(sa)
            log_cb("NIC-FW  Connected ✔")

            for name, hexframe in HDLC_FRAMES:
                current_frame_name = name
                frame = bytes.fromhex(hexframe.replace(" ", ""))
                log_cb(f"NIC-FW  TX [{name}]  {len(frame)} bytes")

                try:
                    sock.sendall(frame)
                except OSError as e:
                    msg = f"Meter is not sending RX against {name} — TX failed ({type(e).__name__}: {e})."
                    log_cb(f"NIC-FW  ❌ {msg}")
                    _safe_close_socket(sock)
                    done_cb(None, msg)
                    return

                time.sleep(1)

                try:
                    rx = sock.recv(4096)
                except socket.timeout:
                    rx = b""
                except OSError as e:
                    msg = f"Meter is not sending RX against {name} — connection dropped ({type(e).__name__}: {e})."
                    log_cb(f"NIC-FW  ❌ {msg}")
                    _safe_close_socket(sock)
                    done_cb(None, msg)
                    return

                if not rx:
                    msg = f"Meter is not sending RX against {name}."
                    log_cb(f"NIC-FW  ❌ {msg}")
                    _safe_close_socket(sock)
                    done_cb(None, msg)
                    return

                log_cb(f"NIC-FW  RX [{name}]  {len(rx)} bytes  |  HEX: {rx.hex(' ').upper()[:80]}{'…' if len(rx)>40 else ''}")
                if name == "NIC Firmware":
                    result = decode_firmware_response(rx)

            _safe_close_socket(sock)
            log_cb("NIC-FW  Session closed.")
        except Exception as exc:
            _safe_close_socket(sock)
            if current_frame_name:
                err_msg = f"Meter is not sending RX against {current_frame_name} — {type(exc).__name__}: {exc}"
            else:
                err_msg = f"{type(exc).__name__}: {exc}"
            log_cb(f"NIC-FW  ❌ {err_msg}")
            done_cb(None, err_msg)
            return

        if result is None or not result.get("firmware"):
            done_cb(None, "HDLC sequence completed but no firmware string was returned by the NIC.")
            return
        done_cb(result, None)

    threading.Thread(target=_worker, daemon=True).start()


# ─────────────────────────────────────────────────────────────
#  Styled widget building blocks
# ─────────────────────────────────────────────────────────────
class Card(QtWidgets.QFrame):
    """A consistently-styled rounded 'card' panel used throughout the UI."""
    def __init__(self, parent=None):
        super().__init__(parent)
        self.setStyleSheet(f"""
            QFrame#card {{
                background-color: {CARD_BG};
                border: 1px solid {CARD_BORDER};
                border-radius: 14px;
            }}
        """)
        self.setObjectName("card")
        shadow = QtWidgets.QGraphicsDropShadowEffect(self)
        shadow.setBlurRadius(18)
        shadow.setOffset(0, 3)
        shadow.setColor(QtGui.QColor(0, 0, 0, 28))
        self.setGraphicsEffect(shadow)


class Label(QtWidgets.QLabel):
    def __init__(self, text="", font=None, color=None, textvariable=None, parent=None):
        super().__init__(text, parent)
        self.setFont(font() if font else FONT_BODY())
        self._color = color
        if color:
            self.setStyleSheet(f"color: {color};")
        if textvariable is not None:
            self.setText(textvariable.get())
            textvariable.changed.connect(self.setText)

    def set_color(self, color):
        self._color = color
        self.setStyleSheet(f"color: {color};")


class StatusPill(QtWidgets.QLabel):
    def __init__(self, text="", bg="#8a9c5c", parent=None):
        super().__init__(text, parent)
        self.setFont(FONT_LABEL())
        self.setAlignment(Qt.AlignCenter)
        self._apply(bg)

    def _apply(self, bg):
        self.setStyleSheet(f"""
            QLabel {{
                color: white; background-color: {bg};
                border-radius: 13px; padding: 6px 16px;
            }}
        """)

    def set_status(self, text, bg):
        self.setText(text)
        self._apply(bg)


class Button(QtWidgets.QPushButton):
    def __init__(self, text="", fg_color=BRAND, hover_color=BRAND_HOVER,
                 width=None, height=36, font=None, parent=None):
        super().__init__(text, parent)
        self.setFont(font() if font else FONT_LABEL())
        self.setCursor(Qt.PointingHandCursor)
        self.setFixedHeight(height)
        if width:
            self.setMinimumWidth(width)
        self._fg, self._hover = fg_color, hover_color
        self._apply_style()

    def _apply_style(self):
        self.setStyleSheet(f"""
            QPushButton {{
                background-color: {self._fg}; color: white; border: none;
                border-radius: 9px; padding: 6px 16px; font-weight: 600;
            }}
            QPushButton:hover:!disabled {{ background-color: {self._hover}; }}
            QPushButton:disabled {{ background-color: #cfd3c8; color: #90948a; }}
        """)

    def set_colors(self, fg_color, hover_color):
        self._fg, self._hover = fg_color, hover_color
        self._apply_style()


class LineEdit(QtWidgets.QLineEdit):
    def __init__(self, text="", width=None, mono=False, parent=None):
        super().__init__(text, parent)
        self.setFont(FONT_MONO() if mono else FONT_BODY())
        self.setFixedHeight(34)
        if width:
            self.setFixedWidth(width)
        self.setStyleSheet(f"""
            QLineEdit {{
                border: 1px solid {CARD_BORDER}; border-radius: 8px;
                padding: 4px 10px; background: white; color: {TEXT_DARK};
            }}
            QLineEdit:focus {{ border: 1.5px solid {BRAND}; }}
            QLineEdit:disabled {{ background: #f0f1ec; color: #9a9d94; }}
            QLineEdit:read-only {{ background: #f6f8f4; color: #555; }}
        """)


class ComboBox(QtWidgets.QComboBox):
    def __init__(self, values=None, width=None, parent=None):
        super().__init__(parent)
        self.setFont(FONT_BODY())
        self.setFixedHeight(34)
        if width:
            self.setFixedWidth(width)
        if values:
            self.addItems(values)
        self.setStyleSheet(f"""
            QComboBox {{
                border: 1px solid {CARD_BORDER}; border-radius: 8px;
                padding: 4px 10px; background: white; color: {TEXT_DARK};
            }}
            QComboBox:focus {{ border: 1.5px solid {BRAND}; }}
            QComboBox::drop-down {{ border: none; width: 24px; }}
            QComboBox QAbstractItemView {{
                border: 1px solid {CARD_BORDER}; selection-background-color: {BRAND_LIGHT};
                selection-color: {TEXT_DARK};
            }}
        """)


class TextBox(QtWidgets.QPlainTextEdit):
    def __init__(self, mono=True, height=None, readonly=False, parent=None):
        super().__init__(parent)
        self.setFont(FONT_MONO() if mono else FONT_BODY())
        if height:
            self.setFixedHeight(height)
        self.setReadOnly(readonly)
        self.setStyleSheet(f"""
            QPlainTextEdit {{
                border: 1px solid {CARD_BORDER}; border-radius: 10px;
                padding: 8px; background: #fbfcfa; color: {TEXT_DARK};
            }}
        """)

    def append_line(self, text):
        self.moveCursor(QtGui.QTextCursor.End)
        self.insertPlainText(text)
        self.moveCursor(QtGui.QTextCursor.End)
        self.ensureCursorVisible()

    def clear_all(self):
        self.clear()


class ProgressBar(QtWidgets.QProgressBar):
    def __init__(self, height=14, parent=None):
        super().__init__(parent)
        self.setRange(0, 1000)
        self.setTextVisible(False)
        self.setFixedHeight(height)
        self._color = BRAND
        self._apply()

    def set_fraction(self, frac):
        self.setValue(int(max(0.0, min(1.0, frac)) * 1000))

    def set_color(self, color):
        self._color = color
        self._apply()

    def _apply(self):
        self.setStyleSheet(f"""
            QProgressBar {{
                background: #e2e5df; border-radius: 7px; border: none;
            }}
            QProgressBar::chunk {{
                background-color: {self._color}; border-radius: 7px;
            }}
        """)


# ─────────────────────────────────────────────────────────────
#  NIC Firmware Result Dialog
# ─────────────────────────────────────────────────────────────
class NicFirmwareDialog(QtWidgets.QDialog):
    def __init__(self, parent, result, error_msg=None):
        super().__init__(parent)
        self.setWindowTitle("NIC Firmware Info")
        self.resize(680, 460)
        self.setStyleSheet(f"QDialog {{ background-color: {BG_MAIN}; }}")

        root = QtWidgets.QVBoxLayout(self)
        root.setContentsMargins(24, 20, 24, 18)
        root.setSpacing(10)

        root.addWidget(Label("📟  NIC Firmware Details", font=FONT_H1))

        if result is None:
            root.addSpacing(10)
            err_title = Label("❌  Could not retrieve firmware version.", font=FONT_BODY, color=DANGER)
            root.addWidget(err_title)
            err_box = Label(error_msg or "Unknown error — check logs.", font=FONT_MONO, color=DANGER)
            err_box.setWordWrap(True)
            root.addWidget(err_box)
        else:
            card = Card(self)
            grid = QtWidgets.QGridLayout(card)
            grid.setContentsMargins(16, 12, 16, 12)
            grid.setVerticalSpacing(10)

            def row(r, label, value, color=None):
                grid.addWidget(Label(label, font=FONT_LABEL), r, 0, alignment=Qt.AlignLeft)
                v = Label(value or "—", font=FONT_BODY, color=color)
                v.setWordWrap(True)
                grid.addWidget(v, r, 1, alignment=Qt.AlignLeft)

            fw = result.get("firmware")
            row(0, "🔧  Firmware",     fw or "Not found", SUCCESS if fw else DANGER)
            row(1, "🆔  IMEI",         result.get("imei") or "Not found")
            row(2, "🏭  Manufacturer", result.get("manufacturer") or "Not found")
            root.addWidget(card)

            root.addWidget(Label("ASCII representation:", font=FONT_LABEL))
            ascii_box = TextBox(mono=True, height=70, readonly=True)
            ascii_box.setPlainText(result.get("ascii", ""))
            root.addWidget(ascii_box)

            root.addWidget(Label("Raw HEX:", font=FONT_LABEL))
            hex_box = TextBox(mono=True, height=60, readonly=True)
            hex_box.setPlainText(result.get("raw_hex", ""))
            root.addWidget(hex_box)

        root.addStretch(1)
        btn_row = QtWidgets.QHBoxLayout()
        btn_row.addStretch(1)
        close_btn = Button("Close", fg_color=SLATE, hover_color=SLATE_HOVER, width=110)
        close_btn.clicked.connect(self.accept)
        btn_row.addWidget(close_btn)
        btn_row.addStretch(1)
        root.addLayout(btn_row)


# ─────────────────────────────────────────────────────────────
#  Ping Dialog
# ─────────────────────────────────────────────────────────────
class PingDialog(QtWidgets.QDialog):
    _log_signal = QtCore.pyqtSignal(str)
    _status_signal = QtCore.pyqtSignal(object)
    _reenable_signal = QtCore.pyqtSignal()

    def __init__(self, parent, default_ip=""):
        super().__init__(parent)
        self.setWindowTitle("Ping Tool")
        self.resize(660, 480)
        self.setStyleSheet(f"QDialog {{ background-color: {BG_MAIN}; }}")

        self._log_signal.connect(self._append)
        self._status_signal.connect(self._set_status)
        self._reenable_signal.connect(lambda: self.ping_btn.setEnabled(True))

        root = QtWidgets.QVBoxLayout(self)
        root.setContentsMargins(18, 16, 18, 14)
        root.setSpacing(10)

        root.addWidget(Label("🔔  Ping Tool", font=FONT_H1))

        top = Card(self)
        top_l = QtWidgets.QHBoxLayout(top)
        top_l.setContentsMargins(14, 12, 14, 12)
        top_l.addWidget(Label("Address:", font=FONT_LABEL))
        self.ip_edit = LineEdit(default_ip, width=320)
        top_l.addWidget(self.ip_edit)
        top_l.addWidget(Label("Count:", font=FONT_LABEL))
        self.count_edit = LineEdit("4", width=60)
        top_l.addWidget(self.count_edit)
        self.ping_btn = Button("📡  Ping", fg_color=INFO, hover_color=INFO_HOVER, width=100)
        self.ping_btn.clicked.connect(self.run_ping)
        top_l.addWidget(self.ping_btn)
        top_l.addStretch(1)
        root.addWidget(top)

        self.status_label = Label("", font=FONT_H2)
        root.addWidget(self.status_label)

        out_card = Card(self)
        out_l = QtWidgets.QVBoxLayout(out_card)
        out_l.setContentsMargins(12, 10, 12, 10)
        out_l.addWidget(Label("Output", font=FONT_LABEL))
        self.output_box = TextBox(mono=True, readonly=True)
        out_l.addWidget(self.output_box)
        root.addWidget(out_card, 1)

        btn_row = QtWidgets.QHBoxLayout()
        clear_btn = Button("Clear", fg_color=SLATE, hover_color=SLATE_HOVER, width=90)
        clear_btn.clicked.connect(self.output_box.clear_all)
        btn_row.addWidget(clear_btn)
        btn_row.addStretch(1)
        close_btn = Button("Close", fg_color=SLATE, hover_color=SLATE_HOVER, width=90)
        close_btn.clicked.connect(self.accept)
        btn_row.addWidget(close_btn)
        root.addLayout(btn_row)

    def _append(self, text):
        self.output_box.append_line(text)

    def _set_status(self, success):
        if success is True:
            self.status_label.setText("✅  Host reachable")
            self.status_label.set_color(SUCCESS)
        elif success is False:
            self.status_label.setText("❌  Host unreachable / timeout")
            self.status_label.set_color(DANGER)
        else:
            self.status_label.setText("⏳  Pinging…")
            self.status_label.set_color(WARNING)

    def run_ping(self):
        ip = self.ip_edit.text().strip()
        if not ip:
            self._append("⚠  No address entered.\n")
            return
        try:
            count = int(self.count_edit.text().strip())
        except ValueError:
            count = 4
        self.output_box.clear_all()
        self._set_status(None)
        self.ping_btn.setEnabled(False)
        threading.Thread(target=self._ping_worker, args=(ip, count), daemon=True).start()

    def _ping_worker(self, ip, count):
        is_win = platform.system().lower() == "windows"
        is_v6  = ":" in ip
        if is_v6:
            if is_win:
                candidates = [["ping", "-6", "-n", str(count), ip]]
            else:
                candidates = [["ping6", "-c", str(count), ip],
                              ["ping",  "-6", "-c", str(count), ip]]
        else:
            flag = "-n" if is_win else "-c"
            candidates = [["ping", flag, str(count), ip]]

        proc = None
        for cmd in candidates:
            self._log_signal.emit("$ " + " ".join(cmd) + "\n\n")
            try:
                proc = subprocess.Popen(cmd, stdout=subprocess.PIPE,
                                        stderr=subprocess.STDOUT, text=True)
                break
            except FileNotFoundError:
                self._log_signal.emit("Not found: " + cmd[0] + "\n")
                proc = None

        if proc is None:
            self._log_signal.emit("⚠  No suitable ping command found.\n")
            self._status_signal.emit(False)
            self._reenable_signal.emit()
            return

        success = None
        for line in proc.stdout:
            self._log_signal.emit(line)
            ll = line.lower()
            if any(k in ll for k in ("ttl=", "time=", "bytes from")):
                success = True
            if any(k in ll for k in ("unreachable", "100% packet loss",
                                      "request timed out", "unknown host",
                                      "network is unreachable")):
                success = False
        proc.wait()
        if success is None:
            success = (proc.returncode == 0)
        self._status_signal.emit(success)
        self._reenable_signal.emit()


# ─────────────────────────────────────────────────────────────
#  FOTA Step Progress Dialog — driven by REAL step results
# ─────────────────────────────────────────────────────────────
class FotaStepDialog(QtWidgets.QDialog):
    STATUS_ICON = {"pending": "⚪", "running": "⏳", "ok": "✅", "fail": "❌", "warn": "⚠️"}

    def __init__(self, parent, steps):
        super().__init__(parent)
        self.setWindowTitle("FOTA Update — Live Progress")
        self.resize(900, 650)
        self.setMinimumWidth(900)
        self.setModal(True)
        self.setStyleSheet(f"QDialog {{ background-color: {BG_MAIN}; }}")

        self._cancelled = False
        self._done      = False
        self._steps     = steps

        root = QtWidgets.QVBoxLayout(self)
        root.setContentsMargins(22, 22, 22, 20)
        root.setSpacing(10)

        title = Label("🚀  FOTA Update in Progress", font=FONT_H1)
        title.setAlignment(Qt.AlignCenter)
        root.addWidget(title)
        sub = Label("Please wait — all controls are locked until this finishes.",
                     font=FONT_SMALL, color=MUTED_TEXT)
        sub.setAlignment(Qt.AlignCenter)
        root.addWidget(sub)

        self.overall_bar = ProgressBar(height=14)
        root.addWidget(self.overall_bar)
        root.addSpacing(5)
        self.overall_pct = Label("0 / " + str(len(steps)) + " steps complete",
                                  font=FONT_SMALL, color=MUTED_TEXT)
        self.overall_pct.setAlignment(Qt.AlignCenter)
        root.addWidget(self.overall_pct)

        steps_card = Card(self)
        steps_l = QtWidgets.QVBoxLayout(steps_card)
        steps_l.setContentsMargins(14, 10, 14, 10)
        steps_l.setSpacing(9)

        self._row_widgets = []
        for i, label in enumerate(steps):

            row = QtWidgets.QHBoxLayout()
            row.setSpacing(12)

            # Status Icon
            icon = Label("⚪", font=lambda: qfont(FONT_FAMILY, 15))
            icon.setFixedWidth(32)
            icon.setAlignment(Qt.AlignCenter)
            row.addWidget(icon)

            # Step Name
            text = Label(str(i + 1) + ". " + label, font=FONT_BODY)
            text.setMinimumWidth(360)
            text.setMaximumWidth(360)
            text.setWordWrap(False)
            text.setAlignment(Qt.AlignVCenter | Qt.AlignLeft)
            row.addWidget(text)

            # Status / Detail
            detail = Label("", font=FONT_SMALL, color=MUTED_TEXT)
            detail.setWordWrap(False)
            detail.setAlignment(Qt.AlignLeft | Qt.AlignVCenter)
            row.addWidget(detail, 1)

            steps_l.addLayout(row)

            self._row_widgets.append((icon, text, detail))        
        
        root.addWidget(steps_card)

        root.addWidget(Label("Live Log", font=FONT_LABEL))
        self.log_box = TextBox(mono=True, height=140, readonly=True)
        root.addWidget(self.log_box)

        btn_row = QtWidgets.QHBoxLayout()
        btn_row.addStretch(1)
        self.close_btn = Button("Cancel", fg_color=DANGER, hover_color=DANGER_HOVER, width=130)
        self.close_btn.clicked.connect(self._cancel)
        btn_row.addWidget(self.close_btn)
        btn_row.addStretch(1)
        root.addLayout(btn_row)

    def closeEvent(self, event):
        # mirror the Tk version: block the window-close button until done
        if not self._done:
            event.ignore()
        else:
            event.accept()

    def _cancel(self):
        self._cancelled = True
        self.close_btn.setEnabled(False)
        self.close_btn.setText("Cancelling …")

    def _log(self, text):
        ts = datetime.now().strftime("%H:%M:%S")
        self.log_box.append_line("[" + ts + "] " + text + "\n")

    def set_step_status(self, i, status, detail=None):
        icon, text, detail_lbl = self._row_widgets[i]
        icon.setText(self.STATUS_ICON.get(status, "⚪"))
        color_map = {"running": WARNING, "ok": SUCCESS, "fail": DANGER,
                     "pending": TEXT_DARK, "warn": WARNING}
        text.set_color(color_map.get(status, TEXT_DARK))
        if detail:
            detail_lbl.setText(detail)

        done_count = sum(1 for ic, _, _ in self._row_widgets
                          if ic.text() == self.STATUS_ICON["ok"])
        total = len(self._row_widgets)
        self.overall_bar.set_fraction(done_count / total if total else 0)
        self.overall_pct.setText(str(done_count) + " / " + str(total) + " steps complete")

        self._log(self._steps[i] + " — " + status.upper() + (("  (" + detail + ")") if detail else ""))

    def update_wait_countdown(self, i, remaining):
        _, _, detail_lbl = self._row_widgets[i]
        detail_lbl.setText(str(remaining) + "s remaining …")

    def mark_failed(self, i, reason):
        self.set_step_status(i, "fail", reason)
        self.overall_bar.set_color(DANGER)
        self.close_btn.setText("Close")
        self.close_btn.set_colors(DANGER, DANGER_HOVER)
        self.close_btn.setEnabled(True)
        try:
            self.close_btn.clicked.disconnect()
        except TypeError:
            pass
        self.close_btn.clicked.connect(self.accept)
        self._done = True

    def mark_cancelled(self):
        self.overall_bar.set_color(WARNING)
        self._log("⚠ Cancelled by user.")
        self.close_btn.setText("Close")
        self.close_btn.set_colors(WARNING, WARNING_HOVER)
        self.close_btn.setEnabled(True)
        try:
            self.close_btn.clicked.disconnect()
        except TypeError:
            pass
        self.close_btn.clicked.connect(self.accept)
        self._done = True

    def mark_all_complete(self, old_ver, new_ver):
        self.overall_bar.set_fraction(1.0)
        self.overall_bar.set_color(SUCCESS)
        self._log("✅ FOTA sequence complete.  " + (old_ver or "?") + "  →  " + (new_ver or "?"))
        self.close_btn.setText("Close")
        self.close_btn.set_colors(SUCCESS, SUCCESS_HOVER)
        self.close_btn.setEnabled(True)
        try:
            self.close_btn.clicked.disconnect()
        except TypeError:
            pass
        self.close_btn.clicked.connect(self.accept)
        self._done = True


# ─────────────────────────────────────────────────────────────
#  Main App
# ─────────────────────────────────────────────────────────────
class OrionFOTAApp(QtWidgets.QMainWindow):

    FOTA_STEPS = [
        "Validate firmware URL",
        "Validate NIC IPv6 address",
        "Verify firmware file is accessible",
        "Ping test the NIC",
        "Read current firmware version",
        "Send FOTA command",
        "Close socket & wait for NIC to update (30s)",
        "Reconnect & verify firmware version",
    ]

    def __init__(self):
        super().__init__()
        self.setWindowTitle("⚡ ORION EC200 / EC800 Firmware Updater")
        self.resize(1240, 920)
        self.setMinimumSize(1040, 740)
        self.setStyleSheet(f"QMainWindow {{ background-color: {BG_MAIN}; }}")

        self._dispatch = _Dispatcher()

        self._fota_running = False
        self._fw_btn_busy  = False
        self._settings     = load_settings()
        self._freezable    = []   # every control that must lock during FOTA

        self.build_ui()
        self._restore_settings()

    # ── thread → UI-thread dispatch (mirrors Tk's self.after(0, fn, *a)) ──
    def after(self, _ms, fn, *args, **kwargs):
        self._dispatch.fire.emit(fn, args, kwargs)

    def closeEvent(self, event):
        if self._fota_running:
            if not mb.askyesno("FOTA in progress",
                                "A FOTA update is currently running.\n\n"
                                "Closing now may interrupt the update. Close anyway?"):
                event.ignore()
                return
        self._save_current_settings()
        event.accept()

    # ── Settings ──────────────────────────────────────────────
    def _save_current_settings(self):
        data = {
            "ip":       self.ip_entry.text().strip(),
            "port":     self.nic_port_entry.text().strip(),
            "protocol": "TCP" if self.rb_tcp.isChecked() else "UDP",
            "fota": {label: self._entry_value(e) for label, e in self.entries.items()
                     if e is not None},
            "fp_url":   self.fp_url_entry.text().strip(),
            "fp_func":  self.fp_func_entry.text().strip(),
            "fp_fname": self.fp_fname_entry.text().strip(),
        }
        save_settings(data)

    @staticmethod
    def _entry_value(e):
        if isinstance(e, QtWidgets.QComboBox):
            return e.currentText()
        return e.text()

    @staticmethod
    def _set_entry_value(e, val):
        if isinstance(e, QtWidgets.QComboBox):
            idx = e.findText(val)
            if idx >= 0:
                e.setCurrentIndex(idx)
        else:
            e.setText(val)

    def _restore_settings(self):
        s = self._settings
        if not s:
            return
        if "ip" in s:
            self.ip_entry.setText(s["ip"])
        if "port" in s:
            self.nic_port_entry.setText(s["port"])
        if s.get("protocol") == "UDP":
            self.rb_udp.setChecked(True)
        else:
            self.rb_tcp.setChecked(True)
        fota = s.get("fota", {})
        for label, val in fota.items():
            if label in self.entries and self.entries[label] is not None:
                self._set_entry_value(self.entries[label], val)
        if "fp_url" in s:
            self.fp_url_entry.setText(s["fp_url"])
        if "fp_func" in s:
            self.fp_func_entry.setText(s["fp_func"])
        if "fp_fname" in s:
            self.fp_fname_entry.setText(s["fp_fname"])
        self.update_preview()

    # ── UI ────────────────────────────────────────────────────
    def build_ui(self):
        central = QtWidgets.QWidget()
        self.setCentralWidget(central)
        outer = QtWidgets.QVBoxLayout(central)
        outer.setContentsMargins(0, 0, 0, 0)
        outer.setSpacing(0)

        # ── Header banner ──
        header = QtWidgets.QFrame()
        header.setFixedHeight(78)
        header.setStyleSheet(f"""
            QFrame {{
                background: qlineargradient(x1:0, y1:0, x2:1, y2:0,
                    stop:0 {BRAND}, stop:1 {BRAND_HOVER});
            }}
        """)
        h_l = QtWidgets.QHBoxLayout(header)
        h_l.setContentsMargins(24, 10, 24, 10)

        title_box = QtWidgets.QVBoxLayout()
        t1 = Label("⚡  ORION Firmware Updater", font=lambda: qfont(FONT_FAMILY, 18, QtGui.QFont.Bold), color="white")
        title_box.addWidget(t1)
        t2 = Label("📡 EC200 / EC800  ·  DLMS/COSEM NIC over IPv6", font=FONT_SMALL, color="#eef3e2")
        title_box.addWidget(t2)
        h_l.addLayout(title_box)
        h_l.addStretch(1)

        self.status_pill = StatusPill("●  Idle", bg="#8a9c5c")
        h_l.addWidget(self.status_pill, alignment=Qt.AlignVCenter)
        outer.addWidget(header)

        # ── Scrollable body ──
        scroll = QtWidgets.QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setStyleSheet("QScrollArea { border: none; background: transparent; }")
        body = QtWidgets.QWidget()
        body.setStyleSheet(f"background-color: {BG_MAIN};")
        body_l = QtWidgets.QVBoxLayout(body)
        body_l.setContentsMargins(16, 16, 16, 16)
        body_l.setSpacing(12)
        scroll.setWidget(body)
        outer.addWidget(scroll, 1)

        # ── Firmware version row ──
        fw_card = Card()
        fw_l = QtWidgets.QHBoxLayout(fw_card)
        fw_l.setContentsMargins(16, 14, 16, 14)
        fw_l.addWidget(Label("🔧  Current Firmware Version", font=FONT_LABEL))
        self.version_var = StringVar("Not Fetched")
        self.version_label = Label(textvariable=self.version_var,
                                    font=lambda: qfont(FONT_FAMILY, 16, QtGui.QFont.Bold),
                                    color=BRAND_HOVER)
        fw_l.addWidget(self.version_label)
        fw_l.addStretch(1)

        self.get_version_btn = Button("Get Version", fg_color=SLATE, hover_color=SLATE_HOVER, width=110)
        self.get_version_btn.clicked.connect(self.get_version)
        fw_l.addWidget(self.get_version_btn)
        self._freezable.append(self.get_version_btn)

        self.nic_details_btn = Button("ℹ️  Get NIC Details", fg_color=INFO, hover_color=INFO_HOVER, width=160)
        self.nic_details_btn.clicked.connect(self.get_nic_details)
        fw_l.addWidget(self.nic_details_btn)
        self._freezable.append(self.nic_details_btn)

        self.nic_reset_btn = Button("🔄  NIC Reset", fg_color=WARNING, hover_color=WARNING_HOVER, width=130)
        self.nic_reset_btn.clicked.connect(self.send_nic_reset)
        fw_l.addWidget(self.nic_reset_btn)
        self._freezable.append(self.nic_reset_btn)

        self.nic_fw_btn = Button("📟  Get NIC Firmware", fg_color=INFO, hover_color=INFO_HOVER, width=180)
        self.nic_fw_btn.clicked.connect(self.get_nic_firmware)
        fw_l.addWidget(self.nic_fw_btn)
        self._freezable.append(self.nic_fw_btn)

        body_l.addWidget(fw_card)

        # ── Device config ──
        dev_card = Card()
        dev_l = QtWidgets.QGridLayout(dev_card)
        dev_l.setContentsMargins(16, 14, 16, 14)
        dev_l.setVerticalSpacing(10)
        dev_l.setHorizontalSpacing(8)

        dev_l.addWidget(Label("📶  Protocol", font=FONT_LABEL), 0, 0)
        proto_row = QtWidgets.QHBoxLayout()
        self.rb_tcp = QtWidgets.QRadioButton("TCP")
        self.rb_udp = QtWidgets.QRadioButton("UDP")
        self.rb_tcp.setChecked(True)
        for rb in (self.rb_tcp, self.rb_udp):
            rb.setFont(FONT_BODY())
            rb.setStyleSheet(f"QRadioButton::indicator:checked {{ background-color: {BRAND}; }}")
        proto_row.addWidget(self.rb_tcp)
        proto_row.addWidget(self.rb_udp)
        proto_row.addStretch(1)
        dev_l.addLayout(proto_row, 0, 1, 1, 3)
        self._freezable += [self.rb_tcp, self.rb_udp]

        dev_l.addWidget(Label("🌐  NIC IPv6 Address", font=FONT_LABEL), 1, 0)
        self.ip_entry = LineEdit("2401:4900:984a:3399::2")
        dev_l.addWidget(self.ip_entry, 1, 1)
        self._freezable.append(self.ip_entry)

        dev_l.addWidget(Label("Port", font=FONT_LABEL), 1, 2)
        self.nic_port_entry = LineEdit("4059", width=90)
        dev_l.addWidget(self.nic_port_entry, 1, 3)
        self._freezable.append(self.nic_port_entry)

        self.ping_btn_top = Button("🔔  Ping", fg_color=SLATE, hover_color=SLATE_HOVER, width=100)
        self.ping_btn_top.clicked.connect(self.open_ping_popup)
        dev_l.addWidget(self.ping_btn_top, 1, 4)
        self._freezable.append(self.ping_btn_top)
        dev_l.setColumnStretch(1, 1)
        body_l.addWidget(dev_card)

        # ── FOTA config (2-column layout) ───────────────────────────────
        fota_card = Card()
        fota_l = QtWidgets.QGridLayout(fota_card)
        fota_l.setContentsMargins(16, 14, 16, 14)
        fota_l.setVerticalSpacing(8)
        fota_l.setHorizontalSpacing(6)

        fota_l.addWidget(Label("🛰️  FOTA Command Parameters", font=FONT_H2), 0, 0, 1, 4)

        self.entries = {}

        # ---------------- Left Side ----------------
        left_col = [
            ("User", "0"),
            ("Password", "0"),
            ("Server IP", "0"),
            ("File Port", "0")
        ]

        for row, (label, default) in enumerate(left_col, start=1):

            lbl = Label(
                label,
                font=lambda: qfont(FONT_FAMILY, 10, QtGui.QFont.Medium)
            )
            fota_l.addWidget(lbl, row, 0)

            entry = LineEdit(default)
            entry.setSizePolicy(
                QtWidgets.QSizePolicy.Expanding,
                QtWidgets.QSizePolicy.Fixed
            )
            entry.textChanged.connect(lambda _t: self.update_preview())

            fota_l.addWidget(entry, row, 1)

            self.entries[label] = entry
            self._freezable.append(entry)

        # FOTA Mode
        lbl = Label(
            "FOTA Mode",
            font=lambda: qfont(FONT_FAMILY, 10, QtGui.QFont.Medium)
        )
        fota_l.addWidget(lbl, 5, 0)

        mode_combo = ComboBox(
            values=[
                "0 - Full FOTA",
                "1 - Delta FOTA"
            ]
        )
        mode_combo.setSizePolicy(
            QtWidgets.QSizePolicy.Expanding,
            QtWidgets.QSizePolicy.Fixed
        )
        mode_combo.currentTextChanged.connect(lambda _t: self.update_preview())

        fota_l.addWidget(mode_combo, 5, 1)

        self.entries["FOTA Mode"] = mode_combo
        self._freezable.append(mode_combo)

        # ---------------- Right Side ----------------
        _default_url = "https://ties-nancy-mary-dos.trycloudflare.com"
        _default_func = "getFile"
        _default_fname = "Full_FOTA_13X.pac"

        # File URL
        lbl = Label(
            "File URL",
            font=lambda: qfont(FONT_FAMILY, 10, QtGui.QFont.Medium)
        )
        fota_l.addWidget(lbl, 1, 2)

        self.fp_url_entry = LineEdit(_default_url)
        self.fp_url_entry.setPlaceholderText("https://example.com")
        self.fp_url_entry.setSizePolicy(
            QtWidgets.QSizePolicy.Expanding,
            QtWidgets.QSizePolicy.Fixed
        )
        self.fp_url_entry.textChanged.connect(lambda _t: self.update_preview())
        fota_l.addWidget(self.fp_url_entry, 1, 3)
        self._freezable.append(self.fp_url_entry)

        # Function
        lbl = Label(
            "Function",
            font=lambda: qfont(FONT_FAMILY, 10, QtGui.QFont.Medium)
        )
        fota_l.addWidget(lbl, 2, 2)

        self.fp_func_entry = LineEdit(_default_func)
        self.fp_func_entry.setPlaceholderText("getFile")
        self.fp_func_entry.setSizePolicy(
            QtWidgets.QSizePolicy.Expanding,
            QtWidgets.QSizePolicy.Fixed
        )
        self.fp_func_entry.textChanged.connect(lambda _t: self.update_preview())
        fota_l.addWidget(self.fp_func_entry, 2, 3)
        self._freezable.append(self.fp_func_entry)

        # File Name
        lbl = Label(
            "File Name",
            font=lambda: qfont(FONT_FAMILY, 10, QtGui.QFont.Medium)
        )
        fota_l.addWidget(lbl, 3, 2)

        self.fp_fname_entry = LineEdit(_default_fname)
        self.fp_fname_entry.setPlaceholderText("firmware.pac")
        self.fp_fname_entry.setSizePolicy(
            QtWidgets.QSizePolicy.Expanding,
            QtWidgets.QSizePolicy.Fixed
        )
        self.fp_fname_entry.textChanged.connect(lambda _t: self.update_preview())
        fota_l.addWidget(self.fp_fname_entry, 3, 3)
        self._freezable.append(self.fp_fname_entry)

        self.entries["File Path"] = None

        # ---------------- Layout Stretch ----------------

        # Small width for labels
        fota_l.setColumnMinimumWidth(0, 80)
        fota_l.setColumnMinimumWidth(2, 90)

        # Large width for input fields
        fota_l.setColumnStretch(0, 0)
        fota_l.setColumnStretch(1, 4)
        fota_l.setColumnStretch(2, 0)
        fota_l.setColumnStretch(3, 6)

        body_l.addWidget(fota_card)

        # ── Command preview ──
        preview_card = Card()
        prev_l = QtWidgets.QVBoxLayout(preview_card)
        prev_l.setContentsMargins(16, 12, 16, 12)
        prev_l.addWidget(Label("👁️  Command Preview", font=FONT_LABEL))
        self.command_var = StringVar()
        self.command_preview = LineEdit(mono=True)
        self.command_preview.setReadOnly(True)
        self.command_var.changed.connect(self.command_preview.setText)
        prev_l.addWidget(self.command_preview)
        body_l.addWidget(preview_card)
        self.update_preview()

        # ── Action buttons ──
        action_card = Card()
        act_l = QtWidgets.QHBoxLayout(action_card)
        act_l.setContentsMargins(16, 14, 16, 14)

        self.test_btn = Button("🔗  Test Connection", fg_color=INFO, hover_color=INFO_HOVER, width=170)
        self.test_btn.clicked.connect(self.test_connection)
        act_l.addWidget(self.test_btn)
        self._freezable.append(self.test_btn)

        self.fota_btn = Button("🚀  Send FOTA", fg_color=BRAND, hover_color=BRAND_HOVER,
                                width=160, height=40, font=lambda: qfont(FONT_FAMILY, 12, QtGui.QFont.Bold))
        self.fota_btn.clicked.connect(self.send_fota)
        act_l.addWidget(self.fota_btn)
        self._freezable.append(self.fota_btn)

        self.reset_btn = Button("♻️  Reset", fg_color=SLATE, hover_color=SLATE_HOVER, width=110)
        self.reset_btn.clicked.connect(self.reset_all)
        act_l.addWidget(self.reset_btn)
        self._freezable.append(self.reset_btn)

        self.ping_btn_bottom = Button("🔔  Ping", fg_color=SLATE, hover_color=SLATE_HOVER, width=100)
        self.ping_btn_bottom.clicked.connect(self.open_ping_popup)
        act_l.addWidget(self.ping_btn_bottom)
        self._freezable.append(self.ping_btn_bottom)

        act_l.addStretch(1)
        body_l.addWidget(action_card)

        # ── Log area ──
        log_card = Card()
        log_l = QtWidgets.QVBoxLayout(log_card)
        log_l.setContentsMargins(16, 12, 16, 14)
        log_header = QtWidgets.QHBoxLayout()
        log_header.addWidget(Label("📝  Logs", font=FONT_LABEL))
        log_header.addStretch(1)
        self.clear_log_btn = Button("Clear Logs", fg_color=SLATE, hover_color=SLATE_HOVER, width=100, height=28)
        self.clear_log_btn.clicked.connect(lambda: self.log_box.clear_all())
        log_header.addWidget(self.clear_log_btn)
        self._freezable.append(self.clear_log_btn)
        log_l.addLayout(log_header)

        self.log_box = TextBox(mono=True, height=240, readonly=True)
        log_l.addWidget(self.log_box)
        body_l.addWidget(log_card, 1)

        # ── Status bar ──
        self.status_var = StringVar("Disconnected")
        status_bar = QtWidgets.QStatusBar()
        status_bar.setStyleSheet(f"QStatusBar {{ background-color: #e9ede3; color: #444; }}")
        self.status_bar_label = QtWidgets.QLabel("Disconnected")
        self.status_bar_label.setFont(FONT_SMALL())
        self.status_var.changed.connect(self.status_bar_label.setText)
        status_bar.addWidget(self.status_bar_label, 1)
        self.setStatusBar(status_bar)

    # ── helpers ──────────────────────────────────────────────
    def _get_file_path(self) -> str:
        url   = self.fp_url_entry.text().strip().rstrip("/")
        func  = self.fp_func_entry.text().strip().strip("/")
        fname = self.fp_fname_entry.text().strip()
        if func:
            return url + "/" + func + "/" + fname
        return url + "/" + fname

    def update_preview(self):
        mode_raw = self.entries["FOTA Mode"].currentText()
        mode_val = mode_raw.split(" ")[0]
        fname = self.fp_fname_entry.text().strip()

        if mode_val == "1":
            if fname.endswith(".pac") and not fname.endswith(".pack"):
                fname = fname[:-4] + ".pack"
        else:
            if fname.endswith(".pack"):
                fname = fname[:-5] + ".pac"

        url  = self.fp_url_entry.text().strip().rstrip("/")
        func = self.fp_func_entry.text().strip().strip("/")
        file_path = url + "/" + func + "/" + fname if func else url + "/" + fname

        cmd = (
            "*GPRS-FOTA,"
            + self.entries["User"].text() + ","
            + self.entries["Password"].text() + ","
            + self.entries["Server IP"].text() + ","
            + mode_val + ","
            + file_path + ","
            + self.entries["File Port"].text() + ",#"
        )
        self.command_var.set(cmd)

    def log(self, message):
        ts = datetime.now().strftime("%H:%M:%S")
        self.log_box.append_line("[" + ts + "] " + message + "\n")

    def _set_status_pill(self, text, color="#8a9c5c"):
        self.status_pill.set_status(text, color)

    def _get_ip_port(self):
        ip   = self.ip_entry.text().strip()
        port = int(self.nic_port_entry.text().strip() or 4059)
        return ip, port

    def open_ping_popup(self):
        dlg = PingDialog(self, default_ip=self.ip_entry.text().strip())
        dlg.exec_()

    # ── Freeze / unfreeze every control during FOTA ─────────────
    def _freeze_ui(self):
        for w in self._freezable:
            try:
                w.setEnabled(False)
            except Exception:
                pass

    def _unfreeze_ui(self):
        for w in self._freezable:
            try:
                w.setEnabled(True)
            except Exception:
                pass

    # ── Get NIC Firmware ────────────────────────────────
    def get_nic_firmware(self):
        if self._fw_btn_busy or self._fota_running:
            return
        self._fw_btn_busy = True
        self.nic_fw_btn.setEnabled(False)
        self.nic_fw_btn.setText("⏳  Reading …")
        ip, port = self._get_ip_port()
        self.log("=" * 60)
        self.log(f"📟  Getting NIC firmware from [{ip}]:{port} …")
        self.log("    Sending HDLC sequence: SNRM → AARQ → RLRQ → NIC Firmware → DISC")

        def _log_cb(msg):
            self.after(0, self.log, msg)

        def _done_cb(result, error_msg=None):
            self.after(0, self._on_nic_firmware_done, result, error_msg)

        run_nic_firmware_sequence(ip, port, _log_cb, _done_cb)

    def _on_nic_firmware_done(self, result, error_msg=None):
        self._fw_btn_busy = False
        self.nic_fw_btn.setEnabled(True)
        self.nic_fw_btn.setText("📟  Get NIC Firmware")

        if result is None:
            reason = error_msg or "Unknown error — check logs above."
            self.log(f"❌  NIC Firmware retrieval failed — {reason}")
            self.status_var.set("✘  NIC firmware read failed")
            mb.showerror("Could Not Retrieve Firmware Version",
                         "Could not retrieve the NIC firmware version.\n\n" + reason)
            return

        fw  = result.get("firmware") or "—"
        imei = result.get("imei")    or "—"
        self.log(f"✅  NIC Firmware: {fw}   |   IMEI: {imei}")
        self.status_var.set(f"✅  NIC FW: {fw}  |  IMEI: {imei}")
        NicFirmwareDialog(self, result, error_msg).exec_()

    # ── Generic raw AT-style command sender (used by Reset / Details) ──
    def _send_raw_gprs_command(self, cmd: str, label: str):
        ip, port = self._get_ip_port()
        payload = (cmd + "\r\n").encode("ascii")
        try:
            sa = resolve_ipv6_sockaddr(ip, port)
            self.after(0, self.log, f"{label}  Connecting to [{sa[0]}]:{sa[1]} …")
            with make_tcp_socket(timeout=10) as sock:
                sock.connect(sa)
                self.after(0, self.log, f"{label}  RAW → {payload!r}")
                sock.sendall(payload)
                self.after(0, self.log, f"{label}  Sent {len(payload)} bytes ✔")
                try:
                    resp = sock.recv(1024).decode(errors="replace").strip()
                except (socket.timeout, ConnectionResetError):
                    resp = ""
            if resp:
                self.after(0, self.log, f"{label}  NIC response: {resp}")
                return True, resp
            return True, "(no response received — command sent successfully)"
        except Exception as e:
            reason = type(e).__name__ + ": " + str(e)
            self.after(0, self.log, f"{label}  ❌ {reason}")
            return False, reason

    # ── NIC Reset (*GPRS-RESET) ─────────────────────────────────
    def send_nic_reset(self):
        if self._fota_running:
            return
        if not mb.askyesno("Confirm NIC Reset",
                            "This will send *GPRS-RESET# to the NIC and reset it.\n\n"
                            "Continue?"):
            return
        self.nic_reset_btn.setEnabled(False)
        self.nic_reset_btn.setText("⏳  Resetting …")
        self.log("=" * 60)
        self.log("🔄  Sending NIC Reset command …")

        def _worker():
            ok, resp = self._send_raw_gprs_command("*GPRS-RESET#", "RESET")
            self.after(0, self._on_nic_reset_done, ok, resp)

        threading.Thread(target=_worker, daemon=True).start()

    def _on_nic_reset_done(self, ok, resp):
        self.nic_reset_btn.setEnabled(True)
        self.nic_reset_btn.setText("🔄  NIC Reset")
        if ok:
            mb.showinfo("NIC Reset", "Command sent: *GPRS-RESET#\n\nResponse:\n" + resp)
        else:
            mb.showerror("NIC Reset Failed", "Could not send *GPRS-RESET#.\n\n" + resp)

    # ── Get NIC Details (*GPRS-INFO) ────────────────────────────
    def get_nic_details(self):
        if self._fota_running:
            return
        self.nic_details_btn.setEnabled(False)
        self.nic_details_btn.setText("⏳  Reading …")
        self.log("=" * 60)
        self.log("ℹ️  Requesting NIC details …")

        def _worker():
            ok, resp = self._send_raw_gprs_command("*GPRS-INFO#", "INFO")
            self.after(0, self._on_nic_details_done, ok, resp)

        threading.Thread(target=_worker, daemon=True).start()

    def _on_nic_details_done(self, ok, resp):
        self.nic_details_btn.setEnabled(True)
        self.nic_details_btn.setText("ℹ️  Get NIC Details")
        if ok:
            mb.showinfo("NIC Details", "Command sent: *GPRS-INFO#\n\nResponse:\n" + resp)
            fw_slice = resp[21:37].strip()   # 22nd–38th characters (1-indexed)
            if fw_slice:
                self.version_var.set(fw_slice)
        else:
            mb.showerror("Get NIC Details Failed", "Could not send *GPRS-INFO#.\n\n" + resp)

    # ── Reset ─────────────────────────────────────────────────
    def reset_all(self):
        if self._fota_running:
            self.log("⚠  Cannot reset while FOTA is in progress.")
            return
        self.log_box.clear_all()
        self.version_var.set("Not Fetched")
        self.status_var.set("Disconnected")
        defaults = {"User": "0", "Password": "0", "Server IP": "0", "File Port": "0"}
        for label, val in defaults.items():
            self.entries[label].setText(val)
        idx = self.entries["FOTA Mode"].findText("0 - Full FOTA")
        if idx >= 0:
            self.entries["FOTA Mode"].setCurrentIndex(idx)
        self.fp_url_entry.setText("https://ties-nancy-mary-dos.trycloudflare.com")
        self.fp_func_entry.setText("getFile")
        self.fp_fname_entry.setText("Full_FOTA_13X.pac")
        self.update_preview()
        self.fota_btn.set_colors(BRAND, BRAND_HOVER)
        self.fota_btn.setText("🚀  Send FOTA")
        self.fota_btn.setEnabled(True)
        self._set_status_pill("●  Idle", "#8a9c5c")
        self.log("♻️  Reset complete.")
        self._save_current_settings()

    # ── Get Version ──────────────────────────────────────────
    def get_version(self):
        if self._fota_running:
            return
        self.log("=" * 60)
        self.log("🚀  Get Firmware Version requested …")
        self.version_var.set("Checking …")
        threading.Thread(target=self._get_version_worker, daemon=True).start()

    def _get_version_worker(self):
        ip, port = self._get_ip_port()
        sock = None
        try:
            self.after(0, self.log, "🔎  Checking NIC is reachable …")
            ok, msg, sa = self._check_nic_reachable(ip, port)
            if not ok:
                self.after(0, self.log, "❌  NIC not reachable — " + msg)
                self.after(0, self.version_var.set, "Not Reachable")
                return
            self.after(0, self.log, "✅  " + msg)

            self.after(0, self.log, "Connecting to " + sa[0] + " port " + str(sa[1]) + " …")
            sock = make_tcp_socket(timeout=5)
            sock.connect(sa)
            self.after(0, self.log, "Sending *GPRS-APPVER …")
            sock.sendall(b"*GPRS-APPVER\r\n")
            resp = sock.recv(256).decode(errors="replace").strip()
            if resp:
                self.after(0, self.version_var.set, resp)
                self.after(0, self.log, "✅  Version: " + resp)
            else:
                self.after(0, self.version_var.set, "No Response")
                self.after(0, self.log, "⚠️  NIC closed connection with no response.")
        except socket.timeout:
            self.after(0, self.log, "❌  Get Version timed out waiting for NIC response.")
            self.after(0, self.version_var.set, "Timeout")
        except Exception as e:
            self.after(0, self.log, "❌  Get Version failed — " + type(e).__name__ + ": " + str(e))
            self.after(0, self.version_var.set, "Error")
        finally:
            if sock is not None:
                try:
                    sock.close()
                    self.after(0, self.log, "🔌  Socket closed.")
                except Exception:
                    pass

    # ── Test Connection ──────────────────────────────────────
    def test_connection(self):
        if self._fota_running:
            return
        ip, port = self._get_ip_port()
        proto = "TCP" if self.rb_tcp.isChecked() else "UDP"
        self.log("Testing " + proto + " → [" + ip + "]:" + str(port) + " …")
        threading.Thread(target=self._test_worker, daemon=True).start()

    def _test_worker(self):
        ip, port = self._get_ip_port()
        proto = "TCP" if self.rb_tcp.isChecked() else "UDP"
        try:
            sa = resolve_ipv6_sockaddr(ip, port)
            self.after(0, self.log, "Resolved: " + sa[0] + " port " + str(sa[1]))
            if proto == "TCP":
                with make_tcp_socket(timeout=5) as sock:
                    sock.connect(sa)
                    try:
                        sock.sendall(b"\r\n")
                        banner = sock.recv(128).decode(errors="replace").strip()
                        if banner:
                            self.after(0, self.log, "Banner: " + banner)
                    except socket.timeout:
                        pass
                self.after(0, self.status_var.set, "✔ TCP Connected → [" + ip + "]:" + str(port))
                self.after(0, self.log, "TCP connection successful ✔")
            else:
                with make_udp_socket(timeout=3) as sock:
                    sock.connect(sa)
                self.after(0, self.status_var.set, "✔ UDP ready → [" + ip + "]:" + str(port))
                self.after(0, self.log, "UDP socket OK ✔")
        except socket.gaierror as e:
            self.after(0, self.log, "Address resolution failed: " + str(e))
            self.after(0, self.status_var.set, "✘ Address error")
        except ConnectionRefusedError:
            self.after(0, self.log, "Connection refused — NIC not listening on port " + str(port))
            self.after(0, self.status_var.set, "✘ Connection refused")
        except socket.timeout:
            self.after(0, self.log, "Timed out — check NIC reachable from this machine")
            self.after(0, self.status_var.set, "✘ Timeout")
        except OSError as e:
            self.after(0, self.log, "Socket OS error: " + str(e))
            self.after(0, self.status_var.set, "✘ Socket error")

    # ── Validation helpers ──────────────────────────────────
    def _validate_url_format(self, file_path: str):
        try:
            parsed = urlparse(file_path)
        except Exception as e:
            return False, "URL could not be parsed: " + str(e)
        if parsed.scheme not in ("http", "https"):
            return False, "URL must start with http:// or https://"
        if not parsed.netloc:
            return False, "URL is missing a host (e.g. https://example.com/...)"
        if not parsed.path or parsed.path == "/":
            return False, "URL has no file path / filename."
        return True, "URL is well-formed."

    def _check_remote_file_exists(self, file_path: str, timeout: int = 8):
        req = urllib.request.Request(file_path, method="HEAD")
        try:
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                if 200 <= resp.status < 400:
                    return True, "File reachable (HTTP " + str(resp.status) + ")"
                return False, "Server returned HTTP " + str(resp.status)
        except urllib.error.HTTPError as e:
            if e.code == 405:
                try:
                    req2 = urllib.request.Request(
                        file_path, method="GET",
                        headers={"Range": "bytes=0-0"})
                    with urllib.request.urlopen(req2, timeout=timeout) as resp2:
                        if 200 <= resp2.status < 400:
                            return True, "File reachable (HTTP " + str(resp2.status) + ")"
                        return False, "Server returned HTTP " + str(resp2.status)
                except urllib.error.HTTPError as e2:
                    return False, "File not found on server (HTTP " + str(e2.code) + ")"
                except Exception as e2:
                    return False, "Could not reach file URL: " + type(e2).__name__ + ": " + str(e2)
            if e.code == 404:
                return False, "File not found on server (HTTP 404)"
            return False, "Server returned HTTP " + str(e.code)
        except urllib.error.URLError as e:
            return False, "Could not reach URL host: " + str(e.reason)
        except Exception as e:
            return False, "URL check failed: " + type(e).__name__ + ": " + str(e)

    def _check_nic_reachable(self, ip: str, port: int, timeout: int = 5):
        if not ip:
            return False, "NIC IP address is empty.", None
        try:
            sa = resolve_ipv6_sockaddr(ip, port)
        except socket.gaierror as e:
            return False, "Could not resolve NIC IPv6 address: " + str(e), None
        except Exception as e:
            return False, "Address resolution failed: " + type(e).__name__ + ": " + str(e), None

        sock = None
        try:
            sock = make_tcp_socket(timeout=timeout)
            sock.connect(sa)
            return True, "NIC reachable at [" + sa[0] + "]:" + str(sa[1]), sa
        except ConnectionRefusedError:
            return False, "Connection refused -- NIC not listening on port " + str(port), sa
        except socket.timeout:
            return False, "Timed out connecting to NIC -- check it's powered on and reachable", sa
        except OSError as e:
            return False, "Socket error while connecting to NIC: " + str(e), sa
        finally:
            if sock is not None:
                try:
                    sock.close()
                except Exception:
                    pass

    def _ping_test_ip(self, ip: str, count: int = 2):
        is_win = platform.system().lower() == "windows"
        is_v6  = ":" in ip
        if is_v6:
            candidates = (["ping", "-6", "-n", str(count), ip] if is_win else
                          [["ping6", "-c", str(count), ip], ["ping", "-6", "-c", str(count), ip]])
            if is_win:
                candidates = [candidates]
        else:
            flag = "-n" if is_win else "-c"
            candidates = [["ping", flag, str(count), ip]]

        last_err = None
        for cmd in candidates:
            try:
                proc = subprocess.run(cmd, capture_output=True, text=True, timeout=6 * count + 6)
            except FileNotFoundError as e:
                last_err = str(e)
                continue
            except subprocess.TimeoutExpired:
                return False, "Ping command timed out."
            output = (proc.stdout or "") + (proc.stderr or "")
            ll = output.lower()

            has_reply   = any(k in ll for k in ("ttl=", "time=", "bytes from", "time<1ms"))
            has_unreach = any(k in ll for k in ("destination net unreachable",
                                                 "destination host unreachable",
                                                 "network is unreachable"))

            if has_reply:
                return True, "Host responded to ping."

            if has_unreach or "100% packet loss" in ll or "100% loss" in ll:
                return False, "Host did not respond to ping (unreachable)."

            if proc.returncode == 0:
                return True, "Ping succeeded."
            return False, "Host did not respond to ping (unreachable)."

        return False, "No suitable ping command found on this system." + \
            (" (" + last_err + ")" if last_err else "")

    def _validate_file_extension(self) -> bool:
        mode_raw = self.entries["FOTA Mode"].currentText()
        mode_val = mode_raw.split(" ")[0]
        fname    = self.fp_fname_entry.text().strip()

        if mode_val == "0":
            if fname.endswith(".pack"):
                mb.showerror("Wrong File Extension",
                             "⚠️  You've selected a wrong file!\n\n"
                             "FOTA Mode 0 (Full FOTA) requires a  .pac  file,\n"
                             "but the File Name ends with  .pack\n\n"
                             "Please update the File Name and try again.")
                return False
            if not fname.endswith(".pac"):
                mb.showerror("Wrong File Extension",
                             "⚠️  You've selected a wrong file!\n\n"
                             "FOTA Mode 0 (Full FOTA) requires a  .pac  file,\n"
                             "but the File Name does not end with  .pac\n\n"
                             "Please update the File Name and try again.")
                return False
        elif mode_val == "1":
            if fname.endswith(".pac") and not fname.endswith(".pack"):
                mb.showerror("Wrong File Extension",
                             "⚠️  You've selected a wrong file!\n\n"
                             "FOTA Mode 1 (Delta FOTA) requires a  .pack  file,\n"
                             "but the File Name ends with  .pac\n\n"
                             "Please update the File Name and try again.")
                return False
            if not fname.endswith(".pack"):
                mb.showerror("Wrong File Extension",
                             "⚠️  You've selected a wrong file!\n\n"
                             "FOTA Mode 1 (Delta FOTA) requires a  .pack  file,\n"
                             "but the File Name does not end with  .pack\n\n"
                             "Please update the File Name and try again.")
                return False
        return True

    def _run_with_retries(self, step_index, step_name, attempt_fn, max_attempts=3):
        popup = self._progress_popup
        result = None
        for attempt in range(1, max_attempts + 1):
            if popup._cancelled:
                return result if result is not None else (False, "Cancelled")
            if attempt > 1:
                self.after(0, self.log,
                           "🔁  '" + step_name + "' failed — retry " +
                           str(attempt) + "/" + str(max_attempts) + " …")
                self.after(0, popup.set_step_status, step_index, "running",
                           "Retry " + str(attempt) + "/" + str(max_attempts) + " …")
                time.sleep(2)
            try:
                result = attempt_fn()
            except Exception as e:
                result = (False, type(e).__name__ + ": " + str(e))
            if result[0]:
                return result
            self.after(0, self.log,
                       "⚠️  '" + step_name + "' attempt " + str(attempt) + "/" +
                       str(max_attempts) + " failed — " + str(result[1]))
        return result

    def _on_fota_nic_busy(self, step_name, reason):
        self._fota_running = False
        self._unfreeze_ui()
        self._set_status_pill("✘  NIC busy", "#a33025")
        self.status_var.set("✘  FOTA failed — NIC busy at: " + step_name)
        self.log("❌  FOTA stopped after 3 attempts at: " + step_name + " — " + reason)
        self.fota_btn.set_colors(DANGER, DANGER_HOVER)
        self.fota_btn.setText("❌  Failed — Retry")
        self.fota_btn.setEnabled(True)
        mb.showerror("NIC Busy",
                     "The FOTA update could not proceed because the NIC "
                     "did not respond after 3 attempts at:\n\n"
                     "🔸  " + step_name + "\n\n"
                     "Last error:\n" + str(reason) + "\n\n"
                     "The NIC is likely busy or unreachable right now. "
                     "Please wait a moment and try again.")

    # ── Send FOTA — orchestrates the full step sequence ─────────
    def send_fota(self):
        if self._fota_running:
            return
        if not self._validate_file_extension():
            return

        cmd = self.command_var.get()
        self._fota_running = True
        self._freeze_ui()
        self.fota_btn.set_colors(WARNING, WARNING_HOVER)
        self.fota_btn.setText("⏳  Updating …")
        self.fota_btn.setEnabled(False)
        self._set_status_pill("⏳  FOTA running", "#e6791f")
        self.status_var.set("⏳  FOTA sequence starting …")
        self.log("=" * 60)
        self.log("🚀  Starting full FOTA sequence …")
        self.log("    CMD → " + cmd)

        self._progress_popup = FotaStepDialog(self, self.FOTA_STEPS)
        self._progress_popup.show()
        threading.Thread(target=self._fota_worker, args=(cmd,), daemon=True).start()

    def _fota_worker(self, cmd: str):
        ip, port  = self._get_ip_port()
        file_path = self._get_file_path()
        payload   = (cmd + "\r\n").encode("ascii")
        popup     = self._progress_popup
        steps     = self.FOTA_STEPS

        def set_step(i, status, detail=None):
            self.after(0, popup.set_step_status, i, status, detail)

        def stop_failed(i, reason):
            self.after(0, popup.mark_failed, i, reason)
            self.after(0, self._on_fota_failed, steps[i], reason)

        def cancelled_mid_run():
            if popup._cancelled:
                self.after(0, popup.mark_cancelled)
                self.after(0, self._on_fota_cancelled)
                return True
            return False

        current_step = 0
        try:
            # ── Step 1: URL format ──
            current_step = 0
            set_step(0, "running")
            if cancelled_mid_run():
                return
            ok, msg = self._validate_url_format(file_path)
            if not ok:
                set_step(0, "fail", msg)
                stop_failed(0, msg)
                return
            set_step(0, "ok", msg)

            # ── Step 2: NIC IPv6 address valid ──
            current_step = 1
            set_step(1, "running")
            if cancelled_mid_run():
                return
            ok, msg = validate_ipv6_format(ip)
            if not ok:
                set_step(1, "fail", msg)
                stop_failed(1, msg)
                return
            set_step(1, "ok", msg)

            # ── Step 3: firmware file accessible (advisory) ──
            current_step = 2
            set_step(2, "running")
            if cancelled_mid_run():
                return
            ok, msg = self._check_remote_file_exists(file_path)
            if not ok:
                set_step(2, "warn", msg + "  (continuing — this check is informational only)")
            else:
                set_step(2, "ok", msg)

            # ── Step 4: ping test (advisory) ──
            current_step = 3
            set_step(3, "running")
            if cancelled_mid_run():
                return
            ok, msg = self._ping_test_ip(ip)
            if not ok:
                set_step(3, "warn", msg + "  (continuing — ICMP may be blocked even though TCP works)")
            else:
                set_step(3, "ok", msg)

            # ── Step 5: read current firmware version (retried x3) ──
            current_step = 4
            set_step(4, "running")
            if cancelled_mid_run():
                return
            ok, msg, old_ver = self._run_with_retries(
                4, "Read current firmware version",
                lambda: self._read_firmware_version_step(ip, port))
            if not ok:
                set_step(4, "fail", msg)
                self.after(0, popup.mark_failed, 4, msg)
                self.after(0, self._on_fota_nic_busy, steps[4], msg)
                return
            set_step(4, "ok", "Current version: " + old_ver)

            # ── Step 6: send FOTA command (retried x3) ──
            current_step = 5
            set_step(5, "running")
            if cancelled_mid_run():
                return
            ok, msg = self._run_with_retries(
                5, "Send FOTA command",
                lambda: self._send_fota_command_step(ip, port, payload))
            if not ok:
                set_step(5, "fail", msg)
                self.after(0, popup.mark_failed, 5, msg)
                self.after(0, self._on_fota_nic_busy, steps[5], msg)
                return
            set_step(5, "ok", msg)

            # ── Step 7: wait 30s ──
            current_step = 6
            set_step(6, "running")
            for remaining in range(30, 0, -1):
                if popup._cancelled:
                    self.after(0, popup.mark_cancelled)
                    self.after(0, self._on_fota_cancelled)
                    return
                self.after(0, popup.update_wait_countdown, 6, remaining)
                time.sleep(1)
            set_step(6, "ok", "30s wait complete")

            # ── Step 8: reread firmware version (retried x3, advisory) ──
            current_step = 7
            set_step(7, "running")
            ok, msg, new_ver = self._run_with_retries(
                7, "Reread firmware version",
                lambda: self._reread_firmware_version_step(ip, port))
            if not ok:
                set_step(7, "warn", str(msg) + "  (command was sent — verify version manually)")
                new_ver = "Unknown"
            else:
                set_step(7, "ok", "New version: " + new_ver)

            self.after(0, popup.mark_all_complete, old_ver, new_ver)
            self.after(0, self._save_current_settings)
            self.after(0, self._on_fota_send_ok, old_ver, new_ver)

        except Exception as e:
            reason = type(e).__name__ + ": " + str(e)
            self.after(0, popup.mark_failed, current_step, reason)
            self.after(0, self._on_fota_failed, steps[current_step], reason)

    # ── Step implementations ──────────────────────────────────
    def _read_firmware_version_step(self, ip, port):
        ok, msg, result = hdlc_read_firmware_blocking(
            ip, port, lambda m: self.after(0, self.log, m))
        if not ok:
            return False, msg, None
        fw = result.get("firmware")
        self.after(0, self.log, "📥  Firmware version: " + fw)
        self.after(0, self.version_var.set, fw)
        return True, "OK", fw

    def _send_fota_command_step(self, ip, port, payload):
        try:
            sa = resolve_ipv6_sockaddr(ip, port)
            self.after(0, self.log, "Connecting to " + sa[0] + " port " + str(sa[1]) + " …")
            with make_tcp_socket(timeout=10) as sock:
                sock.connect(sa)
                self.after(0, self.log, "Connected. Sending payload …")
                self.after(0, self.log, "RAW → " + repr(payload))
                sock.sendall(payload)
                self.after(0, self.log, "Sent " + str(len(payload)) + " bytes ✔")
                try:
                    resp = sock.recv(512).decode(errors="replace").strip()
                    if resp:
                        self.after(0, self.log, "NIC response: " + resp)
                except (socket.timeout, ConnectionResetError):
                    self.after(0, self.log,
                               "No immediate response — NIC is downloading firmware (normal)")
            return True, "FOTA command sent ✔"

        except ConnectionResetError:
            self.after(0, self.log,
                       "ℹ️  Connection closed by NIC (expected — NIC is rebooting to apply firmware)")
            return True, "FOTA command sent (NIC closed connection — expected)"

        except Exception as e:
            reason = type(e).__name__ + ": " + str(e)
            return False, reason

    def _reread_firmware_version_step(self, ip, port):
        ok, msg, result = hdlc_read_firmware_blocking(
            ip, port, lambda m: self.after(0, self.log, m))
        if not ok:
            return False, msg, None
        fw = result.get("firmware")
        self.after(0, self.log, "📥  Post-update firmware version: " + fw)
        self.after(0, self.version_var.set, fw)
        return True, "OK", fw

    # ── Completion / failure / cancellation handlers ────────────
    def _on_fota_send_ok(self, old_ver, new_ver):
        self._fota_running = False
        self._unfreeze_ui()
        self._set_status_pill("✅  Idle", "#1e7e34")
        self.status_var.set("✅  FOTA complete — " + (old_ver or "?") + " → " + (new_ver or "?"))
        self.log("✅  FOTA sequence fully complete.")
        self.fota_btn.set_colors(BRAND, BRAND_HOVER)
        self.fota_btn.setText("🚀  Send FOTA")
        self.fota_btn.setEnabled(True)
        mb.showinfo("FOTA Update Complete",
                     "Firmware update finished successfully!\n\n"
                     "Previous version: " + (old_ver or "unknown") + "\n"
                     "New version: " + (new_ver or "unknown"))

    def _on_fota_failed(self, step_name, reason):
        self._fota_running = False
        self._unfreeze_ui()
        self._set_status_pill("✘  FOTA failed", "#a33025")
        self.status_var.set("✘  FOTA failed — " + step_name)
        self.log("❌  FOTA stopped at: " + step_name + " — " + reason)
        self.fota_btn.set_colors(DANGER, DANGER_HOVER)
        self.fota_btn.setText("❌  Failed — Retry")
        self.fota_btn.setEnabled(True)
        mb.showerror("FOTA Update Blocked",
                     "The FOTA update process was stopped at:\n\n"
                     "🔸  " + step_name + "\n\n"
                     "Reason:\n" + reason + "\n\n"
                     "Please resolve the issue and try again.")

    def _on_fota_cancelled(self):
        self._fota_running = False
        self._unfreeze_ui()
        self._set_status_pill("⚠  Cancelled", "#b8860b")
        self.status_var.set("⚠  FOTA cancelled by user")
        self.log("⚠  FOTA cancelled by user.")
        self.fota_btn.set_colors(WARNING, WARNING_HOVER)
        self.fota_btn.setText("🚀  Send FOTA")
        self.fota_btn.setEnabled(True)


def main():
    app = QtWidgets.QApplication(sys.argv)
    app.setStyle("Fusion")
    app.setFont(qfont(FONT_FAMILY, 10))
    win = OrionFOTAApp()
    win.show()
    sys.exit(app.exec_())


if __name__ == "__main__":
    main()