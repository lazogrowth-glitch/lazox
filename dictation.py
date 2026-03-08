#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
VocalType - Dictee vocale universelle pour Windows
Raccourci par defaut : Ctrl+Shift+Espace
"""

import os
import io
import re
import sys
import json
import time
import ctypes
import threading
import tempfile
import winreg
import winsound
import logging
import logging.handlers
import webbrowser
import urllib.request
import urllib.error
import queue as _queue
from datetime import datetime, timedelta

# ── Instance unique ───────────────────────────────────────────
_MUTEX = ctypes.windll.kernel32.CreateMutexW(None, True, "VocalType_SingleInstance_v1")
if ctypes.windll.kernel32.GetLastError() == 183:
    sys.exit(0)

try:
    import numpy as np
    import sounddevice as sd
    from scipy.io.wavfile import write as write_wav
    import pyperclip
    import keyboard
    import tkinter as tk
    from PIL import Image, ImageDraw
    import pystray
except ImportError as e:
    import tkinter as tk
    from tkinter import messagebox
    _root = tk.Tk()
    _root.withdraw()
    messagebox.showerror(
        "VocalType - Module manquant",
        f"Module manquant : {e}\n\n"
        "Lance install.bat pour installer toutes les dependances."
    )
    sys.exit(1)

# ── Chemins (fonctionne en .py ET en .exe PyInstaller) ────────
if getattr(sys, 'frozen', False):
    _DIR = os.path.dirname(sys.executable)
else:
    _DIR = os.path.dirname(os.path.abspath(__file__))

SETTINGS_PATH = os.path.join(_DIR, 'settings.json')
HISTORY_PATH  = os.path.join(_DIR, 'history.json')
LOG_PATH      = os.path.join(_DIR, 'vocaltype.log')
AUTH_PATH     = os.path.join(_DIR, 'auth.json')

# ── Logging avec rotation (max 2 Mo, 3 fichiers) ─────────────
_log_handler = logging.handlers.RotatingFileHandler(
    LOG_PATH, maxBytes=2*1024*1024, backupCount=3, encoding='utf-8'
)
_log_handler.setFormatter(logging.Formatter(
    '%(asctime)s [%(levelname)s] %(message)s', datefmt='%Y-%m-%d %H:%M:%S'
))
logging.getLogger().setLevel(logging.INFO)
logging.getLogger().addHandler(_log_handler)
log = logging.getLogger('VocalType')

# ── Constantes ────────────────────────────────────────────────
APP_NAME          = 'VocalType'
DEFAULT_HOTKEY    = 'ctrl+shift+space'
SAMPLE_RATE       = 16000
_TRANSP           = '#010101'
VAD_PAUSE_SEC     = 0.35       # Mode rapide : pause de 350ms = fin de phrase naturelle
VAD_MIN_SEC       = 0.4        # Duree minimale d'audio pour tenter la transcription
VAD_MAX_SEC       = 3.5        # Duree max avant envoi force (si parole continue)
PRECISE_BLOCK_SEC = 10         # Mode precis : taille des blocs audio (secondes)
MAX_HISTORY       = 50
SILENCE_THRESHOLD = 0.012
BACKEND_URL       = 'https://lazox-production.up.railway.app'

# Tailles de la pilule
PW_MINI, PH_MINI = 22, 22    # Mini : petit point discret en haut de l'ecran
PW_FULL, PH_FULL = 180, 30   # Full : pilule avec texte pendant l'ecoute

_STATES = {
    'ready':      ('#1a1a2e', '#a5b4fc'),
    'idle':       ('#1a1a2e', '#a5b4fc'),
    'recording':  ('#3b0000', '#fca5a5'),
    'preview':    ('#1a1a3e', '#c4b5fd'),  # Mode rapide : texte provisoire en violet clair
    'processing': ('#2d1a00', '#fde68a'),
    'success':    ('#002d0f', '#86efac'),
    'error':      ('#2d0000', '#fca5a5'),
}
_LABELS_FR = {
    'ready':      'VocalType',
    'idle':       'Clic -> Parler  |  Clic droit -> Menu',
    'recording':  'Ecoute...',
    'processing': 'Transcription...',
    'error':      'Erreur - reessaie',
}
_LABELS_EN = {
    'ready':      'VocalType',
    'idle':       'Click -> Speak  |  Right click -> Menu',
    'recording':  'Listening...',
    'processing': 'Transcribing...',
    'error':      'Error - try again',
}
# _LABELS est assigne dynamiquement selon la langue dans VocalType.__init__
_LABELS = _LABELS_FR  # defaut FR, remplace au lancement
PW_IDLE  = 260   # Pilule idle plus large pour afficher les instructions
PW_PREVIEW = 420  # Pilule elargie pour afficher le texte provisoire en mode rapide
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
    'green':   '#86efac',
    'red':     '#fca5a5',
}


# ── API Helper ────────────────────────────────────────────────
def _api(endpoint, data=None, token=None):
    """Appel HTTP vers le backend Railway.
    data=None => GET, data=dict => POST JSON.
    Retourne (dict|None, code_http).  code_http=0 si erreur reseau."""
    url     = BACKEND_URL + endpoint
    headers = {'Content-Type': 'application/json'}
    if token:
        headers['Authorization'] = f'Bearer {token}'
    body   = json.dumps(data).encode('utf-8') if data is not None else None
    method = 'POST' if data is not None else 'GET'
    try:
        req = urllib.request.Request(url, data=body, headers=headers, method=method)
        with urllib.request.urlopen(req, timeout=15) as r:
            return json.loads(r.read().decode('utf-8')), r.status
    except urllib.error.HTTPError as e:
        try:
            return json.loads(e.read().decode('utf-8')), e.code
        except Exception:
            return {'error': str(e)}, e.code
    except Exception as e:
        log.error(f"API {endpoint} erreur reseau : {e}")
        return None, 0


# ── Commandes IA vocales ───────────────────────────────────────
# Mots-cles detectes au debut de la phrase transcrite
# Format : 'declencheur vocal' -> 'commande interne'
_AI_COMMANDS = {
    # ── Francais ──────────────────────────────────────────────
    'corrige':              'correct',
    'corrige ça':           'correct',
    'corrige ce texte':     'correct',
    'reformule':            'rephrase',
    'reformule ça':         'rephrase',
    'reformule ce texte':   'rephrase',
    'traduis en anglais':   'translate_en',
    'améliore':             'improve',
    'améliore ça':          'improve',
    'améliore ce texte':    'improve',
    'résume':               'summarize',
    'résume ça':            'summarize',
    'résume ce texte':      'summarize',

    # ── Anglais ───────────────────────────────────────────────
    'correct':                  'correct',
    'correct this':             'correct',
    'fix this':                 'correct',
    'fix that':                 'correct',
    'rephrase':                 'rephrase',
    'rephrase this':            'rephrase',
    'rewrite':                  'rephrase',
    'rewrite this':             'rephrase',
    'translate to english':     'translate_en',
    'translate into english':   'translate_en',
    'improve':                  'improve',
    'improve this':             'improve',
    'make this better':         'improve',
    'make it better':           'improve',
    'make this shorter':        'shorten',
    'shorten this':             'shorten',
    'make it shorter':          'shorten',
    'raccourcis':               'shorten',
    'raccourcis ça':            'shorten',
    'summarize':                'summarize',
    'summarize this':           'summarize',
    'sum up':                   'summarize',
}

def _detect_ai_command(text):
    """Detecte si le texte commence par une commande IA.
    Retourne (commande, texte_sans_commande) ou (None, text) si pas de commande."""
    if not text:
        return None, text
    lower = text.lower().strip()
    for trigger, command in sorted(_AI_COMMANDS.items(), key=lambda x: len(x[0]), reverse=True):
        if lower.startswith(trigger):
            remaining = text[len(trigger):].strip()
            remaining = re.sub(r'^[,:\-–]\s*', '', remaining).strip()
            if remaining:
                return command, remaining
    return None, text


# ── Commandes d'edition vocale ────────────────────────────────
# Ces commandes modifient le texte deja presente dans le champ actif
_EDIT_COMMANDS = {
    # Anglais
    'delete last sentence':   'delete_last',
    'remove last sentence':   'delete_last',
    'replace last sentence':  'replace_last',
    # Francais
    'supprime la dernière phrase':  'delete_last',
    'efface la dernière phrase':    'delete_last',
    'remplace la dernière phrase':  'replace_last',
    'supprime la derniere phrase':  'delete_last',
    'efface la derniere phrase':    'delete_last',
    'remplace la derniere phrase':  'replace_last',
}

def _detect_edit_command(text):
    """Detecte si le texte commence par une commande d'edition.
    Retourne (commande, nouveau_texte_ou_None) ou (None, text) si pas de commande.
    Pour replace_last : retourne le texte de remplacement.
    Pour delete_last  : retourne None comme payload."""
    if not text:
        return None, text
    lower = text.lower().strip()
    for trigger, command in sorted(_EDIT_COMMANDS.items(), key=lambda x: len(x[0]), reverse=True):
        if lower.startswith(trigger):
            remaining = text[len(trigger):].strip()
            remaining = re.sub(r'^[,:\-–]\s*', '', remaining).strip()
            if command == 'replace_last' and not remaining:
                # replace sans texte de remplacement -> on ignore
                return None, text
            return command, remaining or None
    return None, text


# ── Commandes ASK (ouvrir chatbot dans navigateur) ────────────
_ASK_TARGETS = {
    # ── ChatGPT (EN) ──────────────────────────────────────────
    'ask chatgpt':          ('chatgpt',  'https://chatgpt.com'),
    'ask chat gpt':         ('chatgpt',  'https://chatgpt.com'),
    'ask gpt':              ('chatgpt',  'https://chatgpt.com'),
    'ask openai':           ('chatgpt',  'https://chatgpt.com'),
    'ask chat':             ('chatgpt',  'https://chatgpt.com'),
    # ── ChatGPT (FR) ──────────────────────────────────────────
    'demande à chatgpt':    ('chatgpt',  'https://chatgpt.com'),
    'demande a chatgpt':    ('chatgpt',  'https://chatgpt.com'),
    'demande à chat gpt':   ('chatgpt',  'https://chatgpt.com'),
    'demande a chat gpt':   ('chatgpt',  'https://chatgpt.com'),
    'demande à gpt':        ('chatgpt',  'https://chatgpt.com'),
    'demande a gpt':        ('chatgpt',  'https://chatgpt.com'),
    'demande à chat':       ('chatgpt',  'https://chatgpt.com'),
    'demande a chat':       ('chatgpt',  'https://chatgpt.com'),
    # ── Claude (EN) ───────────────────────────────────────────
    'ask claude':           ('claude',   'https://claude.ai'),
    'ask claude ai':        ('claude',   'https://claude.ai'),
    # ── Claude (FR) ───────────────────────────────────────────
    'demande à claude':     ('claude',   'https://claude.ai'),
    'demande a claude':     ('claude',   'https://claude.ai'),
    # ── Gemini (EN) ───────────────────────────────────────────
    'ask gemini':           ('gemini',   'https://gemini.google.com'),
    'ask google ai':        ('gemini',   'https://gemini.google.com'),
    'ask google':           ('gemini',   'https://gemini.google.com'),
    # ── Gemini (FR) ───────────────────────────────────────────
    'demande à gemini':     ('gemini',   'https://gemini.google.com'),
    'demande a gemini':     ('gemini',   'https://gemini.google.com'),
    'demande à google':     ('gemini',   'https://gemini.google.com'),
    'demande a google':     ('gemini',   'https://gemini.google.com'),
    # ── DeepSeek (EN) ─────────────────────────────────────────
    'ask deepseek':         ('deepseek', 'https://chat.deepseek.com'),
    'ask deep seek':        ('deepseek', 'https://chat.deepseek.com'),
    'ask deep':             ('deepseek', 'https://chat.deepseek.com'),
    # ── DeepSeek (FR) ─────────────────────────────────────────
    'demande à deepseek':   ('deepseek', 'https://chat.deepseek.com'),
    'demande a deepseek':   ('deepseek', 'https://chat.deepseek.com'),
    'demande à deep seek':  ('deepseek', 'https://chat.deepseek.com'),
    'demande a deep seek':  ('deepseek', 'https://chat.deepseek.com'),
    'demande à deep':       ('deepseek', 'https://chat.deepseek.com'),
    'demande a deep':       ('deepseek', 'https://chat.deepseek.com'),
}

# Corrections phonetiques residuelles que Deepgram peut encore rater
# On garde seulement les variantes plausibles, pas les hacks Google
_ASK_PHONETIC_FIXES = {
    # ChatGPT - Deepgram le reconnait bien, mais au cas ou
    "chat gpt":     'ask chatgpt',
    "tchat gpt":    'ask chatgpt',
    # Claude - tres bien reconnu par Deepgram, rien a corriger
    # Gemini - tres bien reconnu par Deepgram
    'gemeni':       'ask gemini',
    # DeepSeek - peut encore poser probleme
    'deep sik':     'ask deepseek',
    'deep sec':     'ask deepseek',
}

def _normalize_ask_text(text):
    """Normalisation legere pour les commandes ask :
    - minuscules
    - suppression ponctuation finale
    - corrections phonetiques des noms de chatbots
    PAS de _clean_text() pour eviter l'ajout de point final.
    Retourne (texte_normalise, longueur_originale_consommee)."""
    if not text:
        return text, 0
    t = text.lower().strip()
    # Supprimer ponctuation finale seulement
    t = re.sub(r'[.!?,;:]+$', '', t).strip()
    # Corrections phonetiques : cherche si le debut correspond
    for bad, good in sorted(_ASK_PHONETIC_FIXES.items(), key=lambda x: len(x[0]), reverse=True):
        if t.startswith(bad):
            # Longueur originale consommee pour le fix
            return good + t[len(bad):], len(bad)
    return t, 0

def _detect_ask_command(raw_text):
    """Detecte si le texte commence par une commande ask/demande.
    Travaille sur le texte brut (avant _clean_text).
    Retourne (target_key, url, message) ou (None, None, raw_text)."""
    if not raw_text:
        return None, None, raw_text
    normalized, phonetic_len = _normalize_ask_text(raw_text)
    # Trier du plus long au plus court pour eviter faux match
    for trigger, (target, url) in sorted(_ASK_TARGETS.items(), key=lambda x: len(x[0]), reverse=True):
        if normalized.startswith(trigger):
            # Calculer combien de chars originaux correspond au trigger
            if phonetic_len > 0:
                # Le debut a ete corrige phonetiquement : skip les chars originaux
                original_skip = phonetic_len
            else:
                original_skip = len(trigger)
            message = raw_text[original_skip:].strip()
            message = re.sub(r'^[,:\-–]\s*', '', message).strip()
            # Supprimer ponctuation finale ajoutee par Google
            message = re.sub(r'[.!?]+$', '', message).strip()
            if message:
                log.info(f"[Ask] Commande detectee : {target} | msg={message[:50]!r}")
                return target, url, message
    return None, None, raw_text


def _remove_last_sentence(text):
    """Supprime la derniere phrase d'un texte.
    Gere : ponctuation propre, texte sans ponctuation, abreviations courantes.
    Retourne le texte sans la derniere phrase, ou '' si une seule phrase."""
    if not text:
        return text

    text = text.strip()

    # Abreviations courantes a ne pas confondre avec une fin de phrase
    _ABBREV = r'(?<!\bMr)(?<!\bMrs)(?<!\bMs)(?<!\bDr)(?<!\bProf)(?<!\bSt)' \
              r'(?<!\bvs)(?<!\betc)(?<!\bapx)(?<!\bno)'

    # Decouper sur . ! ? suivi d'un espace ou fin, en evitant les abreviations
    pattern = _ABBREV + r'(?<=[.!?])\s+'
    sentences = re.split(pattern, text)

    # Nettoyer les segments vides
    sentences = [s.strip() for s in sentences if s.strip()]

    if len(sentences) <= 1:
        # Pas de ponctuation propre -> chercher la derniere ligne non vide
        lines = [l.strip() for l in text.splitlines() if l.strip()]
        if len(lines) <= 1:
            return ''
        return '\n'.join(lines[:-1]).strip()

    result = ' '.join(sentences[:-1]).strip()
    return result


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
        time.sleep(2.5)
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
            pass
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
#  AUTH MANAGER
# ══════════════════════════════════════════════════════════════
class AuthManager:
    """Gere le token JWT et la verification d'abonnement."""

    def __init__(self):
        self.token         = None
        self.email         = None
        self.last_verified = None   # ISO datetime UTC
        self._load()

    def _load(self):
        try:
            with open(AUTH_PATH, 'r', encoding='utf-8') as f:
                d = json.load(f)
                self.token         = d.get('token')
                self.email         = d.get('email')
                self.last_verified = d.get('last_verified')
        except Exception:
            pass

    def save(self, token, email):
        self.token         = token
        self.email         = email
        self.last_verified = datetime.utcnow().isoformat()
        try:
            with open(AUTH_PATH, 'w', encoding='utf-8') as f:
                json.dump({
                    'token':         token,
                    'email':         email,
                    'last_verified': self.last_verified,
                }, f)
        except Exception as e:
            log.error(f"Auth save error : {e}")

    def clear(self):
        self.token = self.email = self.last_verified = None
        try:
            os.unlink(AUTH_PATH)
        except Exception:
            pass

    def check_subscription(self):
        """Verifie l'abonnement aupres du backend.
        Retourne (is_active|None, status|None, error_str|None).
        is_active=None signifie erreur reseau."""
        if not self.token:
            return False, None, 'no_token'
        data, code = _api('/me', token=self.token)
        if code == 0:
            return None, None, 'network'
        if code == 401:
            return False, None, 'expired'
        if data and 'active' in data:
            return data['active'], data.get('status'), None
        return False, None, 'unknown'


