#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
VocalType - Dictee vocale universelle pour Windows
Raccourci par defaut : Ctrl+Alt+Espace
"""

import os
import sys
import json
import time
import ctypes
import threading
import tempfile
import winreg
import winsound
import logging
from datetime import datetime

# ── Instance unique ───────────────────────────────────────────
_MUTEX = ctypes.windll.kernel32.CreateMutexW(None, True, "VocalType_SingleInstance_v1")
if ctypes.windll.kernel32.GetLastError() == 183:
    sys.exit(0)

try:
    import numpy as np
    import sounddevice as sd
    from scipy.io.wavfile import write as write_wav
    import speech_recognition as sr
    import pyperclip
    import keyboard
    import tkinter as tk
    from PIL import Image, ImageDraw
    import pystray
except ImportError as e:
    print(f"\nERREUR - Module manquant : {e}")
    print("Lance install.bat pour installer toutes les dependances.")
    input("\nAppuie sur Entree pour quitter...")
    sys.exit(1)

# ── Chemins (fonctionne en .py ET en .exe PyInstaller) ────────
if getattr(sys, 'frozen', False):
    _DIR = os.path.dirname(sys.executable)
else:
    _DIR = os.path.dirname(os.path.abspath(__file__))

SETTINGS_PATH = os.path.join(_DIR, 'settings.json')
HISTORY_PATH  = os.path.join(_DIR, 'history.json')
LOG_PATH      = os.path.join(_DIR, 'vocaltype.log')

# ── Logging ───────────────────────────────────────────────────
logging.basicConfig(
    filename=LOG_PATH,
    level=logging.INFO,
    format='%(asctime)s [%(levelname)s] %(message)s',
    datefmt='%Y-%m-%d %H:%M:%S',
    encoding='utf-8',
)
log = logging.getLogger('VocalType')

# ── Constantes ────────────────────────────────────────────────
APP_NAME          = 'VocalType'
DEFAULT_HOTKEY    = 'ctrl+shift+space'
SAMPLE_RATE       = 16000
_TRANSP           = '#010101'
VAD_PAUSE_SEC     = 0.35       # Mode rapide : pause de 350ms = fin de phrase naturelle
VAD_MIN_SEC       = 0.4        # Duree minimale d'audio pour tenter la transcription
VAD_MAX_SEC       = 3.5        # Duree max avant envoi force (si parole continue)
MAX_HISTORY       = 50
SILENCE_THRESHOLD = 0.012

# Tailles de la pilule
PW_MINI, PH_MINI = 22, 22    # Mini : petit point discret en haut de l'ecran
PW_FULL, PH_FULL = 180, 30   # Full : pilule avec texte pendant l'ecoute

_STATES = {
    'ready':      ('#1a1a2e', '#a5b4fc'),
    'idle':       ('#1a1a2e', '#a5b4fc'),
    'recording':  ('#3b0000', '#fca5a5'),
    'processing': ('#2d1a00', '#fde68a'),
    'success':    ('#002d0f', '#86efac'),
    'error':      ('#2d0000', '#fca5a5'),
}
_LABELS = {
    'ready':      'VocalType',
    'idle':       'Clic -> Parler  |  Clic droit -> Menu',
    'recording':  'Ecoute...',
    'processing': 'Transcription...',
    'error':      'Erreur - reessaie',
}
PW_IDLE = 260   # Pilule idle plus large pour afficher les instructions
REG_PATH = r'Software\Microsoft\Windows\CurrentVersion\Run'

UI = {
    'bg':      '#0f0f1a',
    'bg2':     '#1a1a2e',
    'accent':  '#6366f1',
    'accent2': '#818cf8',
    'text':    '#e2e8f0',
    'text2':   '#94a3b8',
    'border':  '#2d2d4e',
    'btn':     '#6366f1',
}


# ── Colle le texte via l'API Windows directement ──────────────
def _paste_via_winapi(text, add_space=False):
    """Copie text dans le presse-papier et envoie Ctrl+V via ctypes.
    add_space=True envoie une touche Espace apres le collage (mode rapide)."""
    try:
        old = pyperclip.paste()
    except Exception:
        old = ''
    pyperclip.copy(text)
    time.sleep(0.25)
    VK_CONTROL      = 0x11
    VK_V            = 0x56
    VK_SPACE        = 0x20
    KEYEVENTF_KEYUP = 0x0002
    ctypes.windll.user32.keybd_event(VK_CONTROL, 0, 0, 0)
    ctypes.windll.user32.keybd_event(VK_V,       0, 0, 0)
    ctypes.windll.user32.keybd_event(VK_V,       0, KEYEVENTF_KEYUP, 0)
    ctypes.windll.user32.keybd_event(VK_CONTROL, 0, KEYEVENTF_KEYUP, 0)
    if add_space:
        time.sleep(0.05)
        ctypes.windll.user32.keybd_event(VK_SPACE, 0, 0, 0)
        ctypes.windll.user32.keybd_event(VK_SPACE, 0, KEYEVENTF_KEYUP, 0)
    def _restore():
        time.sleep(1.5)
        try:
            pyperclip.copy(old)
        except Exception:
            pass
    threading.Thread(target=_restore, daemon=True).start()


# ══════════════════════════════════════════════════════════════
#  PARAMÈTRES
# ══════════════════════════════════════════════════════════════
class Settings:
    DEFAULTS = {
        'language':    'fr-FR',
        'mode':        'precise',
        'hotkey':      DEFAULT_HOTKEY,
        'silence_sec': 2,
    }

    def __init__(self):
        self._lock = threading.Lock()
        self._data = dict(self.DEFAULTS)
        self._load()

    def _load(self):
        try:
            with open(SETTINGS_PATH, 'r', encoding='utf-8') as f:
                loaded = json.load(f)
                if isinstance(loaded.get('language'), str):
                    self._data['language'] = loaded['language']
                if loaded.get('mode') in ('precise', 'fast'):
                    self._data['mode'] = loaded['mode']
                if isinstance(loaded.get('hotkey'), str) and loaded['hotkey']:
                    self._data['hotkey'] = loaded['hotkey']
                if isinstance(loaded.get('silence_sec'), (int, float)):
                    self._data['silence_sec'] = int(loaded['silence_sec'])
        except FileNotFoundError:
            pass   # Premier lancement, fichier pas encore cree
        except Exception as e:
            log.warning(f"Settings corrompus, defaults utilises : {e}")

    def save(self):
        with self._lock:
            try:
                with open(SETTINGS_PATH, 'w', encoding='utf-8') as f:
                    json.dump(self._data, f, indent=2, ensure_ascii=False)
            except Exception as e:
                log.error(f"Impossible de sauvegarder settings : {e}")

    def get(self, key):
        return self._data.get(key, self.DEFAULTS.get(key))

    def set(self, key, value):
        self._data[key] = value
        self.save()


# ══════════════════════════════════════════════════════════════
#  HISTORIQUE
# ══════════════════════════════════════════════════════════════
class History:
    def __init__(self):
        self._lock  = threading.Lock()
        self._items = []
        self._load()

    def _load(self):
        try:
            with open(HISTORY_PATH, 'r', encoding='utf-8') as f:
                data = json.load(f)
                if isinstance(data, list):
                    self._items = data
        except Exception:
            self._items = []

    def _save(self):
        try:
            with open(HISTORY_PATH, 'w', encoding='utf-8') as f:
                json.dump(self._items[-MAX_HISTORY:], f,
                          ensure_ascii=False, indent=2)
        except Exception as e:
            log.error(f"Impossible de sauvegarder l'historique : {e}")

    def add(self, text):
        with self._lock:
            ts = datetime.now().strftime('%H:%M')
            self._items.append([ts, text])
            if len(self._items) > MAX_HISTORY:
                self._items = self._items[-MAX_HISTORY:]
            self._save()

    def clear(self):
        with self._lock:
            self._items = []
            self._save()

    def all(self):
        with self._lock:
            return list(reversed(self._items))


# ══════════════════════════════════════════════════════════════
#  FENÊTRE PARAMÈTRES
# ══════════════════════════════════════════════════════════════
class SettingsWindow:
    def __init__(self, parent, settings, on_save_cb):
        self.settings   = settings
        self.on_save_cb = on_save_cb

        self.win = tk.Toplevel(parent)
        self.win.title("Parametres  -  VocalType")
        self.win.resizable(False, False)
        self.win.configure(bg=UI['bg'])
        self.win.wm_attributes('-topmost', True)
        sw = self.win.winfo_screenwidth()
        sh = self.win.winfo_screenheight()
        w, h = 370, 530
        self.win.geometry(f"{w}x{h}+{(sw-w)//2}+{(sh-h)//2}")
        self._build()

    def _section(self, title):
        tk.Label(self.win, text=title, bg=UI['bg'], fg=UI['accent2'],
                 font=('Segoe UI', 10, 'bold')).pack(anchor='w', pady=(14,3), padx=20)
        tk.Frame(self.win, bg=UI['border'], height=1).pack(fill='x', padx=20)

    def _radio(self, var, label, value):
        tk.Radiobutton(self.win, text=label, variable=var, value=value,
                       bg=UI['bg'], fg=UI['text'], selectcolor=UI['bg2'],
                       activebackground=UI['bg'], activeforeground=UI['accent2'],
                       font=('Segoe UI', 10), borderwidth=0
                       ).pack(anchor='w', padx=32, pady=3)

    def _build(self):
        tk.Label(self.win, text="Parametres", bg=UI['bg'], fg=UI['text'],
                 font=('Segoe UI', 13, 'bold')).pack(pady=(18, 0))

        self._section("Langue")
        self.lang_var = tk.StringVar(value=self.settings.get('language'))
        self._radio(self.lang_var, "Francais", 'fr-FR')
        self._radio(self.lang_var, "Anglais",  'en-US')

        self._section("Mode de transcription")
        self.mode_var = tk.StringVar(value=self.settings.get('mode'))
        self._radio(self.mode_var, "Precis   (texte apres enregistrement)", 'precise')
        self._radio(self.mode_var, "Rapide  [BETA] - texte colle apres chaque phrase", 'fast')

        self._section("Silence auto-stop")
        row = tk.Frame(self.win, bg=UI['bg'])
        row.pack(anchor='w', padx=32, pady=8)
        tk.Label(row, text="Arreter apres", bg=UI['bg'], fg=UI['text'],
                 font=('Segoe UI', 10)).pack(side='left')
        self.silence_var = tk.IntVar(value=int(self.settings.get('silence_sec') or 0))
        tk.Spinbox(row, from_=0, to=10, width=4, textvariable=self.silence_var,
                   bg=UI['bg2'], fg=UI['text'], buttonbackground=UI['bg2'],
                   insertbackground=UI['text'], font=('Segoe UI', 11),
                   relief='flat').pack(side='left', padx=8)
        tk.Label(row, text="sec de silence  (0 = desactive)",
                 bg=UI['bg'], fg=UI['text2'], font=('Segoe UI', 9)).pack(side='left')

        self._section("Raccourci clavier")
        row2 = tk.Frame(self.win, bg=UI['bg'])
        row2.pack(anchor='w', padx=32, pady=6)
        tk.Label(row2, text="Raccourci :", bg=UI['bg'], fg=UI['text2'],
                 font=('Segoe UI', 9)).pack(side='left')
        self.hotkey_var = tk.StringVar(value=self.settings.get('hotkey'))
        tk.Entry(row2, textvariable=self.hotkey_var, width=22,
                 bg=UI['bg2'], fg=UI['text'], insertbackground=UI['text'],
                 font=('Segoe UI', 10), relief='flat', bd=4
                 ).pack(side='left', padx=(8, 0))
        tk.Label(self.win, text="ex: ctrl+alt+space  |  ctrl+shift+v  |  f9",
                 bg=UI['bg'], fg=UI['text2'], font=('Segoe UI', 8)
                 ).pack(anchor='w', padx=32)

        tk.Button(self.win, text="Confirmer",
                  bg=UI['btn'], fg='white', font=('Segoe UI', 11, 'bold'),
                  relief='flat', padx=30, pady=10, cursor='hand2',
                  command=self._save).pack(pady=22)

    def _save(self):
        self.settings.set('language',    self.lang_var.get())
        self.settings.set('mode',        self.mode_var.get())
        try:
            self.settings.set('silence_sec', int(self.silence_var.get()))
        except Exception:
            self.settings.set('silence_sec', 2)
        hk = self.hotkey_var.get().strip().lower()
        if hk:
            self.settings.set('hotkey', hk)
        self.on_save_cb()
        self.win.destroy()


# ══════════════════════════════════════════════════════════════
#  FENÊTRE HISTORIQUE
# ══════════════════════════════════════════════════════════════
class HistoryWindow:
    def __init__(self, parent, history, paste_cb):
        self.history  = history
        self.paste_cb = paste_cb
        self.win = tk.Toplevel(parent)
        self.win.title("Historique  -  VocalType")
        self.win.configure(bg=UI['bg'])
        self.win.wm_attributes('-topmost', True)
        sw = self.win.winfo_screenwidth()
        sh = self.win.winfo_screenheight()
        w, h = 430, 420
        self.win.geometry(f"{w}x{h}+{(sw-w)//2}+{(sh-h)//2}")
        self._build()
        self._refresh()

    def _build(self):
        tk.Label(self.win, text="Historique", bg=UI['bg'], fg=UI['text'],
                 font=('Segoe UI', 13, 'bold')).pack(pady=(18, 8))
        frame = tk.Frame(self.win, bg=UI['bg'])
        frame.pack(fill='both', expand=True, padx=16, pady=4)
        sb = tk.Scrollbar(frame)
        sb.pack(side='right', fill='y')
        self.listbox = tk.Listbox(frame, bg=UI['bg2'], fg=UI['text'],
                                  font=('Segoe UI', 10),
                                  selectbackground=UI['accent'],
                                  selectforeground='white',
                                  borderwidth=0, highlightthickness=0,
                                  activestyle='none',
                                  yscrollcommand=sb.set)
        self.listbox.pack(fill='both', expand=True)
        sb.config(command=self.listbox.yview)

        tk.Label(self.win, text="Selectionne un texte puis clique Re-coller",
                 bg=UI['bg'], fg=UI['text2'], font=('Segoe UI', 8)
                 ).pack(pady=(4, 0))

        bf = tk.Frame(self.win, bg=UI['bg'])
        bf.pack(pady=10)
        for label, cmd, color in [
            ("Re-coller", self._repaste, UI['btn']),
            ("Vider",     self._clear,   '#7f1d1d'),
        ]:
            tk.Button(bf, text=label, command=cmd, bg=color, fg='white',
                      font=('Segoe UI', 9, 'bold'), relief='flat',
                      padx=14, pady=6, cursor='hand2').pack(side='left', padx=6)

    def _refresh(self):
        self.listbox.delete(0, 'end')
        for ts, text in self.history.all():
            preview = text[:55] + ('...' if len(text) > 55 else '')
            self.listbox.insert('end', f"  {ts}   {preview}")

    def _repaste(self):
        sel = self.listbox.curselection()
        if not sel:
            return
        items = self.history.all()
        if sel[0] >= len(items):
            return
        _, text = items[sel[0]]
        # IMPORTANT : fermer la fenetre d'abord pour que l'app
        # precedente reprenne le focus AVANT d'envoyer Ctrl+V
        self.win.withdraw()
        def _do_paste():
            time.sleep(0.6)   # Attendre que la fenetre precedente reprenne le focus
            self.paste_cb(text)
        threading.Thread(target=_do_paste, daemon=True).start()

    def _clear(self):
        self.history.clear()
        self._refresh()


# ══════════════════════════════════════════════════════════════
#  APPLICATION PRINCIPALE
# ══════════════════════════════════════════════════════════════
class VocalType:
    def __init__(self):
        self.recording       = False
        self._audio_lock     = threading.Lock()
        self._paste_lock     = threading.Lock()
        self.audio_data      = []
        self._vad_phrase_buf = []
        self.stream          = None
        self.recognizer      = sr.Recognizer()
        self.tray            = None
        self._last_sound_t   = time.time()
        self._is_mini        = True
        self._is_ready       = False   # Pilule visible mais pas encore en ecoute
        self.settings        = Settings()
        self.history         = History()
        self._current_hotkey = self.settings.get('hotkey') or DEFAULT_HOTKEY

        self._setup_gui()       # GUI d'abord (self.root doit exister avant le tray)
        self._setup_tray()
        self._setup_hotkey()
        self.root.mainloop()

        if self.tray:
            self.tray.stop()

    # ── Icône PIL ─────────────────────────────────────────────

    def _make_icon(self, size=64):
        img = Image.new('RGBA', (size, size), (0, 0, 0, 0))
        d, cx, fg, mw = ImageDraw.Draw(img), size//2, (165,180,252,255), 9
        d.ellipse([0, 0, size, size],            fill=(26,26,46,255))
        d.ellipse([cx-mw, 10, cx+mw, 10+mw*2],  fill=fg)
        d.rectangle([cx-mw, 10+mw, cx+mw, 34],  fill=fg)
        d.ellipse([cx-mw, 34-mw, cx+mw, 34+mw], fill=fg)
        d.arc([cx-14, 28, cx+14, 48], 0, 180,   fill=fg, width=3)
        d.line([cx, 48, cx, 56],                 fill=fg, width=3)
        d.line([cx-9, 56, cx+9, 56],             fill=fg, width=3)
        return img

    def _save_ico(self):
        path = os.path.join(_DIR, 'icon.ico')
        if not os.path.exists(path):
            try:
                self._make_icon(64).save(path, format='ICO',
                                         sizes=[(64,64),(32,32),(16,16)])
            except Exception:
                pass
        return path

    # ── GUI (pilule + mini dot) ───────────────────────────────

    def _setup_gui(self):
        self.root = tk.Tk()
        self.root.title(APP_NAME)
        self.root.overrideredirect(True)
        self.root.wm_attributes('-topmost', True)
        self.root.wm_attributes('-transparentcolor', _TRANSP)
        self.root.config(bg=_TRANSP)

        sw = self.root.winfo_screenwidth()
        # Demarrer en mode MINI (petit point discret)
        self.PW, self.PH = PW_MINI, PH_MINI
        self._is_mini = True
        x = (sw - PW_MINI) // 2
        self.root.geometry(f"{PW_MINI}x{PH_MINI}+{x}+12")

        self.cv = tk.Canvas(self.root, width=PW_MINI, height=PH_MINI,
                            bg=_TRANSP, highlightthickness=0)
        self.cv.pack()
        self.cv.bind('<Button-1>',        self._drag_start)
        self.cv.bind('<B1-Motion>',       self._drag_move)
        self.cv.bind('<ButtonRelease-1>', self._click)
        self.cv.bind('<Button-3>',        self._right_click)
        self.cv.bind('<Double-Button-1>', self._dbl_click)
        self._dx = self._dy = 0
        self.root.withdraw()   # Invisible au demarrage

    # ── Dessin ────────────────────────────────────────────────

    def _draw_dot(self):
        """Mini point discret : fond sombre + petit cercle violet."""
        bg, fg = _STATES['ready']
        self.cv.delete('all')
        self.cv.create_oval(0, 0, PW_MINI, PH_MINI, fill=bg, outline='')
        c = PH_MINI // 2
        self.cv.create_oval(c-5, c-5, c+5, c+5, fill=fg, outline='')

    def _draw(self, text, state='ready'):
        """Pilule pleine avec texte."""
        bg, fg = _STATES.get(state, _STATES['ready'])
        w, h, r = self.PW, self.PH, self.PH // 2
        self.cv.delete('all')
        self.cv.create_oval(0,   0, h,   h,  fill=bg, outline='')
        self.cv.create_oval(w-h, 0, w,   h,  fill=bg, outline='')
        self.cv.create_rectangle(r, 0, w-r, h, fill=bg, outline='')
        px, py = r+3, h//2
        self.cv.create_oval(px-4, py-4, px+4, py+4, fill=fg, outline='')
        self.cv.create_text(w//2+6, h//2, text=text, fill=fg,
                            font=('Segoe UI', 9, 'bold'), anchor='center')

    # ── Redimensionnement mini <-> full ───────────────────────

    def _go_mini(self):
        """Cacher completement la fenetre quand inactif."""
        if self._is_mini:
            return
        self._is_mini = True
        self.root.withdraw()

    def _go_full(self, text, state):
        """Faire apparaitre la pilule (ecoute / traitement)."""
        sw = self.root.winfo_screenwidth()
        w  = PW_IDLE if state == 'idle' else PW_FULL
        if self._is_mini:
            self.PW, self.PH = w, PH_FULL
            self._is_mini = False
            x = (sw - w) // 2
            self.cv.config(width=w, height=PH_FULL)
            self.root.geometry(f"{w}x{PH_FULL}+{x}+12")
            self.root.deiconify()
        elif self.PW != w:
            x = self.root.winfo_x() + self.PW // 2 - w // 2
            self.PW = w
            self.cv.config(width=w, height=PH_FULL)
            self.root.geometry(f"{w}x{PH_FULL}+{x}+12")
        self._draw(text, state)

    def _set_status(self, text, state='ready'):
        def update():
            if state == 'ready':
                self._go_mini()
            else:
                self._go_full(text, state)
        self.root.after(0, update)

    # ── Interactions souris ───────────────────────────────────

    def _drag_start(self, e):
        self._dx, self._dy = e.x, e.y

    def _drag_move(self, e):
        x = self.root.winfo_x() + e.x - self._dx
        y = self.root.winfo_y() + e.y - self._dy
        self.root.geometry(f"+{x}+{y}")

    def _click(self, e):
        """Clic simple : lance l'enregistrement si la pilule est en mode idle."""
        if abs(e.x - self._dx) < 6 and abs(e.y - self._dy) < 6:
            if self._is_ready and not self.recording:
                self._is_ready = False
                self._start_recording()

    def _dbl_click(self, e):
        self._toggle()

    def _right_click(self, e):
        m = tk.Menu(self.root, tearoff=0)
        m.add_command(label="Parametres",  command=self._open_settings)
        m.add_command(label="Historique",  command=self._open_history)
        m.add_separator()
        m.add_command(label="Quitter",     command=self._quit)
        m.tk_popup(e.x_root, e.y_root)

    def _quit(self):
        if self.tray:
            self.tray.stop()
        self.root.quit()

    # ── System tray ───────────────────────────────────────────

    def _setup_tray(self):
        try:
            self._save_ico()
            menu = pystray.Menu(
                pystray.MenuItem(APP_NAME, None, enabled=False),
                pystray.Menu.SEPARATOR,
                pystray.MenuItem('Parametres',
                                 lambda: self.root.after(0, self._open_settings)),
                pystray.MenuItem('Historique',
                                 lambda: self.root.after(0, self._open_history)),
                pystray.Menu.SEPARATOR,
                pystray.MenuItem('Demarrer avec Windows',
                                 self._toggle_autostart,
                                 checked=lambda _: self._is_autostart()),
                pystray.Menu.SEPARATOR,
                pystray.MenuItem('Quitter',
                                 lambda: self.root.after(0, self._quit))
            )
            self.tray = pystray.Icon(APP_NAME, self._make_icon(), APP_NAME, menu)
            self.tray.run_detached()
        except Exception as ex:
            print(f"Tray non disponible : {ex}")

    # ── Fenêtres ──────────────────────────────────────────────

    def _open_settings(self):
        try:
            if hasattr(self, '_sw') and self._sw.win.winfo_exists():
                self._sw.win.lift()
                return
        except Exception:
            pass
        self._sw = SettingsWindow(self.root, self.settings, self._on_settings_saved)

    def _open_history(self):
        try:
            if hasattr(self, '_hw') and self._hw.win.winfo_exists():
                self._hw.win.lift()
                return
        except Exception:
            pass
        self._hw = HistoryWindow(self.root, self.history, _paste_via_winapi)

    def _on_settings_saved(self):
        new_hk = self.settings.get('hotkey') or DEFAULT_HOTKEY
        if new_hk != self._current_hotkey:
            old_hk = self._current_hotkey
            try:
                test_id = keyboard.add_hotkey(new_hk, lambda: None)
                keyboard.remove_hotkey(test_id)
            except Exception:
                self.settings.set('hotkey', old_hk)
                return
            try:
                keyboard.remove_hotkey(old_hk)
            except Exception:
                pass
            try:
                keyboard.add_hotkey(new_hk, self._toggle, suppress=True)
                self._current_hotkey = new_hk
            except Exception:
                keyboard.add_hotkey(old_hk, self._toggle, suppress=True)
                self._current_hotkey = old_hk
                self.settings.set('hotkey', old_hk)

    # ── Autostart ─────────────────────────────────────────────

    def _run_cmd(self):
        if getattr(sys, 'frozen', False):
            return f'"{sys.executable}"'
        pw = sys.executable.replace('python.exe', 'pythonw.exe')
        if not os.path.exists(pw):
            pw = sys.executable
        return f'"{pw}" "{os.path.abspath(__file__)}"'

    def _is_autostart(self):
        try:
            k = winreg.OpenKey(winreg.HKEY_CURRENT_USER, REG_PATH)
            winreg.QueryValueEx(k, APP_NAME)
            winreg.CloseKey(k)
            return True
        except Exception:
            return False

    def _toggle_autostart(self):
        try:
            k = winreg.OpenKey(winreg.HKEY_CURRENT_USER, REG_PATH, 0,
                               winreg.KEY_SET_VALUE)
            if self._is_autostart():
                winreg.DeleteValue(k, APP_NAME)
            else:
                winreg.SetValueEx(k, APP_NAME, 0, winreg.REG_SZ, self._run_cmd())
            winreg.CloseKey(k)
        except Exception as ex:
            print(f"Autostart error : {ex}")

    # ── Raccourci global ──────────────────────────────────────

    def _setup_hotkey(self):
        hk = self.settings.get('hotkey') or DEFAULT_HOTKEY
        try:
            keyboard.add_hotkey(hk, self._toggle, suppress=True)
            self._current_hotkey = hk
        except Exception:
            try:
                keyboard.add_hotkey(DEFAULT_HOTKEY, self._toggle, suppress=True)
                self._current_hotkey = DEFAULT_HOTKEY
            except Exception:
                pass

    def _toggle(self):
        if self.recording:
            threading.Thread(target=self._stop_and_transcribe, daemon=True).start()
        elif self._is_ready:
            # Pilule deja visible -> lancer l'ecoute
            self._is_ready = False
            self._start_recording()
        else:
            # Premiere pression -> montrer la pilule avec instructions
            self._is_ready = True
            self._set_status(_LABELS['idle'], 'idle')
            # Disparait apres 6s si pas d'action
            self.root.after(6000, self._auto_hide_idle)

    def _auto_hide_idle(self):
        """Cache la pilule si toujours en idle apres 6s."""
        if self._is_ready and not self.recording:
            self._is_ready = False
            self._go_mini()

    # ── Enregistrement ────────────────────────────────────────

    def _start_recording(self):
        # Loguer le peripherique audio par defaut pour diagnostic
        try:
            dev = sd.query_devices(kind='input')
            log.info(f"Micro utilise : {dev.get('name','?')} "
                     f"(canaux={dev.get('max_input_channels','?')}, "
                     f"taux={int(dev.get('default_samplerate',0))} Hz)")
        except Exception as e:
            log.warning(f"Impossible de lire le peripherique audio : {e}")

        log.info("Debut enregistrement")
        try:
            winsound.Beep(880, 120)
        except Exception:
            pass
        with self._audio_lock:
            self.recording     = True
            self.audio_data    = []
            self._last_sound_t = time.time()
        self._set_status(_LABELS['recording'], 'recording')
        try:
            self.stream = sd.InputStream(
                samplerate=SAMPLE_RATE, channels=1, dtype='float32',
                callback=self._audio_cb)
            self.stream.start()
        except Exception as e:
            log.error(f"Micro non disponible : {e}")
            self.recording = False
            self._set_status('Micro non disponible', 'error')
            time.sleep(2)
            self._set_status(_LABELS['ready'], 'ready')
            return
        if self.settings.get('mode') == 'fast':
            self._vad_phrase_buf = []
            threading.Thread(target=self._vad_loop, daemon=True).start()
        if int(self.settings.get('silence_sec') or 0) > 0:
            threading.Thread(target=self._silence_monitor, daemon=True).start()

    def _audio_cb(self, indata, frames, t, status):
        if self.recording:
            chunk = indata.copy()
            with self._audio_lock:
                self.audio_data.append(chunk)
            rms = float(np.sqrt(np.mean(indata ** 2)))
            if rms > SILENCE_THRESHOLD:
                self._last_sound_t = time.time()

    def _silence_monitor(self):
        silence_sec = float(self.settings.get('silence_sec') or 2)
        min_chunks  = max(int(SAMPLE_RATE / 2048 * 1.5), 4)
        time.sleep(1.0)
        while self.recording:
            time.sleep(0.2)
            if not self.recording:
                break
            with self._audio_lock:
                n = len(self.audio_data)
            if n < min_chunks:
                continue
            if time.time() - self._last_sound_t >= silence_sec:
                threading.Thread(target=self._stop_and_transcribe,
                                 daemon=True).start()
                break

    # ── Mode rapide avec VAD (coupe aux pauses naturelles) ────

    def _vad_loop(self):
        """Mode rapide hybride : coupe a la pause naturelle OU apres VAD_MAX_SEC."""
        was_speaking  = False
        phrase_start  = None

        while self.recording:
            time.sleep(0.05)

            with self._audio_lock:
                self._vad_phrase_buf.extend(self.audio_data)
                self.audio_data = []

            silence_dur = time.time() - self._last_sound_t
            is_speaking = silence_dur < VAD_PAUSE_SEC

            if is_speaking and not was_speaking:
                phrase_start = time.time()   # Debut de phrase
            if is_speaking:
                was_speaking = True

            phrase_age  = (time.time() - phrase_start) if phrase_start else 0
            force_cut   = was_speaking and phrase_age >= VAD_MAX_SEC
            natural_cut = was_speaking and not is_speaking

            if (force_cut or natural_cut) and self._vad_phrase_buf:
                with self._audio_lock:
                    phrase = list(self._vad_phrase_buf)
                    self._vad_phrase_buf = []

                if phrase:
                    audio = np.concatenate(phrase).flatten()
                    if len(audio) / SAMPLE_RATE >= VAD_MIN_SEC:
                        threading.Thread(
                            target=self._paste_fast_chunk,
                            args=(phrase,),
                            daemon=True
                        ).start()

                if force_cut:
                    phrase_start = time.time()   # Continuer a parler, timer remis a zero
                else:
                    was_speaking = False
                    phrase_start = None

    def _paste_fast_chunk(self, chunks):
        """Transcrit et colle une phrase en mode rapide (serialise pour eviter conflits clipboard)."""
        text = self._do_transcribe(chunks)
        if text:
            self.history.add(text)
            with self._paste_lock:
                _paste_via_winapi(text, add_space=True)
            preview = text[:28] + ('...' if len(text) > 28 else '')
            self._set_status(f'"{preview}"', 'success')
            time.sleep(0.8)
            if self.recording:
                self._set_status(_LABELS['recording'], 'recording')

    # ── Transcription (Google Speech) ─────────────────────────

    def _do_transcribe(self, audio_chunks):
        if not audio_chunks:
            return None
        tmp = None
        try:
            audio = np.concatenate(audio_chunks).flatten()
            duree = len(audio) / SAMPLE_RATE
            rms   = float(np.sqrt(np.mean(audio ** 2)))
            log.info(f"Audio capture : {duree:.1f}s  RMS={rms:.5f}  "
                     f"({'OK' if rms > 0.005 else 'TROP SILENCIEUX - mauvais micro?'})")
            # Ignorer si audio trop court (moins de 0.4s)
            if len(audio) < SAMPLE_RATE * 0.4:
                log.warning("Audio trop court (<0.4s), ignore")
                return None
            audio_i16 = (np.clip(audio, -1.0, 1.0) * 32767).astype(np.int16)
            fd, tmp = tempfile.mkstemp(suffix='.wav')
            os.close(fd)
            write_wav(tmp, SAMPLE_RATE, audio_i16)
            lang = self.settings.get('language')
            with sr.AudioFile(tmp) as source:
                recorded = self.recognizer.record(source)
            result = self.recognizer.recognize_google(recorded, language=lang)
            log.info(f"Transcription OK ({lang}) : {result[:60]}")
            return result
        except sr.UnknownValueError:
            log.info("Google : aucune parole detectee")
            return None
        except sr.RequestError as e:
            log.error(f"Erreur API Google (reseau?) : {e}")
            return None
        except Exception as e:
            log.error(f"Erreur transcription inattendue : {e}")
            return None
        finally:
            if tmp and os.path.exists(tmp):
                try:
                    os.unlink(tmp)
                except Exception:
                    pass

    def _stop_and_transcribe(self):
        with self._audio_lock:
            self.recording       = False
            chunks               = list(self._vad_phrase_buf) + list(self.audio_data)
            self.audio_data      = []
            self._vad_phrase_buf = []

        if self.stream:
            try:
                self.stream.stop()
                self.stream.close()
            except Exception as e:
                log.warning(f"Erreur fermeture stream audio : {e}")
            self.stream = None

        try:
            winsound.Beep(660, 120)
        except Exception:
            pass

        # Mode rapide : coller aussi les dernieres donnees accumulees
        if self.settings.get('mode') == 'fast':
            if chunks:
                text = self._do_transcribe(chunks)
                if text:
                    self.history.add(text)
                    _paste_via_winapi(text, add_space=True)
            self._set_status(_LABELS['ready'], 'ready')
            return

        # Mode precis : une seule transcription a la fin
        self._set_status(_LABELS['processing'], 'processing')
        try:
            text = self._do_transcribe(chunks)
            if text:
                self.history.add(text)
                _paste_via_winapi(text)
                preview = text[:28] + ('...' if len(text) > 28 else '')
                self._set_status(f'"{preview}"', 'success')
                time.sleep(2)
            else:
                self._set_status(_LABELS['error'], 'error')
                time.sleep(2)
        except Exception as e:
            log.error(f"Erreur transcription precise : {e}")
            self._set_status(_LABELS['error'], 'error')
            time.sleep(2)
        finally:
            self._set_status(_LABELS['ready'], 'ready')


# ─────────────────────────────────────────────────────────────
if __name__ == '__main__':
    log.info("=== VocalType demarre ===")
    print("-" * 50)
    print("  VocalType - Dictee vocale universelle")
    print("-" * 50)
    print(f"  Raccourci : {DEFAULT_HOTKEY.upper()}")
    print("  Double-clic ou raccourci pour dicter")
    print("  Clic droit sur le point -> menu")
    print("-" * 50)
    VocalType()
