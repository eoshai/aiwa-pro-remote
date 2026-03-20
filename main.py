#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
╔══════════════════════════════════════════════════════════════════╗
║              AIWA Pro Remote - Controle Remoto Virtual           ║
║                   Para TV AIWA com Android TV                    ║
║                                                                  ║
║  Dependências:                                                   ║
║    pip install customtkinter pillow                              ║
║    + ADB Platform Tools no PATH                                  ║
║    + scrcpy instalado no PATH (para espelhamento)                ║
║                                                                  ║
║  TV IP: 192.168.0.159:5555                                       ║
╚══════════════════════════════════════════════════════════════════╝
"""

import customtkinter as ctk
import subprocess
import threading
import queue
import time
import os
import sys
import re
import tempfile
import json
import base64
import hashlib
import socket
from datetime import datetime
from PIL import Image, ImageTk
import tkinter as tk
from tkinter import messagebox

# Servidor web (Flask) — instalado automaticamente se ausente
try:
    from flask import Flask, request, jsonify, send_file, redirect, session, render_template_string
    FLASK_OK = True
except ImportError:
    FLASK_OK = False

# ─────────────────────────────────────────────────────────────────
#  CONFIGURAÇÕES GLOBAIS
# ─────────────────────────────────────────────────────────────────
TV_HOST          = "tv ip here"
TV_PORT          = # port here
TV_ADDRESS       = f"{TV_HOST}:{TV_PORT}"
ADB_TIMEOUT      = 8    # timeout padrão de comandos ADB (segundos)
PING_INTERVAL    = 5    # intervalo do monitor de conexão (segundos)
RECONNECT_DELAY  = 10   # espera antes de tentar reconectar (segundos)
STATS_INTERVAL   = 2    # intervalo de atualização de CPU/RAM (segundos)

# Servidor web
WEB_PORT     = 8080
WEB_PASSWORD = "your password here"
WEB_SESSION  = hashlib.sha256(WEB_PASSWORD.encode()).hexdigest()[:16]

# Status de conexão
STATUS_CONNECTED    = "connected"
STATUS_STANDBY      = "standby"
STATUS_DISCONNECTED = "disconnected"
STATUS_CONNECTING   = "connecting"
STATUS_ERROR        = "error"

ctk.set_appearance_mode("dark")
ctk.set_default_color_theme("dark-blue")


# ─────────────────────────────────────────────────────────────────
#  BACKEND ADB
# ─────────────────────────────────────────────────────────────────
class ADBBackend:
    """
    Toda comunicação ADB roda em threads daemon separadas.
    Resultados chegam a GUI via result_queue (thread-safe).
    """

    def __init__(self, result_queue: queue.Queue):
        self.address         = TV_ADDRESS
        self.result_queue    = result_queue
        self._status         = STATUS_DISCONNECTED
        self._lock           = threading.Lock()
        self._stop_event     = threading.Event()
        self._monitor_thread = None
        self._stats_thread   = None
        self._stats_active   = False

    # ── helpers ──────────────────────────────────────────────────

    def _run_adb(self, *args, timeout=ADB_TIMEOUT):
        """Executa adb e devolve (ok: bool, output: str)."""
        cmd = ["adb", "-s", self.address] + list(args)
        try:
            r = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
            return r.returncode == 0, (r.stdout + r.stderr).strip()
        except subprocess.TimeoutExpired:
            return False, "Timeout: TV nao respondeu"
        except FileNotFoundError:
            return False, "ADB nao encontrado - adicione ao PATH"
        except Exception as e:
            return False, str(e)

    def _post(self, event_type, data=None):
        self.result_queue.put({"type": event_type, "data": data, "time": datetime.now()})

    # ── conexao ──────────────────────────────────────────────────

    def connect(self):
        def _do():
            self._post("status", STATUS_CONNECTING)
            self._post("log", f"Conectando em {self.address}...")
            ok, out = self._run_adb("connect", self.address, timeout=12)
            if ok and ("connected" in out.lower() or "already connected" in out.lower()):
                with self._lock:
                    self._status = STATUS_CONNECTED
                self._post("status", STATUS_CONNECTED)
                self._post("log", f"Conectado: {out}")
                self._start_monitor()
            else:
                with self._lock:
                    self._status = STATUS_DISCONNECTED
                self._post("status", STATUS_DISCONNECTED)
                self._post("log", f"Falha: {out}")
        threading.Thread(target=_do, daemon=True, name="adb-connect").start()

    def disconnect(self):
        self._stop_event.set()
        self._stats_active = False
        threading.Thread(
            target=lambda: self._run_adb("disconnect", self.address),
            daemon=True
        ).start()
        with self._lock:
            self._status = STATUS_DISCONNECTED
        self._post("status", STATUS_DISCONNECTED)
        self._post("log", "Desconectado.")

    # ── monitor de conexao ────────────────────────────────────────

    def _start_monitor(self):
        self._stop_event.clear()
        if self._monitor_thread and self._monitor_thread.is_alive():
            return
        self._monitor_thread = threading.Thread(
            target=self._monitor_loop, daemon=True, name="adb-monitor"
        )
        self._monitor_thread.start()

    def _monitor_loop(self):
        while not self._stop_event.is_set():
            time.sleep(PING_INTERVAL)
            if self._stop_event.is_set():
                break
            self._check_tv_state()

    def _check_tv_state(self):
        ok, out = self._run_adb("shell", "dumpsys", "power")
        if not ok:
            with self._lock:
                self._status = STATUS_DISCONNECTED
            self._post("status", STATUS_DISCONNECTED)
            self._post("log", "TV sem resposta - tentando reconectar...")
            self._reconnect()
            return
        display_line = next(
            (l.strip() for l in out.splitlines()
             if "Display Power" in l or "mWakefulness" in l),
            ""
        )
        if "Awake" in display_line or "AWAKE" in out:
            ns = STATUS_CONNECTED
        elif "Asleep" in display_line or "ASLEEP" in out or "Dozing" in display_line:
            ns = STATUS_STANDBY
        else:
            ns = STATUS_CONNECTED
        with self._lock:
            if self._status != ns:
                self._status = ns
                self._post("status", ns)

    def _reconnect(self):
        def _do():
            time.sleep(RECONNECT_DELAY)
            if not self._stop_event.is_set():
                self._post("log", "Reconectando...")
                self.connect()
        threading.Thread(target=_do, daemon=True, name="adb-reconnect").start()

    # ── comandos basicos ─────────────────────────────────────────

    def send_keyevent(self, keycode):
        def _do():
            ok, out = self._run_adb("shell", "input", "keyevent", str(keycode))
            if not ok:
                self._post("log", f"x keyevent {keycode}: {out}")
            else:
                self._post("log", f"-> keyevent {keycode}")
        threading.Thread(target=_do, daemon=True).start()

    def send_shell(self, *args, label="cmd"):
        def _do():
            ok, out = self._run_adb("shell", *args)
            if not ok:
                self._post("log", f"x {label}: {out}")
            else:
                self._post("log", f"-> {label}" + (f": {out}" if out else ""))
        threading.Thread(target=_do, daemon=True).start()

    def send_text(self, text):
        safe = text.replace(" ", "%s").replace("'", "\\'")
        def _do():
            ok, out = self._run_adb("shell", "input", "text", safe)
            snippet = text[:30] + ("..." if len(text) > 30 else "")
            if not ok:
                self._post("log", f"x Texto: {out}")
            else:
                self._post("log", f"-> Texto: '{snippet}'")
        threading.Thread(target=_do, daemon=True).start()


    # ── notificacoes ─────────────────────────────────────────────
    # Backend: APK proprio (com.aiwa.remote.toast) instalado na TV.
    # Comando: am broadcast -n com.aiwa.remote.toast/.ToastReceiver
    # Fallback: cmd notification + statusbar expand (caso APK nao instalado)

    TOAST_PKG      = "com.aiwa.remote.toast"
    TOAST_RECEIVER = "com.aiwa.remote.toast/.ToastReceiver"
    TOAST_ACTION   = "com.aiwa.SHOW_TOAST"

    def _toast_apk_available(self) -> bool:
        """Verifica se o APK aiwa-toast esta instalado na TV."""
        ok, out = self._run_adb("shell", "pm", "list", "packages", self.TOAST_PKG)
        return ok and self.TOAST_PKG in out

    def notify_toast(self, message: str):
        """
        Metodo 1 - Toast flutuante curto (~2s).
        Usa o APK proprio aiwa-toast. Fallback: cmd notification.
        """
        def _do():
            snippet = message[:40] + ("..." if len(message) > 40 else "")
            if self._toast_apk_available():
                ok, out = self._run_adb(
                    "shell", "am", "broadcast",
                    "-a", self.TOAST_ACTION,
                    "-n", self.TOAST_RECEIVER,
                    "--es", "message", message,
                    "--ez", "long", "false"
                )
                if ok:
                    self._post("log", f"Toast: '{snippet}'")
                    return
            # Fallback
            ok, out = self._run_adb(
                "shell", "cmd", "notification", "post",
                "-S", "bigtext", "-t", message, "aiwa_toast", " "
            )
            self._run_adb("shell", "cmd", "statusbar", "expand-notifications")
            self._post("log", f"Toast (fallback): '{snippet}'" if ok else f"x Toast: {out[:60]}")
        threading.Thread(target=_do, daemon=True, name="adb-toast").start()

    def notify_system(self, title: str, body: str):
        """
        Metodo 2 - Toast longo (3.5s) com titulo exibido antes da mensagem.
        Usa o APK proprio aiwa-toast com duracao longa.
        """
        def _do():
            snippet = body[:40] + ("..." if len(body) > 40 else "")
            full_msg = f"{title}: {body}" if title else body
            if self._toast_apk_available():
                ok, out = self._run_adb(
                    "shell", "am", "broadcast",
                    "-a", self.TOAST_ACTION,
                    "-n", self.TOAST_RECEIVER,
                    "--es", "message", full_msg,
                    "--ez", "long", "true"
                )
                if ok:
                    self._post("log", f"Notif. sistema (toast longo): [{title}] {snippet}")
                    return
            # Fallback
            ok, out = self._run_adb(
                "shell", "cmd", "notification", "post",
                "-S", "bigtext", "-t", title, "aiwa_notif", body
            )
            self._run_adb("shell", "cmd", "statusbar", "expand-notifications")
            self._post("log", f"Notif. (fallback): [{title}] {snippet}" if ok else f"x Notif: {out[:60]}")
        threading.Thread(target=_do, daemon=True, name="adb-notif-sys").start()

    def notify_overlay(self, message: str):
        """
        Metodo 3 - Toast centralizado na tela (posicao middle).
        Usa o APK proprio com duracao longa e mensagem destacada.
        """
        def _do():
            snippet = message[:40] + ("..." if len(message) > 40 else "")
            if self._toast_apk_available():
                ok, out = self._run_adb(
                    "shell", "am", "broadcast",
                    "-a", self.TOAST_ACTION,
                    "-n", self.TOAST_RECEIVER,
                    "--es", "message", message,
                    "--ez", "long", "true"
                )
                if ok:
                    self._post("log", f"Overlay toast: '{snippet}'")
                    return
            # Fallback
            ok, out = self._run_adb(
                "shell", "am", "start",
                "--user", "0",
                "-a", "android.intent.action.SEND",
                "-t", "text/plain",
                "--es", "android.intent.extra.TEXT", message,
                "-f", "0x10008000"
            )
            self._post("log", f"Overlay (fallback): '{snippet}'" if ok else f"x Overlay: {out[:60]}")
        threading.Thread(target=_do, daemon=True, name="adb-overlay").start()

    def setup_termux(self):
        """Mantido por compatibilidade — nao faz nada na versao APK."""
        self._post("log", "APK aiwa-toast instalado — Termux nao necessario.")

    def launch_app(self, package, activity="", label="App"):
        def _do():
            if activity:
                ok, out = self._run_adb("shell", "am", "start", "-n",
                                        f"{package}/{activity}")
            else:
                ok, out = self._run_adb(
                    "shell", "monkey", "-p", package,
                    "-c", "android.intent.category.LAUNCHER", "1"
                )
            if not ok or "Error" in out:
                self._post("log", f"x {label}: {out}")
            else:
                self._post("log", f"-> Abrindo {label}...")
        threading.Thread(target=_do, daemon=True).start()

    def take_screenshot(self):
        def _do():
            self._post("log", "Capturando tela...")
            remote = "/sdcard/aiwa_ss.png"
            ok, out = self._run_adb("shell", "screencap", "-p", remote)
            if not ok:
                self._post("log", f"x screencap: {out}")
                return
            tmp = tempfile.mktemp(suffix=".png", prefix="aiwa_")
            ok, out = self._run_adb("pull", remote, tmp, timeout=15)
            self._run_adb("shell", "rm", remote)
            if not ok:
                self._post("log", f"x pull: {out}")
                return
            if os.path.exists(tmp) and os.path.getsize(tmp) > 0:
                self._post("screenshot", tmp)
                self._post("log", "Screenshot ok!")
            else:
                self._post("log", "x Arquivo vazio")
        threading.Thread(target=_do, daemon=True, name="adb-screenshot").start()

    # ── CPU / RAM em tempo real ───────────────────────────────────

    def start_stats(self):
        """Inicia coleta periodica de CPU e RAM."""
        if self._stats_active:
            return
        self._stats_active = True
        self._stats_thread = threading.Thread(
            target=self._stats_loop, daemon=True, name="adb-stats"
        )
        self._stats_thread.start()

    def stop_stats(self):
        self._stats_active = False

    def _stats_loop(self):
        while self._stats_active:
            self._fetch_stats()
            time.sleep(STATS_INTERVAL)

    def _fetch_stats(self):
        """
        Coleta CPU via /proc/stat (dois samples com 500ms de intervalo)
        e RAM via /proc/meminfo. Envia resultado para a GUI via fila.
        """
        # ── CPU: dois samples ──
        ok1, raw1 = self._run_adb("shell", "cat", "/proc/stat", timeout=5)
        if not ok1:
            return
        time.sleep(0.5)
        ok2, raw2 = self._run_adb("shell", "cat", "/proc/stat", timeout=5)
        if not ok2:
            return

        def parse_cpu(text):
            for line in text.splitlines():
                if line.startswith("cpu "):
                    vals = list(map(int, line.split()[1:]))
                    total = sum(vals)
                    idle  = vals[3] if len(vals) > 3 else 0
                    return total, idle
            return None, None

        t1, i1 = parse_cpu(raw1)
        t2, i2 = parse_cpu(raw2)
        cpu_pct = 0.0
        if t1 and t2 and (t2 - t1) > 0:
            cpu_pct = 100.0 * (1.0 - (i2 - i1) / (t2 - t1))
            cpu_pct = max(0.0, min(100.0, cpu_pct))

        # ── RAM via /proc/meminfo ──
        ok, meminfo = self._run_adb("shell", "cat", "/proc/meminfo", timeout=5)
        ram_total_mb = ram_used_mb = ram_pct = 0
        if ok:
            def get_kb(key):
                m = re.search(key + r":\s+(\d+)", meminfo)
                return int(m.group(1)) if m else 0
            total_kb = get_kb("MemTotal")
            avail_kb = get_kb("MemAvailable")
            if total_kb > 0:
                used_kb      = total_kb - avail_kb
                ram_total_mb = total_kb // 1024
                ram_used_mb  = used_kb  // 1024
                ram_pct      = 100.0 * used_kb / total_kb

        # ── Top 5 processos por CPU ──
        ok, top_raw = self._run_adb(
            "shell", "top", "-n", "1", "-d", "1", timeout=8
        )
        top_procs = []
        if ok:
            for line in top_raw.splitlines():
                parts = line.split()
                # formato tipico: PID USER PR NI VIRT RES SHR S %CPU %MEM TIME COMMAND
                if len(parts) >= 12 and parts[0].isdigit():
                    try:
                        cpu_val = float(parts[8].replace(",", "."))
                        name    = parts[11][:22]
                        top_procs.append((cpu_val, name))
                    except (ValueError, IndexError):
                        pass
            top_procs.sort(reverse=True)
            top_procs = top_procs[:5]

        self._post("stats", {
            "cpu_pct":      round(cpu_pct, 1),
            "ram_used_mb":  ram_used_mb,
            "ram_total_mb": ram_total_mb,
            "ram_pct":      round(ram_pct, 1),
            "top_procs":    top_procs,
        })


# ─────────────────────────────────────────────────────────────────
#  JANELA DE SCREENSHOT
# ─────────────────────────────────────────────────────────────────
class ScreenshotWindow(ctk.CTkToplevel):
    def __init__(self, parent, image_path):
        super().__init__(parent)
        self.title("Screenshot da TV")
        self.configure(fg_color="#0d0d0d")
        img = Image.open(image_path)
        img.thumbnail((900, 600), Image.LANCZOS)
        self._photo = ImageTk.PhotoImage(img)
        w, h = img.size
        self.geometry(f"{w+20}x{h+80}")
        self.resizable(True, True)
        tk.Label(self, image=self._photo, bg="#0d0d0d", bd=0).pack(padx=10, pady=10)
        ctk.CTkLabel(
            self, text=f"Capturado em {datetime.now().strftime('%d/%m/%Y %H:%M:%S')}",
            text_color="#555", font=("Consolas", 10)
        ).pack(pady=(0, 4))

        def save():
            from tkinter import filedialog
            import shutil
            dest = filedialog.asksaveasfilename(
                defaultextension=".png",
                filetypes=[("PNG", "*.png"), ("Todos", "*.*")],
                initialfile=f"aiwa_{datetime.now().strftime('%Y%m%d_%H%M%S')}.png"
            )
            if dest:
                shutil.copy(image_path, dest)
                messagebox.showinfo("Salvo", f"Salvo em:\n{dest}")

        ctk.CTkButton(
            self, text="Salvar", command=save,
            fg_color="#1a472a", hover_color="#2d7a4f"
        ).pack(pady=(0, 10))
        self.lift()
        self.focus_force()


# MiniBar removida - usando ctk.CTkProgressBar nativo



# ─────────────────────────────────────────────────────────────────
#  SERVIDOR WEB  (Flask — acesso pelo celular na rede local)
# ─────────────────────────────────────────────────────────────────
# HTML da interface web — responsivo, dark mode, otimizado para celular
WEB_HTML = """<!DOCTYPE html>
<html lang="pt-BR">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1, maximum-scale=1, user-scalable=no">
<title>AIWA Remote</title>
<style>
  *{box-sizing:border-box;margin:0;padding:0;-webkit-tap-highlight-color:transparent}
  body{background:#0d0d0f;color:#e8e8f0;font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',sans-serif;min-height:100vh;padding-bottom:30px}
  h3{font-size:10px;font-weight:600;color:#6e6e88;letter-spacing:.08em;padding:14px 16px 6px;text-transform:uppercase}
  .card{background:#18181f;border:1px solid #2a2a35;border-radius:14px;margin:6px 12px;padding:14px}
  .status-bar{background:#111116;padding:12px 16px;display:flex;align-items:center;gap:10px;position:sticky;top:0;z-index:10;border-bottom:1px solid #2a2a35}
  .status-bar .title{font-size:15px;font-weight:600;flex:1}
  .dot{width:10px;height:10px;border-radius:50%;flex-shrink:0}
  .dot.connected{background:#27ae60}
  .dot.standby{background:#f39c12}
  .dot.disconnected{background:#e74c3c}
  .dot.connecting{background:#4fc3f7}
  .status-txt{font-size:12px;color:#6e6e88}
  /* dpad */
  .dpad{display:grid;grid-template-columns:repeat(3,64px);grid-template-rows:repeat(3,64px);gap:6px;justify-content:center;margin:8px auto}
  .dpad-btn{background:#1e1e28;border:none;color:#e8e8f0;font-size:20px;border-radius:10px;cursor:pointer;transition:background .1s;display:flex;align-items:center;justify-content:center;width:64px;height:64px}
  .dpad-btn:active{background:#3a3a50}
  .dpad-ok{background:#4f8ef7;border-radius:50%;font-size:14px;font-weight:700;color:#fff}
  .dpad-ok:active{background:#3a7ae8}
  .dpad-empty{visibility:hidden}
  /* botoes genericos */
  .btn-row{display:flex;gap:8px;flex-wrap:wrap}
  .btn{flex:1;min-width:0;height:44px;border:none;border-radius:10px;background:#1c1c26;color:#e8e8f0;font-size:13px;font-weight:600;cursor:pointer;transition:background .1s;white-space:nowrap;overflow:hidden;text-overflow:ellipsis;padding:0 8px}
  .btn:active{filter:brightness(1.3)}
  .btn-home{background:#1a3a2a;color:#7fcfaf}
  .btn-power{background:#3a0a0a;color:#e74c3c}
  .btn-vol{background:#1c1c26;font-size:15px}
  /* apps grid */
  .apps-grid{display:grid;grid-template-columns:repeat(3,1fr);gap:8px}
  .app-btn{height:52px;border:none;border-radius:10px;font-size:12px;font-weight:700;color:#fff;cursor:pointer;transition:filter .1s}
  .app-btn:active{filter:brightness(1.3)}
  /* text input */
  .text-row{display:flex;gap:8px}
  .txt-input{flex:1;background:#0a0a0e;border:1px solid #2a2a35;border-radius:10px;color:#e8e8f0;font-size:14px;padding:10px 12px;outline:none}
  .txt-input:focus{border-color:#4f8ef7}
  .send-btn{background:#4f8ef7;border:none;border-radius:10px;color:#fff;font-size:13px;font-weight:700;padding:0 16px;cursor:pointer}
  /* notif */
  .notif-cards{display:grid;grid-template-columns:repeat(3,1fr);gap:8px}
  .notif-card{border-radius:10px;padding:10px 8px;border:1px solid}
  .notif-card.toast{background:#0f1f2a;border-color:#1a4060}
  .notif-card.system{background:#1a1a0f;border-color:#3a3a10}
  .notif-card.overlay{background:#1a0f1a;border-color:#3a1060}
  .notif-label{font-size:11px;font-weight:700;margin-bottom:6px}
  .notif-label.toast{color:#4fc3f7}
  .notif-label.system{color:#f9ca24}
  .notif-label.overlay{color:#a29bfe}
  .notif-btn{width:100%;height:32px;border:none;border-radius:7px;font-size:11px;font-weight:700;cursor:pointer}
  .notif-btn.toast{background:#1a3a52;color:#4fc3f7}
  .notif-btn.system{background:#2e2e10;color:#f9ca24}
  .notif-btn.overlay{background:#2a1040;color:#a29bfe}
  /* screenshot */
  #ss-img{width:100%;border-radius:10px;display:none;margin-top:10px}
  .ss-btn{width:100%;height:44px;background:#1a2a3a;border:none;border-radius:10px;color:#e8e8f0;font-size:13px;font-weight:700;cursor:pointer}
  /* toast feedback */
  #feedback{position:fixed;bottom:20px;left:50%;transform:translateX(-50%);background:#27ae60;color:#fff;padding:8px 20px;border-radius:99px;font-size:13px;font-weight:600;opacity:0;transition:opacity .3s;pointer-events:none;z-index:999;white-space:nowrap}
  /* login */
  .login-wrap{display:flex;align-items:center;justify-content:center;min-height:100vh;padding:20px}
  .login-box{background:#18181f;border:1px solid #2a2a35;border-radius:18px;padding:32px 24px;width:100%;max-width:340px}
  .login-title{font-size:22px;font-weight:700;margin-bottom:6px}
  .login-sub{font-size:13px;color:#6e6e88;margin-bottom:24px}
  .login-input{width:100%;background:#0a0a0e;border:1px solid #2a2a35;border-radius:10px;color:#e8e8f0;font-size:15px;padding:12px 14px;outline:none;margin-bottom:12px}
  .login-btn{width:100%;height:46px;background:#4f8ef7;border:none;border-radius:10px;color:#fff;font-size:15px;font-weight:700;cursor:pointer}
  .login-err{color:#e74c3c;font-size:13px;margin-top:8px;display:none}
</style>
</head>
<body>
{% if not logged_in %}
<div class="login-wrap">
  <div class="login-box">
    <div class="login-title">AIWA Remote</div>
    <div class="login-sub">Digite a senha para acessar</div>
    <form method="POST" action="/login">
      <input class="login-input" type="password" name="password" placeholder="Senha" autofocus>
      <button class="login-btn" type="submit">Entrar</button>
      {% if error %}<div class="login-err" style="display:block">Senha incorreta</div>{% endif %}
    </form>
  </div>
</div>
{% else %}
<div class="status-bar">
  <div class="dot {{ conn_status }}" id="dot"></div>
  <div class="title">AIWA Remote</div>
  <div class="status-txt" id="status-txt">{{ conn_label }}</div>
</div>

<h3>Navegacao</h3>
<div class="card">
  <div class="dpad">
    <div class="dpad-empty"></div>
    <button class="dpad-btn" onclick="key(19)">&#9650;</button>
    <div class="dpad-empty"></div>
    <button class="dpad-btn" onclick="key(21)">&#9664;</button>
    <button class="dpad-btn dpad-ok" onclick="key(66)">OK</button>
    <button class="dpad-btn" onclick="key(22)">&#9654;</button>
    <div class="dpad-empty"></div>
    <button class="dpad-btn" onclick="key(20)">&#9660;</button>
    <div class="dpad-empty"></div>
  </div>
</div>

<h3>Atalhos</h3>
<div class="card">
  <div class="btn-row">
    <button class="btn btn-home" onclick="key(3)">Home</button>
    <button class="btn" onclick="key(4)">Voltar</button>
    <button class="btn" onclick="key(82)">Menu</button>
    <button class="btn" onclick="cmd(\'settings\')">Config</button>
    <button class="btn btn-power" onclick="key(26)">Power</button>
  </div>
</div>

<h3>Volume</h3>
<div class="card">
  <div class="btn-row">
    <button class="btn btn-vol" onclick="key(24)">VOL +</button>
    <button class="btn btn-vol" onclick="key(164)">MUTE</button>
    <button class="btn btn-vol" onclick="key(25)">VOL -</button>
    <button class="btn btn-vol" onclick="key(166)">CH +</button>
    <button class="btn btn-vol" onclick="key(167)">CH -</button>
  </div>
</div>

<h3>Apps</h3>
<div class="card">
  <div class="apps-grid">
    <button class="app-btn" style="background:#FF0000" onclick="app(\'youtube\')">YouTube</button>
    <button class="app-btn" style="background:#E50914" onclick="app(\'netflix\')">Netflix</button>
    <button class="app-btn" style="background:#00A8E1" onclick="app(\'prime\')">Prime</button>
    <button class="app-btn" style="background:#003087" onclick="app(\'globoplay\')">Globoplay</button>
    <button class="app-btn" style="background:#1DB954" onclick="app(\'spotify\')">Spotify</button>
    <button class="app-btn" style="background:#003366" onclick="app(\'globo\')">Globo</button>
    <button class="app-btn" style="background:#01875f" onclick="app(\'playstore\')">Play Store</button>
    <button class="app-btn" style="background:#607D8B" onclick="app(\'files\')">Arquivos</button>
    <button class="app-btn" style="background:#455A64" onclick="cmd(\'settings\')">Config.</button>
  </div>
</div>

<h3>Texto</h3>
<div class="card">
  <div class="text-row" style="margin-bottom:8px">
    <input class="txt-input" id="txt" type="text" placeholder="Digite o texto...">
    <button class="send-btn" onclick="sendTxt()">Enviar</button>
  </div>
  <div class="btn-row">
    <button class="btn" onclick="key(67)">Backspace</button>
    <button class="btn" onclick="key(66)">Enter (TV)</button>
  </div>
</div>

<h3>Notificacoes</h3>
<div class="card">
  <input class="txt-input" id="notif-title" type="text" placeholder="Titulo (opcional)..." style="width:100%;margin-bottom:8px">
  <input class="txt-input" id="notif-msg" type="text" placeholder="Mensagem..." style="width:100%;margin-bottom:10px">
  <div class="notif-cards">
    <div class="notif-card toast">
      <div class="notif-label toast">Toast</div>
      <div style="font-size:10px;color:#6e6e88;margin-bottom:8px">Balao curto</div>
      <button class="notif-btn toast" onclick="notif(\'toast\')">Enviar</button>
    </div>
    <div class="notif-card system">
      <div class="notif-label system">Sistema</div>
      <div style="font-size:10px;color:#6e6e88;margin-bottom:8px">Toast longo</div>
      <button class="notif-btn system" onclick="notif(\'system\')">Enviar</button>
    </div>
    <div class="notif-card overlay">
      <div class="notif-label overlay">Overlay</div>
      <div style="font-size:10px;color:#6e6e88;margin-bottom:8px">Central</div>
      <button class="notif-btn overlay" onclick="notif(\'overlay\')">Enviar</button>
    </div>
  </div>
</div>

<h3>Screenshot</h3>
<div class="card">
  <button class="ss-btn" onclick="takeScreenshot()">Capturar tela da TV</button>
  <img id="ss-img" src="" alt="Screenshot">
</div>

<div id="feedback">OK</div>

<script>
const show = (msg, ok=true) => {
  const f = document.getElementById(\'feedback\');
  f.textContent = msg;
  f.style.background = ok ? \'#27ae60\' : \'#e74c3c\';
  f.style.opacity = \'1\';
  setTimeout(() => f.style.opacity = \'0\', 1500);
};

const post = async (url, data={}) => {
  try {
    const r = await fetch(url, {method:\'POST\',headers:{\'Content-Type\':\'application/json\'},body:JSON.stringify(data)});
    const j = await r.json();
    show(j.ok ? \'OK\' : (j.error || \'Erro\'), j.ok);
    return j;
  } catch(e) { show(\'Erro de conexao\', false); }
};

const key  = k  => post(\'/api/keyevent\', {keycode: k});
const app  = a  => post(\'/api/app\', {app: a});
const cmd  = c  => post(\'/api/cmd\', {cmd: c});

const sendTxt = () => {
  const t = document.getElementById(\'txt\').value.trim();
  if (!t) return;
  post(\'/api/text\', {text: t});
  document.getElementById(\'txt\').value = \'\';
};

const notif = type => {
  const title = document.getElementById(\'notif-title\').value.trim() || \'AIWA Remote\';
  const msg   = document.getElementById(\'notif-msg\').value.trim();
  if (!msg) { show(\'Digite a mensagem\', false); return; }
  post(\'/api/notify\', {type, title, message: msg});
};

const takeScreenshot = async () => {
  show(\'Capturando...\');
  const r = await fetch(\'/api/screenshot\');
  if (r.ok) {
    const blob = await r.blob();
    const url  = URL.createObjectURL(blob);
    const img  = document.getElementById(\'ss-img\');
    img.src = url;
    img.style.display = \'block\';
    show(\'Screenshot ok!\');
  } else { show(\'Falhou\', false); }
};

// Atualiza status a cada 5s
const updateStatus = async () => {
  try {
    const r = await fetch(\'/api/status\');
    const j = await r.json();
    const dot = document.getElementById(\'dot\');
    const txt = document.getElementById(\'status-txt\');
    dot.className = \'dot \' + j.status;
    const labels = {connected:\'Conectado\',standby:\'Standby\',disconnected:\'Desconectado\',connecting:\'Conectando...\',error:\'Erro\'};
    txt.textContent = labels[j.status] || j.status;
  } catch(e) {}
};
setInterval(updateStatus, 5000);

// Enter no campo de texto envia
document.getElementById(\'txt\').addEventListener(\'keydown\', e => { if(e.key===\'Enter\') sendTxt(); });
document.getElementById(\'notif-msg\').addEventListener(\'keydown\', e => { if(e.key===\'Enter\') notif(\'toast\'); });
</script>
{% endif %}
</body>
</html>"""


class WebServer:
    """
    Servidor Flask integrado que expoe a interface web do controle remoto.
    Roda em thread daemon separada para nao bloquear a GUI.
    Requer: pip install flask
    """

    APPS = {
        "youtube":   ("com.google.android.youtube.tv",
                      "com.google.android.apps.youtube.tv.activity.ShellActivity"),
        "netflix":   ("com.netflix.ninja", "com.netflix.ninja.MainActivity"),
        "prime":     ("com.amazon.amazonvideo.livingroom",
                      "com.amazon.amazonvideo.livingroom.MainActivity"),
        "globoplay": ("com.globo.globoplay", ""),
        "spotify":   ("com.spotify.tv.android",
                      "com.spotify.tv.android.SpotifyTVActivity"),
        "globo":     ("com.globo.android", ""),
        "playstore": ("com.android.vending", ""),
        "files":     ("com.google.android.documentsui", ""),
    }

    def __init__(self, adb: "ADBBackend"):
        self._adb   = adb
        self._app   = None
        self._thread = None
        self._running = False
        self._last_ss_path = None   # cache do ultimo screenshot

    def start(self):
        if not FLASK_OK:
            print("[WebServer] Flask nao instalado — rode: pip install flask")
            return
        self._running = True
        self._thread  = threading.Thread(
            target=self._run, daemon=True, name="web-server"
        )
        self._thread.start()

    def stop(self):
        self._running = False

    def _run(self):
        app = Flask(__name__)
        app.secret_key = WEB_SESSION

        # ── autenticacao ──────────────────────────────────────────
        def is_logged():
            return session.get("auth") == WEB_SESSION

        @app.route("/login", methods=["GET", "POST"])
        def login():
            error = False
            if request.method == "POST":
                if request.form.get("password") == WEB_PASSWORD:
                    session["auth"] = WEB_SESSION
                    return redirect("/")
                error = True
            return render_template_string(
                WEB_HTML, logged_in=False, error=error,
                conn_status="disconnected", conn_label=""
            )

        @app.route("/logout")
        def logout():
            session.clear()
            return redirect("/login")

        # ── pagina principal ──────────────────────────────────────
        @app.route("/")
        def index():
            if not is_logged():
                return redirect("/login")
            status = self._adb._status
            labels = {
                STATUS_CONNECTED:    "Conectado",
                STATUS_STANDBY:      "Standby",
                STATUS_DISCONNECTED: "Desconectado",
                STATUS_CONNECTING:   "Conectando...",
                STATUS_ERROR:        "Erro",
            }
            return render_template_string(
                WEB_HTML, logged_in=True, error=False,
                conn_status=status,
                conn_label=labels.get(status, status)
            )

        # ── API: status ───────────────────────────────────────────
        @app.route("/api/status")
        def api_status():
            if not is_logged():
                return jsonify({"error": "unauthorized"}), 401
            return jsonify({"status": self._adb._status})

        # ── API: keyevent ─────────────────────────────────────────
        @app.route("/api/keyevent", methods=["POST"])
        def api_keyevent():
            if not is_logged():
                return jsonify({"error": "unauthorized"}), 401
            keycode = request.json.get("keycode")
            if keycode is None:
                return jsonify({"ok": False, "error": "keycode missing"})
            self._adb.send_keyevent(int(keycode))
            return jsonify({"ok": True})

        # ── API: app launcher ─────────────────────────────────────
        @app.route("/api/app", methods=["POST"])
        def api_app():
            if not is_logged():
                return jsonify({"error": "unauthorized"}), 401
            app_key = request.json.get("app", "")
            if app_key not in self.APPS:
                return jsonify({"ok": False, "error": "app desconhecido"})
            pkg, act = self.APPS[app_key]
            self._adb.launch_app(pkg, act, label=app_key)
            return jsonify({"ok": True})

        # ── API: cmd generico ─────────────────────────────────────
        @app.route("/api/cmd", methods=["POST"])
        def api_cmd():
            if not is_logged():
                return jsonify({"error": "unauthorized"}), 401
            cmd_key = request.json.get("cmd", "")
            if cmd_key == "settings":
                self._adb.send_shell(
                    "am", "start", "-a", "android.settings.SETTINGS",
                    label="Configuracoes"
                )
            return jsonify({"ok": True})

        # ── API: texto ────────────────────────────────────────────
        @app.route("/api/text", methods=["POST"])
        def api_text():
            if not is_logged():
                return jsonify({"error": "unauthorized"}), 401
            text = request.json.get("text", "")
            if not text:
                return jsonify({"ok": False, "error": "texto vazio"})
            self._adb.send_text(text)
            return jsonify({"ok": True})

        # ── API: notificacoes ─────────────────────────────────────
        @app.route("/api/notify", methods=["POST"])
        def api_notify():
            if not is_logged():
                return jsonify({"error": "unauthorized"}), 401
            data    = request.json
            ntype   = data.get("type", "toast")
            title   = data.get("title", "AIWA Remote")
            message = data.get("message", "")
            if not message:
                return jsonify({"ok": False, "error": "mensagem vazia"})
            if ntype == "toast":
                self._adb.notify_toast(message)
            elif ntype == "system":
                self._adb.notify_system(title, message)
            elif ntype == "overlay":
                self._adb.notify_overlay(message)
            return jsonify({"ok": True})

        # ── API: screenshot ───────────────────────────────────────
        @app.route("/api/screenshot")
        def api_screenshot():
            if not is_logged():
                return jsonify({"error": "unauthorized"}), 401
            # Tira screenshot sincrono (bloqueia ate ter o arquivo)
            result = {"path": None, "done": threading.Event()}

            def _cb():
                remote = "/sdcard/aiwa_web_ss.png"
                ok, _  = self._adb._run_adb("shell", "screencap", "-p", remote)
                if not ok:
                    result["done"].set()
                    return
                tmp = tempfile.mktemp(suffix=".png", prefix="aiwa_web_")
                ok, _ = self._adb._run_adb("pull", remote, tmp, timeout=15)
                self._adb._run_adb("shell", "rm", remote)
                if ok and os.path.exists(tmp):
                    result["path"] = tmp
                result["done"].set()

            threading.Thread(target=_cb, daemon=True).start()
            result["done"].wait(timeout=20)

            if result["path"]:
                return send_file(result["path"], mimetype="image/png")
            return jsonify({"error": "screenshot falhou"}), 500

        # Suprime logs do Flask no terminal
        import logging
        log = logging.getLogger("werkzeug")
        log.setLevel(logging.ERROR)

        # Descobre o IP local para exibir no log
        try:
            s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            s.connect(("8.8.8.8", 80))
            local_ip = s.getsockname()[0]
            s.close()
        except Exception:
            local_ip = "localhost"

        print(f"\n{'='*50}")
        print(f"  AIWA Remote Web  →  http://{local_ip}:{WEB_PORT}")
        print(f"  Senha: {WEB_PASSWORD}")
        print(f"{'='*50}\n")

        app.run(host="0.0.0.0", port=WEB_PORT, debug=False, use_reloader=False)


# ─────────────────────────────────────────────────────────────────
#  APLICACAO PRINCIPAL
# ─────────────────────────────────────────────────────────────────
class AIWAProRemote(ctk.CTk):

    C_BG        = "#0d0d0f"
    C_PANEL     = "#111116"
    C_CARD      = "#18181f"
    C_BORDER    = "#2a2a35"
    C_ACCENT    = "#4f8ef7"
    C_DPAD      = "#1e1e28"
    C_DPAD_HOV  = "#2a2a38"
    C_BTN       = "#1c1c26"
    C_BTN_HOV   = "#28283a"
    C_OK        = "#4f8ef7"
    C_OK_HOV    = "#3a7ae8"
    C_TEXT      = "#e8e8f0"
    C_TEXT_DIM  = "#6e6e88"
    C_GREEN     = "#27ae60"
    C_YELLOW    = "#f39c12"
    C_RED       = "#e74c3c"
    C_LOG_BG    = "#0a0a0e"

    def __init__(self):
        super().__init__()
        self.title("AIWA Pro Remote")
        self.geometry("700x1020")
        self.minsize(660, 900)
        self.configure(fg_color=self.C_BG)
        self.resizable(True, True)

        self._queue       = queue.Queue()
        self._adb         = ADBBackend(self._queue)
        self._conn_status = STATUS_DISCONNECTED
        self._scrcpy_proc = None    # processo scrcpy ativo
        self._stats_on    = False   # monitor ligado?

        self._build_ui()
        self.after(500, self._adb.connect)
        self._poll_queue()
        self.after(1500, self._check_termux)  # verifica Termux ao iniciar

        # Inicia servidor web em background
        self._web = WebServer(self._adb)
        if FLASK_OK:
            self._web.start()
            self.after(200, self._update_web_indicator)

    # ─────────────────────────────────────────────────────────────
    #  BUILD UI
    # ─────────────────────────────────────────────────────────────
    def _build_ui(self):
        self._main = ctk.CTkScrollableFrame(
            self, fg_color=self.C_BG,
            scrollbar_button_color=self.C_BORDER,
            scrollbar_button_hover_color=self.C_ACCENT
        )
        self._main.pack(fill="both", expand=True)
        self._main.grid_columnconfigure(0, weight=1)

        row = 0
        row = self._build_header(row)
        row = self._build_nav_and_volume(row)
        row = self._build_quick_actions(row)
        row = self._build_text_input(row)
        row = self._build_app_launcher(row)
        row = self._build_capture_and_scrcpy(row)   # screenshot + scrcpy
        row = self._build_stats_panel(row)           # CPU / RAM ao vivo
        row = self._build_notification_panel(row)    # notificacoes
        row = self._build_log(row)

    # ─────── Cabecalho ───────────────────────────────────────────
    def _build_header(self, row):
        hdr = ctk.CTkFrame(self._main, fg_color=self.C_PANEL,
                           corner_radius=0, border_width=0)
        hdr.grid(row=row, column=0, sticky="ew", pady=(0, 2))
        hdr.grid_columnconfigure(1, weight=1)

        ctk.CTkLabel(hdr, text="TV", font=("Segoe UI", 22, "bold"),
                     text_color=self.C_ACCENT
                     ).grid(row=0, column=0, padx=(18, 8), pady=14)

        tf = ctk.CTkFrame(hdr, fg_color="transparent")
        tf.grid(row=0, column=1, sticky="w")
        ctk.CTkLabel(tf, text="AIWA Pro Remote",
                     font=("Segoe UI", 18, "bold"),
                     text_color=self.C_TEXT).pack(anchor="w")
        ctk.CTkLabel(tf, text=TV_ADDRESS,
                     font=("Consolas", 10),
                     text_color=self.C_TEXT_DIM).pack(anchor="w")

        right = ctk.CTkFrame(hdr, fg_color="transparent")
        right.grid(row=0, column=2, padx=14, pady=14, sticky="e")

        self._status_dot = ctk.CTkLabel(
            right, text="o", font=("Segoe UI", 18, "bold"),
            text_color=self.C_RED)
        self._status_dot.pack(side="left", padx=(0, 4))
        self._status_label = ctk.CTkLabel(
            right, text="Desconectado",
            font=("Segoe UI", 11), text_color=self.C_TEXT_DIM)
        self._status_label.pack(side="left", padx=(0, 10))
        ctk.CTkButton(
            right, text="Reconectar", width=110, height=30,
            font=("Segoe UI", 11), fg_color=self.C_BTN,
            hover_color=self.C_BTN_HOV, corner_radius=6,
            command=self._adb.connect
        ).pack(side="left")

        # Indicador do servidor web — linha abaixo do cabecalho
        web_bar = ctk.CTkFrame(hdr, fg_color="#0a0f0a", corner_radius=0)
        web_bar.grid(row=1, column=0, columnspan=3, sticky="ew", padx=0, pady=0)
        self._web_lbl = ctk.CTkLabel(
            web_bar,
            text="Servidor web: iniciando..." if FLASK_OK else "Servidor web: instale flask  (pip install flask)",
            font=("Segoe UI", 9),
            text_color=self.C_TEXT_DIM
        )
        self._web_lbl.pack(side="left", padx=14, pady=4)

        if FLASK_OK:
            import webbrowser
            self._web_open_btn = ctk.CTkButton(
                web_bar, text="Abrir no navegador", width=140, height=22,
                fg_color="transparent", hover_color=self.C_BTN,
                font=("Segoe UI", 9), text_color="#4fc3f7",
                command=lambda: webbrowser.open(f"http://localhost:{WEB_PORT}")
            )
            self._web_open_btn.pack(side="right", padx=8, pady=4)

        return row + 1

    # ─────── D-Pad + Volume ──────────────────────────────────────
    def _build_nav_and_volume(self, row):
        outer = ctk.CTkFrame(self._main, fg_color=self.C_PANEL, corner_radius=12)
        outer.grid(row=row, column=0, sticky="ew", padx=12, pady=6)
        outer.grid_columnconfigure(0, weight=3)
        outer.grid_columnconfigure(1, weight=1)

        ctk.CTkLabel(outer, text="NAVEGACAO",
                     font=("Segoe UI", 9, "bold"),
                     text_color=self.C_TEXT_DIM
                     ).grid(row=0, column=0, sticky="w", padx=16, pady=(12, 0))
        ctk.CTkLabel(outer, text="VOLUME",
                     font=("Segoe UI", 9, "bold"),
                     text_color=self.C_TEXT_DIM
                     ).grid(row=0, column=1, sticky="w", padx=8, pady=(12, 0))

        dpad = ctk.CTkFrame(outer, fg_color=self.C_DPAD, corner_radius=80)
        dpad.grid(row=1, column=0, padx=16, pady=(4, 16))

        bcfg = dict(width=64, height=64, corner_radius=8,
                    fg_color=self.C_DPAD, hover_color=self.C_DPAD_HOV,
                    font=("Segoe UI", 18, "bold"), text_color=self.C_TEXT)
        okcfg = dict(width=68, height=68, corner_radius=34,
                     fg_color=self.C_OK, hover_color=self.C_OK_HOV,
                     font=("Segoe UI", 12, "bold"), text_color="#fff")

        ctk.CTkButton(dpad, text="^", **bcfg,
                      command=lambda: self._adb.send_keyevent(19)
                      ).grid(row=0, column=1, padx=4, pady=4)
        ctk.CTkButton(dpad, text="<", **bcfg,
                      command=lambda: self._adb.send_keyevent(21)
                      ).grid(row=1, column=0, padx=4, pady=4)
        ctk.CTkButton(dpad, text="OK", **okcfg,
                      command=lambda: self._adb.send_keyevent(66)
                      ).grid(row=1, column=1, padx=4, pady=4)
        ctk.CTkButton(dpad, text=">", **bcfg,
                      command=lambda: self._adb.send_keyevent(22)
                      ).grid(row=1, column=2, padx=4, pady=4)
        ctk.CTkButton(dpad, text="v", **bcfg,
                      command=lambda: self._adb.send_keyevent(20)
                      ).grid(row=2, column=1, padx=4, pady=4)

        vol = ctk.CTkFrame(outer, fg_color="transparent")
        vol.grid(row=1, column=1, padx=8, pady=(4, 16), sticky="n")
        vcfg = dict(width=72, height=52, corner_radius=8,
                    fg_color=self.C_BTN, hover_color=self.C_BTN_HOV,
                    font=("Segoe UI", 13, "bold"), text_color=self.C_TEXT)

        ctk.CTkButton(vol, text="VOL +", **vcfg,
                      command=lambda: self._adb.send_keyevent(24)).pack(pady=3)
        ctk.CTkButton(vol, text="MUTE", width=72, height=36, corner_radius=8,
                      fg_color=self.C_BTN, hover_color="#3a2a0a",
                      font=("Segoe UI", 10, "bold"), text_color=self.C_YELLOW,
                      command=lambda: self._adb.send_keyevent(164)).pack(pady=3)
        ctk.CTkButton(vol, text="VOL -", **vcfg,
                      command=lambda: self._adb.send_keyevent(25)).pack(pady=3)

        ctk.CTkLabel(vol, text="CH", font=("Segoe UI", 8, "bold"),
                     text_color=self.C_TEXT_DIM).pack(pady=(8, 0))
        ctk.CTkButton(vol, text="CH +", **vcfg,
                      command=lambda: self._adb.send_keyevent(166)).pack(pady=3)
        ctk.CTkButton(vol, text="CH -", **vcfg,
                      command=lambda: self._adb.send_keyevent(167)).pack(pady=3)
        return row + 1

    # ─────── Atalhos rapidos ──────────────────────────────────────
    def _build_quick_actions(self, row):
        card = self._section_card(row, "ATALHOS RAPIDOS")
        actions = [
            ("Home",    "#1a3a2a", "#2d7a4f",  lambda: self._adb.send_keyevent(3)),
            ("Voltar",  self.C_BTN, self.C_BTN_HOV, lambda: self._adb.send_keyevent(4)),
            ("Config",  self.C_BTN, self.C_BTN_HOV,
             lambda: self._adb.send_shell("am", "start", "-a",
                                          "android.settings.SETTINGS",
                                          label="Configuracoes")),
            ("Menu",    self.C_BTN, self.C_BTN_HOV, lambda: self._adb.send_keyevent(82)),
            ("Power",   "#3a0a0a", "#c0392b",   lambda: self._adb.send_keyevent(26)),
        ]
        row_f = ctk.CTkFrame(card, fg_color="transparent")
        row_f.pack(fill="x", padx=10, pady=(0, 12))
        for text, fg, hov, cmd in actions:
            ctk.CTkButton(row_f, text=text, height=44,
                          fg_color=fg, hover_color=hov,
                          font=("Segoe UI", 11), text_color=self.C_TEXT,
                          command=cmd, corner_radius=8
                          ).pack(side="left", expand=True, fill="x", padx=4)
        return row + 1

    # ─────── Envio de texto ───────────────────────────────────────
    def _build_text_input(self, row):
        card = self._section_card(row, "TECLADO / ENVIO DE TEXTO")
        inner = ctk.CTkFrame(card, fg_color="transparent")
        inner.pack(fill="x", padx=10, pady=(0, 12))
        inner.grid_columnconfigure(0, weight=1)

        self._text_entry = ctk.CTkEntry(
            inner, placeholder_text="Digite o texto a enviar para a TV...",
            height=40, font=("Segoe UI", 12),
            fg_color=self.C_LOG_BG, border_color=self.C_BORDER,
            text_color=self.C_TEXT
        )
        self._text_entry.grid(row=0, column=0, sticky="ew", padx=(0, 6))
        self._text_entry.bind("<Return>", lambda e: self._send_text())

        ctk.CTkButton(inner, text="Enviar", width=90, height=40,
                      fg_color=self.C_ACCENT, hover_color=self.C_OK_HOV,
                      font=("Segoe UI", 11, "bold"),
                      command=self._send_text, corner_radius=8
                      ).grid(row=0, column=1)

        extra = ctk.CTkFrame(card, fg_color="transparent")
        extra.pack(fill="x", padx=10, pady=(0, 8))
        for text, cmd in [
            ("Backspace", lambda: self._adb.send_keyevent(67)),
            ("Enter (TV)", lambda: self._adb.send_keyevent(66)),
            ("Limpar campo", lambda: self._text_entry.delete(0, "end")),
        ]:
            ctk.CTkButton(extra, text=text, height=34,
                          fg_color=self.C_BTN, hover_color=self.C_BTN_HOV,
                          font=("Segoe UI", 10), command=cmd
                          ).pack(side="left", padx=(0, 4))
        return row + 1

    def _send_text(self):
        text = self._text_entry.get().strip()
        if text:
            self._adb.send_text(text)
            self._text_entry.delete(0, "end")
        else:
            self._log("Campo vazio.")

    # ─────── Lancador de Apps ─────────────────────────────────────
    def _build_app_launcher(self, row):
        card = self._section_card(row, "LANCADOR DE APPS")
        apps = [
            ("YouTube",      "#FF0000", "#cc0000",
             "com.google.android.youtube.tv",
             "com.google.android.apps.youtube.tv.activity.ShellActivity"),
            ("Netflix",       "#E50914", "#b8070f",
             "com.netflix.ninja", "com.netflix.ninja.MainActivity"),
            ("Prime Video",   "#00A8E1", "#0090c0",
             "com.amazon.amazonvideo.livingroom",
             "com.amazon.amazonvideo.livingroom.MainActivity"),
            ("Globoplay",     "#003087", "#004db8",
             "com.globo.globoplay", ""),
            ("Spotify",       "#1DB954", "#18a349",
             "com.spotify.tv.android",
             "com.spotify.tv.android.SpotifyTVActivity"),
            ("Globo",         "#003366", "#004499",
             "com.globo.android", ""),
            ("Play Store",    "#01875f", "#017050",
             "com.android.vending", ""),
            ("Arquivos",      "#607D8B", "#546E7A",
             "com.google.android.documentsui", ""),
            ("Config.",       "#455A64", "#37474F",
             "", "settings"),
        ]
        grid = ctk.CTkFrame(card, fg_color="transparent")
        grid.pack(fill="x", padx=10, pady=(0, 12))
        for i in range(3):
            grid.grid_columnconfigure(i, weight=1)

        for idx, (name, fg, hov, pkg, act) in enumerate(apps):
            r, c = divmod(idx, 3)
            if pkg == "" and act == "settings":
                cmd = lambda: self._adb.send_shell(
                    "am", "start", "-a", "android.settings.SETTINGS",
                    label="Configuracoes")
            elif act == "":
                cmd = lambda p=pkg, n=name: self._adb.launch_app(p, label=n)
            else:
                cmd = lambda p=pkg, a=act, n=name: self._adb.launch_app(p, a, n)

            ctk.CTkButton(
                grid, text=name, height=50,
                fg_color=fg, hover_color=hov,
                font=("Segoe UI", 11, "bold"), text_color="#fff",
                command=cmd, corner_radius=8
            ).grid(row=r, column=c, padx=4, pady=4, sticky="ew")
        return row + 1

    # ─────── Screenshot + scrcpy ─────────────────────────────────
    def _build_capture_and_scrcpy(self, row):
        card = self._section_card(row, "CAPTURA E ESPELHAMENTO")

        # -- Screenshot
        ss_row = ctk.CTkFrame(card, fg_color="transparent")
        ss_row.pack(fill="x", padx=10, pady=(0, 6))

        ctk.CTkButton(
            ss_row, text="Screenshot",
            height=44, fg_color="#1a2a3a", hover_color="#253a52",
            font=("Segoe UI", 11, "bold"), text_color=self.C_TEXT,
            command=self._adb.take_screenshot, corner_radius=8
        ).pack(side="left", fill="x", expand=True, padx=(0, 8))
        ctk.CTkLabel(
            ss_row, text="Captura a tela da TV\ne abre em popup para salvar",
            font=("Segoe UI", 9), text_color=self.C_TEXT_DIM
        ).pack(side="left")

        # -- divisor
        ctk.CTkFrame(card, height=1, fg_color=self.C_BORDER
                     ).pack(fill="x", padx=14, pady=6)

        # -- scrcpy: titulo
        ctk.CTkLabel(
            card, text="ESPELHAMENTO AO VIVO  (scrcpy)",
            font=("Segoe UI", 9, "bold"), text_color=self.C_TEXT_DIM
        ).pack(anchor="w", padx=14, pady=(2, 6))

        # -- opcoes em linha
        opts = ctk.CTkFrame(card, fg_color="transparent")
        opts.pack(fill="x", padx=12, pady=(0, 6))

        ctk.CTkLabel(opts, text="Bitrate:",
                     font=("Segoe UI", 10),
                     text_color=self.C_TEXT_DIM).pack(side="left")
        self._scrcpy_bitrate = ctk.CTkOptionMenu(
            opts, values=["4M", "8M", "12M", "16M", "20M"],
            width=76, height=28, fg_color=self.C_BTN,
            font=("Segoe UI", 10), text_color=self.C_TEXT
        )
        self._scrcpy_bitrate.set("8M")
        self._scrcpy_bitrate.pack(side="left", padx=(4, 14))

        ctk.CTkLabel(opts, text="Max FPS:",
                     font=("Segoe UI", 10),
                     text_color=self.C_TEXT_DIM).pack(side="left")
        self._scrcpy_fps = ctk.CTkOptionMenu(
            opts, values=["15", "24", "30", "60"],
            width=66, height=28, fg_color=self.C_BTN,
            font=("Segoe UI", 10), text_color=self.C_TEXT
        )
        self._scrcpy_fps.set("30")
        self._scrcpy_fps.pack(side="left", padx=(4, 14))

        self._scrcpy_nocontrol = tk.BooleanVar(value=False)
        ctk.CTkCheckBox(
            opts, text="So visualizar",
            variable=self._scrcpy_nocontrol,
            font=("Segoe UI", 10), text_color=self.C_TEXT_DIM,
            fg_color=self.C_ACCENT, border_color=self.C_BORDER,
            checkbox_width=16, checkbox_height=16
        ).pack(side="left", padx=(0, 10))

        self._scrcpy_record = tk.BooleanVar(value=False)
        ctk.CTkCheckBox(
            opts, text="Gravar .mp4",
            variable=self._scrcpy_record,
            font=("Segoe UI", 10), text_color=self.C_TEXT_DIM,
            fg_color="#c0392b", border_color=self.C_BORDER,
            checkbox_width=16, checkbox_height=16
        ).pack(side="left")

        # -- botoes iniciar / parar
        btn_row = ctk.CTkFrame(card, fg_color="transparent")
        btn_row.pack(fill="x", padx=10, pady=(4, 6))

        self._scrcpy_btn_start = ctk.CTkButton(
            btn_row, text="Iniciar espelhamento",
            height=44, fg_color="#1a3a1a", hover_color="#2d6e2d",
            font=("Segoe UI", 11, "bold"), text_color=self.C_TEXT,
            command=self._scrcpy_start, corner_radius=8
        )
        self._scrcpy_btn_start.pack(side="left", fill="x", expand=True, padx=(0, 6))

        self._scrcpy_btn_stop = ctk.CTkButton(
            btn_row, text="Parar",
            height=44, fg_color="#3a0a0a", hover_color="#c0392b",
            font=("Segoe UI", 11, "bold"), text_color=self.C_TEXT,
            command=self._scrcpy_stop, corner_radius=8, state="disabled"
        )
        self._scrcpy_btn_stop.pack(side="left", fill="x", expand=True)

        # -- indicador de status
        self._scrcpy_status_lbl = ctk.CTkLabel(
            card, text="  Parado",
            font=("Segoe UI", 10), text_color=self.C_TEXT_DIM
        )
        self._scrcpy_status_lbl.pack(anchor="w", padx=14, pady=(0, 10))

        return row + 1

    def _scrcpy_start(self):
        """Lanca o scrcpy em processo separado — nao bloqueia a GUI."""
        if self._scrcpy_proc and self._scrcpy_proc.poll() is None:
            self._log("scrcpy ja esta rodando.")
            return

        bitrate    = self._scrcpy_bitrate.get()
        fps        = self._scrcpy_fps.get()
        no_control = self._scrcpy_nocontrol.get()
        do_record  = self._scrcpy_record.get()

        cmd = [
            "scrcpy",
            f"--tcpip={TV_ADDRESS}",
            f"--video-bit-rate={bitrate}",
            f"--max-fps={fps}",
            "--window-title=AIWA TV",
        ]
        if no_control:
            cmd.append("--no-control")
        if do_record:
            ts       = datetime.now().strftime("%Y%m%d_%H%M%S")
            rec_path = os.path.join(os.path.expanduser("~"), f"aiwa_tv_{ts}.mp4")
            cmd += ["--record", rec_path]
            self._log(f"Gravando em: {rec_path}")

        def _run():
            try:
                self._scrcpy_proc = subprocess.Popen(
                    cmd,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.STDOUT,
                    text=True
                )
                self._q_scrcpy_status(True)
                self._log(f"scrcpy iniciado (pid {self._scrcpy_proc.pid})")
                self._scrcpy_proc.wait()           # aguarda em background
                self._q_scrcpy_status(False)
                self._log("scrcpy encerrado.")
            except FileNotFoundError:
                self._q_scrcpy_status(False)
                self._adb.result_queue.put({
                    "type": "log",
                    "data": "scrcpy nao encontrado — instale e adicione ao PATH",
                    "time": datetime.now()
                })
            except Exception as e:
                self._q_scrcpy_status(False)
                self._adb.result_queue.put({
                    "type": "log", "data": f"scrcpy erro: {e}",
                    "time": datetime.now()
                })

        threading.Thread(target=_run, daemon=True, name="scrcpy").start()

    def _scrcpy_stop(self):
        if self._scrcpy_proc and self._scrcpy_proc.poll() is None:
            self._scrcpy_proc.terminate()
            self._log("scrcpy interrompido.")
        else:
            self._log("scrcpy nao esta rodando.")

    def _q_scrcpy_status(self, running: bool):
        """Enfileira atualizacao de status do scrcpy para a GUI."""
        self._adb.result_queue.put({
            "type": "scrcpy_status", "data": running, "time": datetime.now()
        })

    def _update_scrcpy_ui(self, running: bool):
        if running:
            self._scrcpy_btn_start.configure(state="disabled")
            self._scrcpy_btn_stop.configure(state="normal")
            self._scrcpy_status_lbl.configure(
                text="  Transmitindo ao vivo", text_color=self.C_GREEN)
        else:
            self._scrcpy_btn_start.configure(state="normal")
            self._scrcpy_btn_stop.configure(state="disabled")
            self._scrcpy_status_lbl.configure(
                text="  Parado", text_color=self.C_TEXT_DIM)

    # ─────── Painel CPU / RAM ─────────────────────────────────────
    def _build_stats_panel(self, row):
        card = self._section_card(row, "MONITOR DE DESEMPENHO DA TV")

        # -- toggle
        top_row = ctk.CTkFrame(card, fg_color="transparent")
        top_row.pack(fill="x", padx=10, pady=(0, 8))

        self._stats_toggle_btn = ctk.CTkButton(
            top_row, text="Iniciar monitor",
            height=36, width=150,
            fg_color="#1a2a1a", hover_color="#2d6e2d",
            font=("Segoe UI", 10, "bold"), text_color=self.C_TEXT,
            command=self._toggle_stats, corner_radius=8
        )
        self._stats_toggle_btn.pack(side="left", padx=(0, 10))
        ctk.CTkLabel(
            top_row,
            text=f"Atualiza a cada {STATS_INTERVAL}s  |  /proc/stat  +  /proc/meminfo",
            font=("Segoe UI", 9), text_color=self.C_TEXT_DIM
        ).pack(side="left")

        # -- CPU card
        cpu_card = ctk.CTkFrame(card, fg_color=self.C_DPAD, corner_radius=8)
        cpu_card.pack(fill="x", padx=10, pady=(0, 6))

        cpu_top = ctk.CTkFrame(cpu_card, fg_color="transparent")
        cpu_top.pack(fill="x", padx=12, pady=(8, 2))
        ctk.CTkLabel(cpu_top, text="CPU",
                     font=("Segoe UI", 11, "bold"),
                     text_color=self.C_TEXT).pack(side="left")
        self._cpu_pct_lbl = ctk.CTkLabel(
            cpu_top, text="  --%",
            font=("Consolas", 14, "bold"),
            text_color=self.C_TEXT_DIM)
        self._cpu_pct_lbl.pack(side="right")

        self._cpu_bar = ctk.CTkProgressBar(cpu_card, height=12, corner_radius=4,
                                              progress_color=self.C_GREEN,
                                              fg_color=self.C_DPAD)
        self._cpu_bar.set(0)
        self._cpu_bar.pack(fill="x", padx=12, pady=(2, 8))

        # -- RAM card
        ram_card = ctk.CTkFrame(card, fg_color=self.C_DPAD, corner_radius=8)
        ram_card.pack(fill="x", padx=10, pady=(0, 6))

        ram_top = ctk.CTkFrame(ram_card, fg_color="transparent")
        ram_top.pack(fill="x", padx=12, pady=(8, 2))
        ctk.CTkLabel(ram_top, text="RAM",
                     font=("Segoe UI", 11, "bold"),
                     text_color=self.C_TEXT).pack(side="left")
        self._ram_pct_lbl = ctk.CTkLabel(
            ram_top, text="  -- / -- MB",
            font=("Consolas", 12),
            text_color=self.C_TEXT_DIM)
        self._ram_pct_lbl.pack(side="right")

        self._ram_bar = ctk.CTkProgressBar(ram_card, height=12, corner_radius=4,
                                              progress_color=self.C_GREEN,
                                              fg_color=self.C_DPAD)
        self._ram_bar.set(0)
        self._ram_bar.pack(fill="x", padx=12, pady=(2, 8))

        # -- top processos
        ctk.CTkLabel(card, text="TOP PROCESSOS POR CPU",
                     font=("Segoe UI", 9, "bold"),
                     text_color=self.C_TEXT_DIM
                     ).pack(anchor="w", padx=14, pady=(4, 2))

        self._top_frame = ctk.CTkFrame(
            card, fg_color=self.C_LOG_BG, corner_radius=6,
            border_width=1, border_color=self.C_BORDER)
        self._top_frame.pack(fill="x", padx=10, pady=(0, 12))

        self._top_labels = []
        for _ in range(5):
            rf = ctk.CTkFrame(self._top_frame, fg_color="transparent")
            rf.pack(fill="x", padx=8, pady=1)
            n = ctk.CTkLabel(rf, text="--", font=("Consolas", 10),
                             text_color=self.C_TEXT_DIM, anchor="w", width=200)
            n.pack(side="left")
            p = ctk.CTkLabel(rf, text="", font=("Consolas", 10),
                             text_color=self.C_YELLOW)
            p.pack(side="right")
            self._top_labels.append((n, p))

        return row + 1

    def _toggle_stats(self):
        if not self._stats_on:
            self._stats_on = True
            self._stats_toggle_btn.configure(
                text="Parar monitor",
                fg_color="#3a0a0a", hover_color="#c0392b")
            self._adb.start_stats()
            self._log("Monitor de desempenho iniciado")
        else:
            self._stats_on = False
            self._stats_toggle_btn.configure(
                text="Iniciar monitor",
                fg_color="#1a2a1a", hover_color="#2d6e2d")
            self._adb.stop_stats()
            self._log("Monitor de desempenho parado")

    def _update_stats_ui(self, data: dict):
        """Atualiza barras e labels de CPU/RAM na thread da GUI."""
        cpu   = data.get("cpu_pct", 0)
        rused = data.get("ram_used_mb", 0)
        rtot  = data.get("ram_total_mb", 0)
        rpct  = data.get("ram_pct", 0)
        procs = data.get("top_procs", [])

        # CPU
        c_color = self.C_GREEN if cpu < 60 else (self.C_YELLOW if cpu < 80 else self.C_RED)
        self._cpu_pct_lbl.configure(text=f"  {cpu:.1f}%", text_color=c_color)
        self._cpu_bar.configure(progress_color=c_color)
        self._cpu_bar.set(cpu / 100.0)

        # RAM
        r_color = self.C_GREEN if rpct < 60 else (self.C_YELLOW if rpct < 80 else self.C_RED)
        self._ram_pct_lbl.configure(
            text=f"  {rused} / {rtot} MB  ({rpct:.0f}%)",
            text_color=r_color)
        self._ram_bar.configure(progress_color=r_color)
        self._ram_bar.set(rpct / 100.0)

        # Top processos
        for i, (n_lbl, p_lbl) in enumerate(self._top_labels):
            if i < len(procs):
                pct_val, pname = procs[i]
                n_lbl.configure(text=pname, text_color=self.C_TEXT)
                p_lbl.configure(text=f"{pct_val:.1f}%")
            else:
                n_lbl.configure(text="--", text_color=self.C_TEXT_DIM)
                p_lbl.configure(text="")


    # ─────── Notificacoes ────────────────────────────────────────
    def _build_notification_panel(self, row):
        card = self._section_card(row, "NOTIFICACOES NA TV  (via aiwa-toast APK)")

        # ── faixa de status Termux + botao setup
        setup_row = ctk.CTkFrame(card, fg_color="#0a1a0a", corner_radius=8)
        setup_row.pack(fill="x", padx=10, pady=(0, 8))

        self._termux_status_lbl = ctk.CTkLabel(
            setup_row,
            text="aiwa-toast APK: verificando...",
            font=("Segoe UI", 10), text_color=self.C_TEXT_DIM
        )
        self._termux_status_lbl.pack(side="left", padx=12, pady=8)

        ctk.CTkButton(
            setup_row,
            text="Verificar APK instalado",
            width=180, height=30,
            fg_color="#1a3a1a", hover_color="#2d6e2d",
            font=("Segoe UI", 10, "bold"), text_color="#7fcfaf",
            command=self._check_termux, corner_radius=6
        ).pack(side="right", padx=8, pady=8)

        ctk.CTkButton(
            setup_row,
            text="Verificar",
            width=80, height=30,
            fg_color=self.C_BTN, hover_color=self.C_BTN_HOV,
            font=("Segoe UI", 9), text_color=self.C_TEXT_DIM,
            command=self._check_termux, corner_radius=6
        ).pack(side="right", padx=(0, 4), pady=8)

        # ── campo de titulo (usado pelos metodos 2 e 3)
        title_row = ctk.CTkFrame(card, fg_color="transparent")
        title_row.pack(fill="x", padx=10, pady=(0, 6))
        title_row.grid_columnconfigure(1, weight=1)

        ctk.CTkLabel(title_row, text="Titulo:",
                     font=("Segoe UI", 10), text_color=self.C_TEXT_DIM,
                     width=50, anchor="w").grid(row=0, column=0, padx=(0, 6))
        self._notif_title = ctk.CTkEntry(
            title_row,
            placeholder_text="Titulo da notificacao (opcional para Toast)...",
            height=34, font=("Segoe UI", 11),
            fg_color=self.C_LOG_BG, border_color=self.C_BORDER,
            text_color=self.C_TEXT
        )
        self._notif_title.grid(row=0, column=1, sticky="ew")

        # ── campo de mensagem
        msg_row = ctk.CTkFrame(card, fg_color="transparent")
        msg_row.pack(fill="x", padx=10, pady=(0, 10))
        msg_row.grid_columnconfigure(1, weight=1)

        ctk.CTkLabel(msg_row, text="Mensagem:",
                     font=("Segoe UI", 10), text_color=self.C_TEXT_DIM,
                     width=70, anchor="w").grid(row=0, column=0, padx=(0, 6))
        self._notif_msg = ctk.CTkEntry(
            msg_row,
            placeholder_text="Texto da notificacao...",
            height=34, font=("Segoe UI", 11),
            fg_color=self.C_LOG_BG, border_color=self.C_BORDER,
            text_color=self.C_TEXT
        )
        self._notif_msg.grid(row=0, column=1, sticky="ew")
        self._notif_msg.bind("<Return>", lambda e: self._send_toast())

        # ── botoes dos 3 metodos
        btns = ctk.CTkFrame(card, fg_color="transparent")
        btns.pack(fill="x", padx=10, pady=(0, 8))
        btns.grid_columnconfigure((0, 1, 2), weight=1)

        # Metodo 1 — Toast
        f1 = ctk.CTkFrame(btns, fg_color="#0f1f2a", corner_radius=8,
                           border_width=1, border_color="#1a4060")
        f1.grid(row=0, column=0, padx=(0, 5), pady=0, sticky="nsew")
        ctk.CTkLabel(f1, text="1  Toast",
                     font=("Segoe UI", 10, "bold"),
                     text_color="#4fc3f7").pack(pady=(8, 2))
        ctk.CTkLabel(f1, text="Balao flutuante\nsome automaticamente",
                     font=("Segoe UI", 8), text_color=self.C_TEXT_DIM,
                     justify="center").pack(pady=(0, 6))
        ctk.CTkButton(
            f1, text="Enviar Toast",
            height=36, fg_color="#1a3a52", hover_color="#235e82",
            font=("Segoe UI", 10, "bold"), text_color="#4fc3f7",
            command=self._send_toast, corner_radius=6
        ).pack(fill="x", padx=8, pady=(0, 8))

        # Metodo 2 — Sistema
        f2 = ctk.CTkFrame(btns, fg_color="#1a1a0f", corner_radius=8,
                           border_width=1, border_color="#3a3a10")
        f2.grid(row=0, column=1, padx=3, pady=0, sticky="nsew")
        ctk.CTkLabel(f2, text="2  Sistema",
                     font=("Segoe UI", 10, "bold"),
                     text_color="#f9ca24").pack(pady=(8, 2))
        ctk.CTkLabel(f2, text="Aparece na bandeja\npersiste ate dispensar",
                     font=("Segoe UI", 8), text_color=self.C_TEXT_DIM,
                     justify="center").pack(pady=(0, 6))
        ctk.CTkButton(
            f2, text="Enviar p/ Bandeja",
            height=36, fg_color="#2e2e10", hover_color="#4a4a18",
            font=("Segoe UI", 10, "bold"), text_color="#f9ca24",
            command=self._send_system_notif, corner_radius=6
        ).pack(fill="x", padx=8, pady=(0, 8))

        # Metodo 3 — Overlay / Intent
        f3 = ctk.CTkFrame(btns, fg_color="#1a0f1a", corner_radius=8,
                           border_width=1, border_color="#3a1060")
        f3.grid(row=0, column=2, padx=(5, 0), pady=0, sticky="nsew")
        ctk.CTkLabel(f3, text="3  Overlay",
                     font=("Segoe UI", 10, "bold"),
                     text_color="#a29bfe").pack(pady=(8, 2))
        ctk.CTkLabel(f3, text="Intent ACTION_SEND\nabre seletor do sistema",
                     font=("Segoe UI", 8), text_color=self.C_TEXT_DIM,
                     justify="center").pack(pady=(0, 6))
        ctk.CTkButton(
            f3, text="Enviar Overlay",
            height=36, fg_color="#2a1040", hover_color="#3d186b",
            font=("Segoe UI", 10, "bold"), text_color="#a29bfe",
            command=self._send_overlay, corner_radius=6
        ).pack(fill="x", padx=8, pady=(0, 8))

        # ── dica rapida
        # ── dica + botao de diagnostico
        hint_row = ctk.CTkFrame(card, fg_color="transparent")
        hint_row.pack(fill="x", padx=10, pady=(0, 10))

        ctk.CTkLabel(
            hint_row,
            text="Cada botao tenta 3-4 metodos em cascata e loga qual funcionou.",
            font=("Segoe UI", 8), text_color=self.C_TEXT_DIM
        ).pack(side="left", anchor="w")

        ctk.CTkButton(
            hint_row, text="Diagnostico", width=100, height=24,
            fg_color=self.C_BTN, hover_color=self.C_BTN_HOV,
            font=("Segoe UI", 8), text_color=self.C_TEXT_DIM,
            command=self._notif_diagnostico, corner_radius=4
        ).pack(side="right")

        return row + 1

    def _setup_termux(self):
        """Configura permissoes do Termux (rodar uma vez apos instalar)."""
        self._log("Iniciando configuracao do Termux...")
        self._adb.setup_termux()

    def _check_termux(self):
        """Verifica se o Termux esta disponivel e atualiza o indicador."""
        def _do():
            available = self._adb._toast_apk_available()
            self._adb.result_queue.put({
                "type": "termux_status",
                "data": available,
                "time": __import__("datetime").datetime.now()
            })
        __import__("threading").Thread(target=_do, daemon=True).start()

    def _notif_diagnostico(self):
        """Roda uma bateria de testes de notificacao e loga os resultados."""
        def _do():
            self._adb._post("log", "=== Diagnostico de notificacoes ===")

            # Testa cmd notification
            ok, out = self._adb._run_adb("shell", "cmd", "notification", "help")
            self._adb._post("log", f"cmd notification: {'OK' if ok else 'FALHOU'} — {out[:60]}")

            # Testa service call notification
            ok, out = self._adb._run_adb("shell", "service", "check", "notification")
            self._adb._post("log", f"service notification: {'OK — ' + out[:50] if ok else 'FALHOU'}")

            # Testa am broadcast usuario 0
            ok, out = self._adb._run_adb(
                "shell", "am", "broadcast", "--user", "0",
                "-a", "android.intent.action.SEND",
                "--es", "android.intent.extra.TEXT", "TESTE_AIWA",
                "-t", "text/plain"
            )
            self._adb._post("log", f"am broadcast --user 0: {'OK' if ok else 'FALHOU'} — {out[:60]}")

            # Testa am start ACTION_SEND
            ok, out = self._adb._run_adb(
                "shell", "am", "start",
                "-a", "android.intent.action.SEND",
                "-t", "text/plain",
                "--es", "android.intent.extra.TEXT", "TESTE_AIWA",
                "-f", "0x10008000"
            )
            self._adb._post("log", f"am start ACTION_SEND: {'OK' if ok else 'FALHOU'} — {out[:60]}")

            self._adb._post("log", "=== Fim do diagnostico ===")

        threading.Thread(target=_do, daemon=True, name="notif-diag").start()

    def _get_notif_fields(self):
        title = self._notif_title.get().strip() or "AIWA Remote"
        msg   = self._notif_msg.get().strip()
        return title, msg

    def _send_toast(self):
        title, msg = self._get_notif_fields()
        if not msg:
            self._log("Preencha a Mensagem antes de enviar.")
            return
        self._adb.notify_toast(msg)

    def _send_system_notif(self):
        title, msg = self._get_notif_fields()
        if not msg:
            self._log("Preencha a Mensagem antes de enviar.")
            return
        self._adb.notify_system(title, msg)

    def _send_overlay(self):
        title, msg = self._get_notif_fields()
        if not msg:
            self._log("Preencha a Mensagem antes de enviar.")
            return
        self._adb.notify_overlay(msg)

    # ─────── Log console ─────────────────────────────────────────
    def _build_log(self, row):
        card = self._section_card(row, "LOG DE COMANDOS")
        self._log_text = ctk.CTkTextbox(
            card, height=120,
            fg_color=self.C_LOG_BG, border_color=self.C_BORDER, border_width=1,
            font=("Consolas", 10), text_color="#7fcfaf",
            scrollbar_button_color=self.C_BORDER,
            corner_radius=6
        )
        self._log_text.pack(fill="x", padx=10, pady=(0, 4))
        self._log_text.configure(state="disabled")

        ctk.CTkButton(card, text="Limpar log", height=26, width=90,
                      fg_color="transparent", hover_color=self.C_BTN,
                      font=("Segoe UI", 9), text_color=self.C_TEXT_DIM,
                      command=self._clear_log
                      ).pack(anchor="e", padx=10, pady=(0, 8))

        self._log(f"AIWA Pro Remote iniciado  {datetime.now().strftime('%d/%m/%Y %H:%M:%S')}")
        self._log(f"Alvo: {TV_ADDRESS}")
        return row + 1

    # ─────── Helpers ─────────────────────────────────────────────
    def _section_card(self, row, title):
        outer = ctk.CTkFrame(self._main, fg_color=self.C_CARD,
                             corner_radius=12, border_width=1,
                             border_color=self.C_BORDER)
        outer.grid(row=row, column=0, sticky="ew", padx=12, pady=4)
        ctk.CTkLabel(outer, text=title,
                     font=("Segoe UI", 9, "bold"),
                     text_color=self.C_TEXT_DIM).pack(anchor="w", padx=14, pady=(10, 4))
        ctk.CTkFrame(outer, height=1, fg_color=self.C_BORDER
                     ).pack(fill="x", padx=14, pady=(0, 10))
        return outer

    def _log(self, msg):
        ts = datetime.now().strftime("%H:%M:%S")
        self._log_text.configure(state="normal")
        self._log_text.insert("end", f"[{ts}] {msg}\n")
        self._log_text.see("end")
        self._log_text.configure(state="disabled")

    def _clear_log(self):
        self._log_text.configure(state="normal")
        self._log_text.delete("1.0", "end")
        self._log_text.configure(state="disabled")

    def _update_status(self, status):
        self._conn_status = status
        info = {
            STATUS_CONNECTED:    (self.C_GREEN,  "Conectado"),
            STATUS_STANDBY:      (self.C_YELLOW, "Standby"),
            STATUS_DISCONNECTED: (self.C_RED,    "Desconectado"),
            STATUS_CONNECTING:   ("#4fc3f7",     "Conectando..."),
            STATUS_ERROR:        (self.C_RED,    "Erro"),
        }
        color, label = info.get(status, (self.C_RED, "?"))
        self._status_dot.configure(text_color=color)
        self._status_label.configure(text=label)

    # ─────── Polling da fila ─────────────────────────────────────
    def _poll_queue(self):
        try:
            while True:
                msg   = self._queue.get_nowait()
                mtype = msg["type"]
                data  = msg["data"]

                if mtype == "log":
                    self._log(str(data))
                elif mtype == "status":
                    self._update_status(data)
                elif mtype == "screenshot":
                    self._show_screenshot(data)
                elif mtype == "stats":
                    self._update_stats_ui(data)
                elif mtype == "scrcpy_status":
                    self._update_scrcpy_ui(data)
                elif mtype == "termux_status":
                    self._update_termux_status(data)

        except queue.Empty:
            pass
        finally:
            self.after(50, self._poll_queue)

    def _update_termux_status(self, available: bool):
        """Atualiza o indicador de status do Termux no painel de notificacoes."""
        if available:
            self._termux_status_lbl.configure(
                text="aiwa-toast: instalado e pronto",
                text_color=self.C_GREEN
            )
        else:
            self._termux_status_lbl.configure(
                text="aiwa-toast: nao instalado — instale via adb install",
                text_color=self.C_RED
            )

    def _show_screenshot(self, path):
        try:
            ScreenshotWindow(self, path)
        except Exception as e:
            self._log(f"Screenshot erro: {e}")

    def _update_web_indicator(self):
        """Atualiza o label do servidor web com o IP local."""
        if not FLASK_OK:
            return
        try:
            import socket
            s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            s.connect(("8.8.8.8", 80))
            ip = s.getsockname()[0]
            s.close()
            self._web_lbl.configure(
                text=f"Servidor web ativo  →  http://{ip}:{WEB_PORT}  |  Senha: {WEB_PASSWORD}",
                text_color="#4fc3f7"
            )
        except Exception:
            self._web_lbl.configure(
                text=f"Servidor web ativo  →  http://localhost:{WEB_PORT}",
                text_color="#4fc3f7"
            )

    def on_closing(self):
        self._adb._stop_event.set()
        self._adb.stop_stats()
        if self._scrcpy_proc and self._scrcpy_proc.poll() is None:
            self._scrcpy_proc.terminate()
        if hasattr(self, "_web"):
            self._web.stop()
        self.destroy()


# ─────────────────────────────────────────────────────────────────
#  VERIFICACAO DE DEPENDENCIAS
# ─────────────────────────────────────────────────────────────────
def check_dependencies():
    missing = []
    for pkg, imp in [("customtkinter", "customtkinter"), ("Pillow", "PIL")]:
        try:
            __import__(imp)
        except ImportError:
            missing.append(pkg)
    try:
        subprocess.run(["adb", "version"], capture_output=True, timeout=5)
    except (FileNotFoundError, subprocess.TimeoutExpired):
        missing.append("adb (Android Platform Tools)")
    # Flask e opcional — servidor web nao inicia sem ele, mas nao bloqueia o app
    if not FLASK_OK:
        print("[aviso] Flask nao instalado — servidor web desativado. Rode: pip install flask")
    return missing


# ─────────────────────────────────────────────────────────────────
#  ENTRY POINT
# ─────────────────────────────────────────────────────────────────
if __name__ == "__main__":
    missing = check_dependencies()
    if missing:
        print("=" * 60)
        print("  DEPENDENCIAS FALTANDO")
        print("=" * 60)
        for d in missing:
            print(f"  x {d}")
        print()
        print("  pip install customtkinter Pillow")
        print()
        print("  ADB:    https://developer.android.com/tools/releases/platform-tools")
        print()
        print("  scrcpy (opcional, para espelhamento):")
        print("    Windows: winget install Genymobile.scrcpy")
        print("    macOS:   brew install scrcpy")
        print("    Linux:   sudo apt install scrcpy")
        print("=" * 60)
        try:
            import tkinter as _tk
            from tkinter import messagebox as _mb
            _r = _tk.Tk(); _r.withdraw()
            _mb.showerror(
                "Dependencias faltando",
                "\n".join(f"  {d}" for d in missing)
                + "\n\npip install customtkinter Pillow"
            )
            _r.destroy()
        except Exception:
            pass
        sys.exit(1)

    app = AIWAProRemote()
    app.protocol("WM_DELETE_WINDOW", app.on_closing)
    app.mainloop()