# ══════════════════════════════════════════════════════════════
#  FENÊTRE CONNEXION / INSCRIPTION
# ══════════════════════════════════════════════════════════════
class LoginWindow:
    """Fenetre de connexion/inscription.  Apres wait_window(), verifier .result."""

    def __init__(self, parent, auth_manager, lang='fr'):
        self.auth   = auth_manager
        self.lang   = lang  # 'fr' ou 'en'
        self.result = None   # 'ok' | 'quit'
        self.active = False
        self.status = 'none'
        self.mode   = 'login'
        self._q     = _queue.Queue()   # thread-safe : le thread met, le main thread lit

        _t = self._t  # raccourci
        self.win = tk.Toplevel(parent)
        self.win.title(f"VocalType - {_t('Connexion', 'Sign in')}")
        self.win.resizable(False, False)
        self.win.configure(bg=UI['bg'])
        self.win.wm_attributes('-topmost', True)
        sw = self.win.winfo_screenwidth()
        sh = self.win.winfo_screenheight()
        w, h = 380, 490
        self.win.geometry(f"{w}x{h}+{(sw-w)//2}+{(sh-h)//2}")
        self.win.protocol('WM_DELETE_WINDOW', self._on_close)
        self._build()

    def _t(self, fr, en):
        """Retourne fr ou en selon la langue configuree."""
        return en if self.lang == 'en' else fr

    def _on_close(self):
        self.result = 'quit'
        self.win.destroy()

    def _build(self):
        t = self._t
        # En-tete
        tk.Label(self.win, text="🎤", bg=UI['bg'], fg=UI['accent'],
                 font=('Segoe UI', 36)).pack(pady=(26, 0))
        tk.Label(self.win, text="VocalType", bg=UI['bg'], fg=UI['text'],
                 font=('Segoe UI', 18, 'bold')).pack()
        self.subtitle = tk.Label(self.win,
                                  text=t("Connexion a votre compte", "Sign in to your account"),
                                  bg=UI['bg'], fg=UI['text2'],
                                  font=('Segoe UI', 10))
        self.subtitle.pack(pady=(2, 16))

        # Email
        tk.Label(self.win, text="Email", bg=UI['bg'], fg=UI['text2'],
                 font=('Segoe UI', 9)).pack(anchor='w', padx=44)
        self.email_var = tk.StringVar()
        self.email_entry = tk.Entry(
            self.win, textvariable=self.email_var,
            bg=UI['bg2'], fg=UI['text'], insertbackground=UI['text'],
            font=('Segoe UI', 11), relief='flat', bd=8, width=28)
        self.email_entry.pack(padx=44, pady=(2, 10), fill='x')
        self.email_entry.focus()

        # Mot de passe / Password
        tk.Label(self.win, text=t("Mot de passe", "Password"),
                 bg=UI['bg'], fg=UI['text2'],
                 font=('Segoe UI', 9)).pack(anchor='w', padx=44)
        self.pass_var = tk.StringVar()
        self.pass_entry = tk.Entry(
            self.win, textvariable=self.pass_var, show='*',
            bg=UI['bg2'], fg=UI['text'], insertbackground=UI['text'],
            font=('Segoe UI', 11), relief='flat', bd=8, width=28)
        self.pass_entry.pack(padx=44, pady=(2, 6), fill='x')
        self.pass_entry.bind('<Return>', lambda e: self._submit())

        # Message d'erreur
        self.err_var = tk.StringVar()
        tk.Label(self.win, textvariable=self.err_var,
                 bg=UI['bg'], fg=UI['red'],
                 font=('Segoe UI', 9), wraplength=300).pack(pady=(2, 4))

        # Bouton principal
        self.btn_text = tk.StringVar(value=t("Se connecter", "Sign in"))
        self.btn = tk.Button(
            self.win, textvariable=self.btn_text,
            bg=UI['btn'], fg='white',
            font=('Segoe UI', 11, 'bold'),
            relief='flat', padx=30, pady=10,
            cursor='hand2', command=self._submit)
        self.btn.pack(pady=8)

        # Lien bascule login <-> register
        self.toggle = tk.Label(
            self.win,
            text=t("Pas encore de compte ?  Creer un compte  ->",
                   "No account yet?  Create one  ->"),
            bg=UI['bg'], fg=UI['accent2'],
            font=('Segoe UI', 9), cursor='hand2')
        self.toggle.pack(pady=(4, 0))
        self.toggle.bind('<Button-1>', self._toggle_mode)

        # Mention prix
        tk.Label(self.win,
                 text=t("7 jours gratuits  *  5 $/mois (CAD) apres l'essai",
                        "7-day free trial  *  $5/month (CAD) after trial"),
                 bg=UI['bg'], fg=UI['text2'],
                 font=('Segoe UI', 8)).pack(pady=(18, 0))

    def _toggle_mode(self, _=None):
        t = self._t
        if self.mode == 'login':
            self.mode = 'register'
            self.win.title(f"VocalType - {t('Creer un compte', 'Create account')}")
            self.subtitle.config(text=t("Creer votre compte VocalType", "Create your VocalType account"))
            self.btn_text.set(t("Creer le compte", "Create account"))
            self.toggle.config(text=t("Deja un compte ?  Se connecter  ->",
                                      "Already have an account?  Sign in  ->"))
        else:
            self.mode = 'login'
            self.win.title(f"VocalType - {t('Connexion', 'Sign in')}")
            self.subtitle.config(text=t("Connexion a votre compte", "Sign in to your account"))
            self.btn_text.set(t("Se connecter", "Sign in"))
            self.toggle.config(text=t("Pas encore de compte ?  Creer un compte  ->",
                                      "No account yet?  Create one  ->"))
        self.err_var.set('')

    def _submit(self):
        t = self._t
        email    = self.email_var.get().strip().lower()
        password = self.pass_var.get()

        if not email or '@' not in email:
            self.err_var.set(t("Email invalide", "Invalid email"))
            return
        if len(password) < 6:
            self.err_var.set(t("Mot de passe trop court (min 6 caracteres)",
                               "Password too short (min 6 characters)"))
            return

        self.btn.config(state='disabled')
        self.btn_text.set(t('Connexion...', 'Signing in...') if self.mode == 'login'
                          else t('Creation...', 'Creating...'))
        self.err_var.set('')

        def _call():
            endpoint = '/register' if self.mode == 'register' else '/login'
            data, code = _api(endpoint, {'email': email, 'password': password})
            self._q.put((data, code, email))

        threading.Thread(target=_call, daemon=True).start()
        self.win.after(100, self._poll)

    def _poll(self):
        """Verifie la queue toutes les 100ms (main thread uniquement)."""
        try:
            data, code, email = self._q.get_nowait()
            self._handle(data, code, email)
        except _queue.Empty:
            self.win.after(100, self._poll)

    def _handle(self, data, code, email):
        t = self._t
        restore_lbl = t('Se connecter', 'Sign in') if self.mode == 'login' \
                      else t('Creer le compte', 'Create account')
        if code == 0:
            self.err_var.set(t("Serveur inaccessible. Verifie ta connexion.",
                               "Server unreachable. Check your connection."))
            self.btn.config(state='normal')
            self.btn_text.set(restore_lbl)
            return
        if code not in (200, 201):
            err = (data or {}).get('error', t('Erreur inconnue', 'Unknown error'))
            self.err_var.set(err)
            self.btn.config(state='normal')
            self.btn_text.set(restore_lbl)
            return

        token  = data.get('token', '')
        active = data.get('active', False)
        status = data.get('status', 'none')
        self.auth.save(token, email)
        self.active = active
        self.status = status
        self.result = 'ok'
        self.win.destroy()


# ══════════════════════════════════════════════════════════════
#  FENÊTRE ABONNEMENT
# ══════════════════════════════════════════════════════════════
class SubscribeWindow:
    """Fenetre pour demarrer l'essai Stripe ou verifier l'abonnement."""

    def __init__(self, parent, auth_manager, email=''):
        self.auth   = auth_manager
        self.email  = email or auth_manager.email or ''
        self.result = None   # 'ok' | 'quit'
        self._q     = _queue.Queue()

        self.win = tk.Toplevel(parent)
        self.win.title("VocalType - Abonnement")
        self.win.resizable(False, False)
        self.win.configure(bg=UI['bg'])
        self.win.wm_attributes('-topmost', True)
        sw = self.win.winfo_screenwidth()
        sh = self.win.winfo_screenheight()
        w, h = 400, 440
        self.win.geometry(f"{w}x{h}+{(sw-w)//2}+{(sh-h)//2}")
        self.win.protocol('WM_DELETE_WINDOW', self._on_close)
        self._build()

    def _on_close(self):
        self.result = 'quit'
        self.win.destroy()

    def _build(self):
        tk.Label(self.win, text="🚀", bg=UI['bg'], fg=UI['accent'],
                 font=('Segoe UI', 32)).pack(pady=(26, 0))
        tk.Label(self.win, text="Commencer l'essai gratuit",
                 bg=UI['bg'], fg=UI['text'],
                 font=('Segoe UI', 15, 'bold')).pack()
        tk.Label(self.win, text=f"Compte : {self.email}",
                 bg=UI['bg'], fg=UI['text2'],
                 font=('Segoe UI', 9)).pack(pady=(2, 18))

        # Liste des avantages
        avantages = [
            "  7 jours gratuits, annulez n'importe quand",
            "  Dictee vocale illimitee",
            "  Fonctionne avec ChatGPT, Claude, Gemini...",
            "  5 $/mois seulement (CAD) apres l'essai",
        ]
        for txt in avantages:
            row = tk.Frame(self.win, bg=UI['bg'])
            row.pack(anchor='w', padx=48, pady=1)
            tk.Label(row, text="OK", bg=UI['bg'], fg=UI['green'],
                     font=('Segoe UI', 9, 'bold')).pack(side='left')
            tk.Label(row, text=txt, bg=UI['bg'], fg=UI['text'],
                     font=('Segoe UI', 10)).pack(side='left')

        # Bouton principal
        self.main_btn = tk.Button(
            self.win, text="Demarrer l'essai gratuit  ->",
            bg=UI['btn'], fg='white',
            font=('Segoe UI', 12, 'bold'),
            relief='flat', padx=24, pady=12,
            cursor='hand2', command=self._start_trial)
        self.main_btn.pack(pady=(22, 6))

        # Statut (messages d'info)
        self.status_var = tk.StringVar()
        tk.Label(self.win, textvariable=self.status_var,
                 bg=UI['bg'], fg=UI['green'],
                 font=('Segoe UI', 9), wraplength=360).pack(pady=2)

        # Bouton verifier
        self.check_btn = tk.Button(
            self.win,
            text="J'ai paye  ->  Verifier mon acces",
            bg=UI['bg2'], fg=UI['text2'],
            font=('Segoe UI', 9),
            relief='flat', padx=14, pady=6,
            cursor='hand2', command=self._check_sub)
        self.check_btn.pack(pady=(0, 6))

        tk.Label(self.win, text="Paiement securise par Stripe",
                 bg=UI['bg'], fg=UI['text2'],
                 font=('Segoe UI', 8)).pack(pady=(8, 0))

    def _start_trial(self):
        self.main_btn.config(state='disabled', text='Chargement...')
        self.status_var.set('')

        def _call():
            data, code = _api('/create-checkout', data={}, token=self.auth.token)
            self._q.put(('checkout', data, code))

        threading.Thread(target=_call, daemon=True).start()
        self.win.after(100, self._poll)

    def _open_checkout(self, data, code):
        self.main_btn.config(state='normal', text="Demarrer l'essai gratuit  ->")
        if code == 0:
            self.status_var.set("Serveur inaccessible. Reessaie.")
            return
        if code != 200:
            err = (data or {}).get('error', 'Erreur')
            self.status_var.set(err)
            return
        url = (data or {}).get('url', '')
        if url:
            webbrowser.open(url)
            self.status_var.set(
                "Page Stripe ouverte dans ton navigateur !\n"
                "Clique sur «J'ai paye» une fois l'abonnement active.")

    def _poll(self):
        """Verifie la queue toutes les 100ms (main thread)."""
        try:
            item = self._q.get_nowait()
            if item[0] == 'checkout':
                self._open_checkout(item[1], item[2])
            elif item[0] == 'check':
                self._handle_check(item[1], item[2], item[3])
        except _queue.Empty:
            self.win.after(100, self._poll)

    def _check_sub(self):
        self.check_btn.config(state='disabled', text='Verification...')
        self.status_var.set('')

        def _call():
            is_active, status, error = self.auth.check_subscription()
            self._q.put(('check', is_active, status, error))

        threading.Thread(target=_call, daemon=True).start()
        self.win.after(100, self._poll)

    def _handle_check(self, is_active, status, error):
        self.check_btn.config(state='normal',
                               text="J'ai paye  ->  Verifier mon acces")
        if error == 'network':
            self.status_var.set("Serveur inaccessible. Reessaie.")
            return
        if is_active:
            self.auth.save(self.auth.token, self.auth.email)
            self.result = 'ok'
            self.win.destroy()
        else:
            st = status or 'none'
            self.status_var.set(
                f"Abonnement non actif (statut : {st}).\n"
                "Complete le paiement Stripe puis reessaie.")


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
        self.win.withdraw()
        def _do_paste():
            time.sleep(0.6)
            self.paste_cb(text)
        threading.Thread(target=_do_paste, daemon=True).start()

    def _clear(self):
        self.history.clear()
        self._refresh()


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
        
        self.tray            = None
        self._last_sound_t   = time.time()
        self._is_mini        = True
        self._is_ready       = False   # Pilule visible mais pas encore en ecoute
        # Mode precis : blocs pre-transcrits pendant l'enregistrement
        self._precise_results      = {}   # index -> texte transcrit
        self._precise_results_lock = threading.Lock()
        self._precise_block_idx    = 0    # prochain index de bloc a attribuer
        self._precise_buf          = []   # buffer courant (chunks en cours d'accumulation)
        self._precise_buf_dur      = 0.0  # duree accumulee dans le buffer courant (secondes)
        self._precise_last_words   = []   # derniers mots du bloc precedent (overlap contexte)
        # Mode rapide : texte confirme (pour detection doublons)
        self._fast_confirmed       = ''
        self.settings        = Settings()
        self.history         = History()
        self.auth            = AuthManager()
        self._current_hotkey = self.settings.get('hotkey') or DEFAULT_HOTKEY

        # Labels bilingues selon la langue configuree
        global _LABELS
        lang = self.settings.get('language') or 'fr-FR'
        _LABELS = _LABELS_EN if lang.startswith('en') else _LABELS_FR

        self._setup_gui()       # GUI d'abord (self.root doit exister avant les Toplevel)

        # ── Verification authentification + abonnement ─────────
        if not self._check_auth():
            try:
                self.root.destroy()
            except Exception:
                pass
            return

        self._setup_tray()
        self._setup_hotkey()
        self.root.mainloop()

        if self.tray:
            self.tray.stop()

    # ── Auth : verification au lancement ──────────────────────

    def _check_auth(self):
        """Verifie auth + abonnement. Retourne True si OK, False si l'app doit quitter."""
        if not self.auth.token:
            return self._run_login_flow()

        # Session recente (< 7 jours) -> demarrage direct, pas de verif reseau
        if self.auth.last_verified:
            try:
                age = datetime.utcnow() - datetime.fromisoformat(self.auth.last_verified)
                if age < timedelta(days=7):
                    log.info(f"Session valide ({self.auth.email}), demarrage direct")
                    return True
            except Exception:
                pass

        # Verif backend (seulement si > 7 jours sans verif)
        is_active, status, error = self.auth.check_subscription()

        if error == 'network':
            # Backend inaccessible : autoriser si token present (max 30 jours offline)
            if self.auth.last_verified:
                try:
                    age = datetime.utcnow() - datetime.fromisoformat(self.auth.last_verified)
                    if age < timedelta(days=30):
                        log.warning("Backend inaccessible - demarrage offline autorise (< 30 jours)")
                        return True
                except Exception:
                    pass
            log.warning("Backend inaccessible et token trop ancien - connexion requise")
            return True  # Autorise quand meme pour eviter blocage utilisateur

        if error in ('expired', 'unknown'):
            self.auth.clear()
            return self._run_login_flow()

        if not is_active:
            return self._run_subscribe_flow()

        # Tout bon : mettre a jour last_verified
        self.auth.save(self.auth.token, self.auth.email)
        log.info(f"Auth OK ({self.auth.email}), statut : {status}")
        return True

    def _run_login_flow(self):
        """Affiche la fenetre de connexion. Retourne True si auth reussie."""
        lang = 'en' if (self.settings.get('language') or 'fr').startswith('en') else 'fr'
        login = LoginWindow(self.root, self.auth, lang=lang)
        self.root.wait_window(login.win)
        if login.result != 'ok':
            return False
        if login.active:
            return True
        return self._run_subscribe_flow()

    def _run_subscribe_flow(self):
        """Affiche la fenetre d'abonnement. Retourne True si abonne."""
        sub = SubscribeWindow(self.root, self.auth)
        self.root.wait_window(sub.win)
        return sub.result == 'ok'

    def _logout(self):
        """Deconnexion : efface le token et quitte."""
        self.auth.clear()
        self._quit()

    def _open_subscribe(self):
        """Ouvre la fenetre d'abonnement depuis le menu."""
        try:
            if hasattr(self, '_subw') and self._subw.win.winfo_exists():
                self._subw.win.lift()
                return
        except Exception:
            pass
        self._subw = SubscribeWindow(self.root, self.auth)

    # ── Icone PIL ─────────────────────────────────────────────

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
        if state == 'idle':
            w = PW_IDLE
        elif state == 'preview':
            w = PW_PREVIEW
        else:
            w = PW_FULL
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
        # Info compte
        if self.auth.email:
            m.add_command(label=f"  {self.auth.email}", state='disabled')
            m.add_separator()
        m.add_command(label="Parametres",     command=self._open_settings)
        m.add_command(label="Historique",     command=self._open_history)
        m.add_separator()
        m.add_command(label="Mon abonnement", command=self._open_subscribe)
        m.add_command(label="Se deconnecter", command=self._logout)
        m.add_separator()
        m.add_command(label="Quitter",        command=self._quit)
        m.tk_popup(e.x_root, e.y_root)

    def _quit(self):
        if self.tray:
            self.tray.stop()
        self.root.quit()

    # ── System tray ───────────────────────────────────────────

    def _setup_tray(self):
        try:
            self._save_ico()
            email_label = self.auth.email or 'Non connecte'
            menu = pystray.Menu(
                pystray.MenuItem(APP_NAME, None, enabled=False),
                pystray.MenuItem(email_label, None, enabled=False),
                pystray.Menu.SEPARATOR,
                pystray.MenuItem('Parametres',
                                 lambda: self.root.after(0, self._open_settings)),
                pystray.MenuItem('Historique',
                                 lambda: self.root.after(0, self._open_history)),
                pystray.Menu.SEPARATOR,
                pystray.MenuItem('Mon abonnement',
                                 lambda: self.root.after(0, self._open_subscribe)),
                pystray.MenuItem('Demarrer avec Windows',
                                 self._toggle_autostart,
                                 checked=lambda _: self._is_autostart()),
                pystray.Menu.SEPARATOR,
                pystray.MenuItem('Se deconnecter',
                                 lambda: self.root.after(0, self._logout)),
                pystray.MenuItem('Quitter',
                                 lambda: self.root.after(0, self._quit))
            )
            self.tray = pystray.Icon(APP_NAME, self._make_icon(), APP_NAME, menu)
            self.tray.run_detached()
        except Exception as ex:
            print(f"Tray non disponible : {ex}")

    # ── Fenetres ──────────────────────────────────────────────

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
        # Mettre a jour les labels si la langue a change
        global _LABELS
        lang = self.settings.get('language') or 'fr-FR'
        _LABELS = _LABELS_EN if lang.startswith('en') else _LABELS_FR

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
            self._precise_buf     = []
            self._precise_buf_dur = 0.0
        # Reset mode precis
        with self._precise_results_lock:
            self._precise_results   = {}
            self._precise_block_idx = 0
        self._precise_last_words = []
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
            self._fast_confirmed = ''
            threading.Thread(target=self._vad_loop, daemon=True).start()
        elif self.settings.get('mode') == 'precise':
            threading.Thread(target=self._precise_block_loop, daemon=True).start()
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
        """Mode rapide :
        - affiche le texte provisoire dans la pilule pendant la parole
        - colle dans le vrai champ seulement quand le segment est stable (pause 600ms)
        - detecte et supprime les doublons entre segments"""
        was_speaking   = False
        phrase_start   = None
        last_ui_update = 0.0   # throttle UI : max 1 refresh / 150ms

        while self.recording:
            time.sleep(0.05)

            with self._audio_lock:
                self._vad_phrase_buf.extend(self.audio_data)
                self.audio_data = []

            silence_dur = time.time() - self._last_sound_t
            is_speaking = silence_dur < VAD_PAUSE_SEC

            if is_speaking and not was_speaking:
                phrase_start = time.time()
            if is_speaking:
                was_speaking = True

            phrase_age  = (time.time() - phrase_start) if phrase_start else 0
            force_cut   = was_speaking and phrase_age >= VAD_MAX_SEC
            # Coupe naturelle : silence >= 600ms (plus long que VAD_PAUSE_SEC)
            stable_cut  = was_speaking and silence_dur >= 0.60

            # Affichage provisoire dans la pilule (throttle 150ms)
            if was_speaking and self.recording and (time.time() - last_ui_update > 0.15):
                buf_preview = list(self._vad_phrase_buf)
                if buf_preview:
                    audio_prev = np.concatenate(buf_preview).flatten()
                    dur_prev   = len(audio_prev) / SAMPLE_RATE
                    if dur_prev >= 0.5:
                        secs = max(1, int(dur_prev))
                        self._set_status(f'Ecoute... {secs}s', 'preview')
                        last_ui_update = time.time()

            if (force_cut or stable_cut) and self._vad_phrase_buf:
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
                    phrase_start = time.time()
                else:
                    was_speaking = False
                    phrase_start = None

    @staticmethod
    def _remove_overlap(confirmed, new_text):
        """Supprime le chevauchement entre la fin du texte confirme
        et le debut du nouveau segment.
        Ex: confirmed='je vais aller au', new='aller au restaurant'
        -> retourne 'restaurant' (on enleve 'aller au' qui est deja la)"""
        if not confirmed or not new_text:
            return new_text
        # Cherche le plus long suffixe de confirmed qui est prefixe de new_text
        confirmed_words = confirmed.lower().split()
        new_words       = new_text.split()
        max_overlap     = min(6, len(confirmed_words), len(new_words))
        for n in range(max_overlap, 0, -1):
            suffix = confirmed_words[-n:]
            prefix = [w.lower() for w in new_words[:n]]
            if suffix == prefix:
                leftover = new_words[n:]
                return ' '.join(leftover) if leftover else ''
        return new_text

    def _paste_fast_chunk(self, chunks):
        """Transcrit un segment, affiche dans la pilule, puis colle dans le champ."""
        # 1. Afficher "transcription..." dans la pilule pendant le traitement
        self._set_status('Transcription...', 'processing')

        text = self._do_transcribe(chunks)
        if not text:
            if self.recording:
                self._set_status(_LABELS['recording'], 'recording')
            return

        # 2. Supprimer les doublons avec le texte deja colle
        with self._paste_lock:
            cleaned = self._remove_overlap(self._fast_confirmed, text)

        if not cleaned:
            if self.recording:
                self._set_status(_LABELS['recording'], 'recording')
            return

        # 3. Afficher le segment valide dans la pilule (apercu avant collage)
        preview = cleaned[:50] + ('...' if len(cleaned) > 50 else '')
        self._set_status(preview, 'preview')
        time.sleep(0.25)   # court affichage pour que l'utilisateur voit ce qui va etre colle

        # 4. Coller dans le vrai champ + espace
        with self._paste_lock:
            combined = (self._fast_confirmed + ' ' + cleaned).strip()
            # Garder seulement les 20 derniers mots (les autres sont inutiles pour _remove_overlap)
            words = combined.split()
            self._fast_confirmed = ' '.join(words[-20:])
            _paste_via_winapi(cleaned, add_space=True)

        self.history.add(cleaned)
        log.info(f"[Rapide] Segment colle : {cleaned[:60]}")

        # 5. Retour a l'ecoute si on enregistre toujours
        time.sleep(0.3)
        if self.recording:
            self._set_status(_LABELS['recording'], 'recording')

    # ── Mode precis avec blocs de 10s envoyes en temps reel ──

    # ── Mode precis avec blocs envoyes en temps reel ──────────

    def _precise_block_loop(self):
        """Pendant l'enregistrement en mode precis :
        - mesure la duree reelle via le nombre de frames (pas une estimation)
        - coupe aux silences naturels pour ne jamais couper un mot
        - passe un overlap de 3 mots au bloc suivant pour preserver le contexte"""
        BLOCK_SEC      = PRECISE_BLOCK_SEC   # 10s cible
        SILENCE_CUT    = 0.6                 # silence min pour couper proprement
        frames_buf     = 0                   # nombre de frames dans le buffer courant
        was_speaking   = False

        while self.recording:
            time.sleep(0.05)

            with self._audio_lock:
                new_chunks = list(self.audio_data)
                self.audio_data = []
                # Compter les vraies frames
                for c in new_chunks:
                    frames_buf += len(c)
                self._precise_buf.extend(new_chunks)

            if not new_chunks:
                continue

            dur_buf      = frames_buf / SAMPLE_RATE
            silence_dur  = time.time() - self._last_sound_t
            is_speaking  = silence_dur < 0.35
            if is_speaking:
                was_speaking = True

            # Couper si : duree atteinte ET silence naturel detecte
            natural_cut = was_speaking and silence_dur >= SILENCE_CUT
            force_cut   = dur_buf >= BLOCK_SEC + 3.0   # forcer apres 13s max

            if (natural_cut and dur_buf >= 3.0) or force_cut:
                with self._audio_lock:
                    block     = list(self._precise_buf)
                    self._precise_buf     = []
                    self._precise_buf_dur = 0.0

                with self._precise_results_lock:
                    block_idx = self._precise_block_idx
                    self._precise_block_idx += 1

                frames_buf   = 0
                was_speaking = False

                if block:
                    log.info(f"[Precis] Bloc {block_idx} "
                             f"({dur_buf:.1f}s, silence={silence_dur:.2f}s) -> Google")
                    threading.Thread(
                        target=self._transcribe_precise_block,
                        args=(block, block_idx),
                        daemon=True
                    ).start()

    def _transcribe_precise_block(self, chunks, idx):
        """Transcrit un bloc audio et stocke le resultat.
        Utilise _remove_overlap pour supprimer le contexte overlap du debut."""
        t0   = time.time()
        text = self._do_transcribe(chunks)
        dur  = time.time() - t0

        if text:
            # Supprimer les mots de contexte du bloc precedent s'ils sont au debut
            cleaned = self._remove_overlap(
                ' '.join(self._precise_last_words), text
            ) if self._precise_last_words else text
            # Mettre a jour les derniers mots pour le prochain bloc
            words = (cleaned or text).split()
            self._precise_last_words = words[-3:] if len(words) >= 3 else words
            result = cleaned or text
        else:
            result = ''

        log.info(f"[Precis] Bloc {idx} transcrit en {dur:.1f}s : "
                 f"{result[:50] or '(vide)'}")
        with self._precise_results_lock:
            self._precise_results[idx] = result

    # ── Commandes ASK (chatbot navigateur) ───────────────────────

    def _execute_ask_command(self, target, url, message):
        """Ouvre le chatbot dans le navigateur, colle le message et envoie Enter."""
        lang   = self.settings.get('language') or 'fr-FR'
        is_en  = lang.startswith('en')
        name   = target.capitalize()

        self._set_status(f'Opening {name}...' if is_en else f'Ouverture {name}...', 'processing')
        log.info(f"[Ask] Cible={target}  message={message[:50]!r}")

        VK_RETURN       = 0x0D
        VK_CONTROL      = 0x11
        VK_V            = 0x56
        KEYEVENTF_KEYUP = 0x0002

        # 1. Ouvrir l'URL dans le navigateur par defaut
        try:
            webbrowser.open(url)
        except Exception as e:
            log.error(f"[Ask] Impossible d'ouvrir le navigateur : {e}")
            err = f"Can't open {name}." if is_en else f"Impossible d'ouvrir {name}."
            self._set_status(err, 'error')
            time.sleep(2)
            self._set_status(_LABELS['ready'], 'ready')
            return

        # 2. Attendre le chargement de la page
        # On donne 3.5s pour le premier chargement, moins si deja ouvert
        log.info(f"[Ask] Attente chargement {url} ...")
        time.sleep(3.5)

        # 3. Copier le message dans le presse-papiers et coller
        try:
            pyperclip.copy(message)
        except Exception as e:
            log.error(f"[Ask] Erreur presse-papiers : {e}")

        # Clic simulé pour s'assurer que le focus est dans la page
        # (l'utilisateur voit la page active, on envoie juste Ctrl+V)
        time.sleep(0.2)
        ctypes.windll.user32.keybd_event(VK_CONTROL, 0, 0, 0)
        ctypes.windll.user32.keybd_event(VK_V,       0, 0, 0)
        ctypes.windll.user32.keybd_event(VK_V,       0, KEYEVENTF_KEYUP, 0)
        ctypes.windll.user32.keybd_event(VK_CONTROL, 0, KEYEVENTF_KEYUP, 0)
        time.sleep(0.4)

        # 4. Appuyer sur Entree pour envoyer
        ctypes.windll.user32.keybd_event(VK_RETURN, 0, 0, 0)
        ctypes.windll.user32.keybd_event(VK_RETURN, 0, KEYEVENTF_KEYUP, 0)

        log.info(f"[Ask] Message envoye a {name}")
        ok_msg = f'Sent to {name}.' if is_en else f'Envoye a {name}.'
        self._set_status(ok_msg, 'success')
        time.sleep(1.5)
        self._set_status(_LABELS['ready'], 'ready')

    # ── Commandes d'edition vocale ────────────────────────────

    def _execute_edit_command(self, command, payload):
        """Execute une commande d'edition sur le texte du champ actif.
        Strategie : Ctrl+A -> Ctrl+C -> modifier -> Ctrl+V (remplace la selection)."""
        VK_CONTROL      = 0x11
        VK_A            = 0x41
        VK_C            = 0x43
        KEYEVENTF_KEYUP = 0x0002

        lang = self.settings.get('language') or 'fr-FR'
        is_en = lang.startswith('en')

        # Labels bilingues
        if command == 'delete_last':
            status_working = 'Editing: deleting...'   if is_en else 'Edition : suppression...'
            status_ok      = 'Last sentence deleted.'  if is_en else 'Derniere phrase supprimee.'
            log.info("[Edit] Commande : delete_last")
        else:
            status_working = 'Editing: replacing...'   if is_en else 'Edition : remplacement...'
            status_ok      = 'Last sentence replaced.'  if is_en else 'Derniere phrase remplacee.'
            log.info(f"[Edit] Commande : replace_last -> '{(payload or '')[:40]}'")

        self._set_status(status_working, 'processing')
        time.sleep(0.15)

        # Selectionner tout (Ctrl+A) puis copier (Ctrl+C)
        # On envoie Ctrl+A deux fois pour couvrir les apps qui ont besoin
        # d'un double appui (ex: certains champs Notion, Gmail)
        for _ in range(2):
            ctypes.windll.user32.keybd_event(VK_CONTROL, 0, 0, 0)
            ctypes.windll.user32.keybd_event(VK_A,       0, 0, 0)
            ctypes.windll.user32.keybd_event(VK_A,       0, KEYEVENTF_KEYUP, 0)
            ctypes.windll.user32.keybd_event(VK_CONTROL, 0, KEYEVENTF_KEYUP, 0)
            time.sleep(0.08)

        ctypes.windll.user32.keybd_event(VK_CONTROL, 0, 0, 0)
        ctypes.windll.user32.keybd_event(VK_C,       0, 0, 0)
        ctypes.windll.user32.keybd_event(VK_C,       0, KEYEVENTF_KEYUP, 0)
        ctypes.windll.user32.keybd_event(VK_CONTROL, 0, KEYEVENTF_KEYUP, 0)
        time.sleep(0.25)

        # Lire le presse-papiers
        try:
            current_text = pyperclip.paste() or ''
        except Exception:
            current_text = ''

        if not current_text.strip():
            err_msg = 'Nothing to edit.' if is_en else 'Rien a modifier.'
            log.info("[Edit] Champ vide ou Ctrl+A non supporte dans cette app")
            self._set_status(err_msg, 'error')
            time.sleep(1.5)
            self._set_status(_LABELS['ready'], 'ready')
            return

        # Modifier le texte
        if command == 'delete_last':
            new_text = _remove_last_sentence(current_text)
            log.info(f"[Edit] Avant : ...{current_text[-60:]!r}")
            log.info(f"[Edit] Apres : ...{new_text[-60:]!r}")
        else:  # replace_last
            base     = _remove_last_sentence(current_text)
            new_text = (base + ' ' + payload).strip() if base else payload
            if new_text and new_text[-1] not in '.!?…':
                new_text += '.'
            log.info(f"[Edit] Remplacement -> ...{new_text[-60:]!r}")

        if new_text == current_text:
            nothing_msg = 'Nothing changed.' if is_en else 'Aucun changement.'
            log.info("[Edit] Texte inchange")
            self._set_status(nothing_msg, 'error')
            time.sleep(1.5)
            self._set_status(_LABELS['ready'], 'ready')
            return

        # Recoller — Ctrl+V remplace la selection (tout le champ)
        _paste_via_winapi(new_text)
        log.info("[Edit] Texte recolle avec succes")

        self._set_status(status_ok, 'success')
        time.sleep(1.5)
        self._set_status(_LABELS['ready'], 'ready')

    # ── Commandes IA ──────────────────────────────────────────

    def _apply_ai_command(self, command, text):
        """Envoie le texte au backend Railway pour traitement IA.
        Retourne le texte transforme ou None si erreur."""
        log.info(f"[IA] Commande '{command}' sur : {text[:60]}")
        self._set_status(f'IA : {command}...', 'processing')
        data, code = _api(
            '/ai',
            data={'command': command, 'text': text},
            token=self.auth.token
        )
        if code == 0:
            log.error("[IA] Backend inaccessible")
            self._set_status('IA : erreur reseau', 'error')
            return None
        if code != 200:
            err = (data or {}).get('error', f'Erreur {code}')
            log.error(f"[IA] Erreur backend : {err}")
            self._set_status(f'IA : {err[:30]}', 'error')
            return None
        result = (data or {}).get('result', '').strip()
        log.info(f"[IA] Resultat : {result[:60]}")
        return result or None

    # ── Nettoyage minimal du texte transcrit (Deepgram) ─────────
    # Deepgram gere deja la ponctuation et les noms propres.
    # On garde uniquement les corrections structurelles légères.

    def _clean_text(self, text, lang):
        """Nettoyage minimal pour Deepgram :
        - normalisation des espaces
        - suppression espace avant ponctuation
        - majuscule en debut de phrase
        Deepgram gere deja la ponctuation -> pas de point final force.
        Pas de suppression de fillers -> risque de deformer le texte."""
        if not text:
            return text

        # 1. Normaliser les espaces
        text = re.sub(r'\s+', ' ', text).strip()

        # 2. Supprimer espace avant ponctuation (artefact rare)
        text = re.sub(r'\s([,\.!?;:])', r'\1', text)

        if not text:
            return None

        # 3. Majuscule en debut de phrase
        text = text[0].upper() + text[1:]

        log.info(f"Texte nettoye : {text[:60]}")
        return text

    # ── Transcription (Deepgram Nova-2) ───────────────────────

    def _do_transcribe(self, audio_chunks):
        if not audio_chunks:
            return None
        try:
            audio = np.concatenate(audio_chunks).flatten()
            duree = len(audio) / SAMPLE_RATE
            rms   = float(np.sqrt(np.mean(audio ** 2)))
            log.info(f"Audio capture : {duree:.1f}s  RMS={rms:.5f}  "
                     f"({'OK' if rms > 0.005 else 'TROP SILENCIEUX - mauvais micro?'})")
            if len(audio) < SAMPLE_RATE * 0.4:
                log.warning("Audio trop court (<0.4s), ignore")
                return None

            # Encoder en WAV en memoire
            audio_i16 = (np.clip(audio, -1.0, 1.0) * 32767).astype(np.int16)
            buf = io.BytesIO()
            write_wav(buf, SAMPLE_RATE, audio_i16)
            audio_bytes = buf.getvalue()

            # Langue : fr-FR -> "fr"  |  en-US -> "en"
            settings_lang = self.settings.get('language') or 'fr-FR'
            dg_lang = 'fr' if settings_lang.startswith('fr') else 'en'

            # Envoyer l'audio au backend Railway qui appelle Deepgram
            import base64
            audio_b64 = base64.b64encode(audio_bytes).decode('utf-8')
            data, code = _api(
                '/transcribe',
                data={'audio': audio_b64, 'language': dg_lang},
                token=self.auth.token
            )
            if code == 0:
                log.error("Transcription : backend inaccessible")
                return None
            if code != 200:
                log.error(f"Transcription : erreur backend {code}")
                return None

            result = (data or {}).get('transcript', '').strip()
            if not result:
                log.info("Deepgram : aucune parole detectee")
                return None

            log.info(f"Transcription brute Deepgram ({dg_lang}) : {result[:60]}")

            # Detecter commande d'edition EN PREMIER (avant nettoyage)
            edit_cmd, edit_payload = _detect_edit_command(result)
            if edit_cmd:
                threading.Thread(
                    target=self._execute_edit_command,
                    args=(edit_cmd, edit_payload),
                    daemon=True
                ).start()
                return None

            # Detecter commande ASK sur texte brut (pas de _clean_text avant)
            ask_target, ask_url, ask_msg = _detect_ask_command(result)
            if ask_target:
                threading.Thread(
                    target=self._execute_ask_command,
                    args=(ask_target, ask_url, ask_msg),
                    daemon=True
                ).start()
                return None

            # Seulement pour dictee normale : nettoyage complet
            result = self._clean_text(result, settings_lang)
            if not result:
                return None
            # Detecter une commande IA en debut de phrase
            command, remaining = _detect_ai_command(result)
            if command:
                ai_result = self._apply_ai_command(command, remaining)
                return ai_result if ai_result else remaining
            return result

        except Exception as e:
            log.error(f"Erreur transcription : {e}")
            return None

    def _stop_and_transcribe(self):
        with self._audio_lock:
            self.recording       = False
            chunks               = list(self._vad_phrase_buf) + list(self.audio_data)
            self.audio_data      = []
            self._vad_phrase_buf = []
            # En mode precis, lire et vider _precise_buf dans le meme lock
            if self.settings.get('mode') == 'precise':
                chunks = list(self._precise_buf) + chunks
                self._precise_buf     = []
                self._precise_buf_dur = 0.0

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
                    cleaned = self._remove_overlap(self._fast_confirmed, text)
                    if cleaned:
                        self.history.add(cleaned)
                        _paste_via_winapi(cleaned, add_space=True)
            self._fast_confirmed = ''
            self._set_status(_LABELS['ready'], 'ready')
            return

        # Mode precis : recupere le dernier bloc restant et fusionne avec les resultats
        self._set_status('Transcription...', 'processing')

        # chunks contient deja _precise_buf (ajoute plus haut) + audio_data
        # C'est le dernier morceau pas encore envoye
        if chunks:
            audio_test = np.concatenate(chunks).flatten()
            if len(audio_test) / SAMPLE_RATE >= VAD_MIN_SEC:
                with self._precise_results_lock:
                    last_idx = self._precise_block_idx
                    self._precise_block_idx += 1
                log.info(f"[Precis] Dernier bloc {last_idx} "
                         f"({len(audio_test)/SAMPLE_RATE:.1f}s) -> envoi Google")
                threading.Thread(
                    target=self._transcribe_precise_block,
                    args=(chunks, last_idx),
                    daemon=True
                ).start()

        # Attendre que tous les blocs soient transcrits
        with self._precise_results_lock:
            total_blocs = self._precise_block_idx
        log.info(f"[Precis] Attente de {total_blocs} bloc(s)...")
        if total_blocs > 1:
            self._set_status('Fusion...', 'processing')

        deadline = time.time() + 30  # timeout de securite
        while time.time() < deadline:
            with self._precise_results_lock:
                done = len(self._precise_results)
            if done >= total_blocs:
                break
            time.sleep(0.15)

        # Fusionner les resultats dans l'ordre
        with self._precise_results_lock:
            results = dict(self._precise_results)

        parts = []
        for i in range(total_blocs):
            t = results.get(i, '')
            if t:
                parts.append(t)

        text = ' '.join(parts).strip()
        log.info(f"[Precis] Texte final ({total_blocs} blocs) : {text[:80]}")

        try:
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
