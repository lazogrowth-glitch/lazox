#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
VocalType Backend - API d'abonnement Stripe
"""

import os
import sqlite3
import stripe
import jwt
from datetime import datetime, timedelta
from flask import Flask, request, jsonify
from flask_cors import CORS
from werkzeug.security import generate_password_hash, check_password_hash

app = Flask(__name__)
CORS(app, resources={r"/*": {"origins": "*"}})

# ── Config depuis variables d'environnement Railway ───────────
STRIPE_SECRET_KEY     = os.environ.get('STRIPE_SECRET_KEY', '')
STRIPE_PUBLISHABLE_KEY= os.environ.get('STRIPE_PUBLISHABLE_KEY', '')
STRIPE_PRICE_ID       = os.environ.get('STRIPE_PRICE_ID', '')
STRIPE_WEBHOOK_SECRET = os.environ.get('STRIPE_WEBHOOK_SECRET', '')
JWT_SECRET            = os.environ.get('JWT_SECRET', 'change-me-in-railway')
FRONTEND_URL          = os.environ.get('FRONTEND_URL', 'https://lazogrowth-glitch.github.io/lazox')
DATABASE              = os.environ.get('DATABASE', 'vocaltype.db')
OPENAI_API_KEY        = os.environ.get('OPENAI_API_KEY', '')
DEEPGRAM_API_KEY      = os.environ.get('DEEPGRAM_API_KEY', '')

stripe.api_key = STRIPE_SECRET_KEY

# Limite la taille des requetes a 50 Mo (audio base64)
app.config['MAX_CONTENT_LENGTH'] = 50 * 1024 * 1024


# ── Base de donnees SQLite ─────────────────────────────────────
def get_db():
    conn = sqlite3.connect(DATABASE)
    conn.row_factory = sqlite3.Row
    return conn

def init_db():
    db = get_db()
    db.execute('''
        CREATE TABLE IF NOT EXISTS users (
            id                   INTEGER PRIMARY KEY AUTOINCREMENT,
            email                TEXT UNIQUE NOT NULL,
            password_hash        TEXT NOT NULL,
            stripe_customer_id   TEXT,
            subscription_status  TEXT DEFAULT 'none',
            trial_end            TEXT,
            period_end           TEXT,
            created_at           TEXT DEFAULT CURRENT_TIMESTAMP
        )
    ''')
    db.commit()
    db.close()

init_db()


# ── Helpers JWT ────────────────────────────────────────────────
def make_token(user_id):
    payload = {
        'user_id': user_id,
        'exp': datetime.utcnow() + timedelta(days=30)
    }
    return jwt.encode(payload, JWT_SECRET, algorithm='HS256')

def get_current_user():
    auth = request.headers.get('Authorization', '')
    if not auth.startswith('Bearer '):
        return None
    try:
        payload = jwt.decode(auth[7:], JWT_SECRET, algorithms=['HS256'])
        db  = get_db()
        try:
            user = db.execute('SELECT * FROM users WHERE id = ?',
                              (payload['user_id'],)).fetchone()
        finally:
            db.close()
        return user
    except Exception:
        return None

def is_active(user):
    status = user['subscription_status']
    if status == 'active':
        return True
    if status == 'trialing' and user['trial_end']:
        try:
            return datetime.utcnow() < datetime.fromisoformat(user['trial_end'])
        except Exception:
            return False
    return False


# ── Routes ─────────────────────────────────────────────────────

@app.route('/health')
def health():
    return jsonify({'status': 'ok', 'app': 'VocalType Backend'})


@app.route('/admin/activate')
def admin_activate():
    secret = request.args.get('secret', '')
    email  = request.args.get('email', '').strip().lower()
    admin_secret = os.environ.get('ADMIN_SECRET', '')

    if not admin_secret or secret != admin_secret:
        return jsonify({'error': 'Non autorise'}), 401
    if not email:
        return jsonify({'error': 'Email requis'}), 400

    db = get_db()
    user = db.execute('SELECT email, subscription_status FROM users WHERE email=?',
                      (email,)).fetchone()
    if not user:
        db.close()
        return jsonify({'error': f'Utilisateur {email} introuvable. Cree le compte dabord.'}), 404

    db.execute(
        "UPDATE users SET subscription_status='active', period_end='2099-12-31T00:00:00' WHERE email=?",
        (email,)
    )
    db.commit()
    db.close()
    return jsonify({'ok': True, 'email': email, 'status': 'active'})


@app.route('/register', methods=['POST'])
def register():
    data     = request.json or {}
    email    = data.get('email', '').strip().lower()
    password = data.get('password', '')

    if not email or '@' not in email:
        return jsonify({'error': 'Email invalide'}), 400
    if len(password) < 6:
        return jsonify({'error': 'Mot de passe trop court (min 6 caracteres)'}), 400

    db = get_db()
    try:
        customer = stripe.Customer.create(email=email)
        db.execute(
            'INSERT INTO users (email, password_hash, stripe_customer_id) VALUES (?,?,?)',
            (email, generate_password_hash(password), customer.id)
        )
        db.commit()
        user  = db.execute('SELECT * FROM users WHERE email = ?', (email,)).fetchone()
        token = make_token(user['id'])
        return jsonify({'token': token, 'email': email, 'active': False, 'status': 'none'}), 201
    except sqlite3.IntegrityError:
        return jsonify({'error': 'Cet email est deja utilise'}), 409
    except Exception as e:
        return jsonify({'error': str(e)}), 500
    finally:
        db.close()


@app.route('/login', methods=['POST'])
def login():
    data     = request.json or {}
    email    = data.get('email', '').strip().lower()
    password = data.get('password', '')

    db   = get_db()
    user = db.execute('SELECT * FROM users WHERE email = ?', (email,)).fetchone()
    db.close()

    if not user or not check_password_hash(user['password_hash'], password):
        return jsonify({'error': 'Email ou mot de passe incorrect'}), 401

    token = make_token(user['id'])
    return jsonify({
        'token':  token,
        'email':  user['email'],
        'status': user['subscription_status'],
        'active': is_active(user)
    })


@app.route('/me', methods=['GET'])
def me():
    user = get_current_user()
    if not user:
        return jsonify({'error': 'Non autorise'}), 401
    return jsonify({
        'email':      user['email'],
        'status':     user['subscription_status'],
        'active':     is_active(user),
        'trial_end':  user['trial_end'],
        'period_end': user['period_end']
    })


@app.route('/create-checkout', methods=['POST'])
def create_checkout():
    user = get_current_user()
    if not user:
        return jsonify({'error': 'Non autorise'}), 401

    try:
        session = stripe.checkout.Session.create(
            customer            = user['stripe_customer_id'],
            payment_method_types= ['card'],
            line_items          = [{'price': STRIPE_PRICE_ID, 'quantity': 1}],
            mode                = 'subscription',
            subscription_data   = {'trial_period_days': 7},
            success_url         = FRONTEND_URL + '?subscribed=1',
            cancel_url          = FRONTEND_URL + '?canceled=1',
        )
        return jsonify({'url': session.url})
    except Exception as e:
        return jsonify({'error': str(e)}), 500


@app.route('/webhook', methods=['POST'])
def webhook():
    payload = request.data
    sig     = request.headers.get('Stripe-Signature', '')

    try:
        event = stripe.Webhook.construct_event(payload, sig, STRIPE_WEBHOOK_SECRET)
    except Exception as e:
        return jsonify({'error': str(e)}), 400

    db = get_db()

    def update_sub(customer_id, status, trial_end=None, period_end=None):
        db.execute(
            '''UPDATE users
               SET subscription_status=?, trial_end=?, period_end=?
               WHERE stripe_customer_id=?''',
            (
                status,
                datetime.utcfromtimestamp(trial_end).isoformat() if trial_end else None,
                datetime.utcfromtimestamp(period_end).isoformat() if period_end else None,
                customer_id
            )
        )
        db.commit()

    et = event['type']

    if et in ('customer.subscription.updated', 'customer.subscription.created'):
        sub = event['data']['object']
        update_sub(sub['customer'], sub['status'],
                   sub.get('trial_end'), sub.get('current_period_end'))

    elif et == 'customer.subscription.deleted':
        sub = event['data']['object']
        update_sub(sub['customer'], 'canceled')

    db.close()
    return jsonify({'ok': True})


@app.route('/transcribe', methods=['POST'])
def transcribe():
    """Transcrit un fichier audio WAV via Deepgram Nova-2.
    Body JSON : { "audio": "<base64 WAV>", "language": "fr"|"en" }
    Requiert un token JWT valide."""
    user = get_current_user()
    if not user:
        return jsonify({'error': 'Non autorise'}), 401
    if not is_active(user):
        return jsonify({'error': 'Abonnement inactif'}), 403

    data     = request.json or {}
    audio_b64 = data.get('audio', '')
    language  = data.get('language', 'fr')

    if not audio_b64:
        return jsonify({'error': 'Audio manquant'}), 400
    if not DEEPGRAM_API_KEY:
        return jsonify({'error': 'Cle Deepgram non configuree'}), 500

    try:
        import base64 as _b64
        import urllib.request as _ur
        import json as _json

        audio_bytes = _b64.b64decode(audio_b64)

        payload_dg = _json.dumps({
            'model':        'nova-2',
            'language':     language,
            'smart_format': True,
            'punctuate':    True,
        })

        req = _ur.Request(
            f'https://api.deepgram.com/v1/listen?model=nova-2&language={language}&smart_format=true&punctuate=true',
            data=audio_bytes,
            headers={
                'Authorization': f'Token {DEEPGRAM_API_KEY}',
                'Content-Type':  'audio/wav',
            },
            method='POST'
        )
        with _ur.urlopen(req, timeout=30) as r:
            resp = _json.loads(r.read().decode('utf-8'))

        transcript = (resp.get('results', {})
                         .get('channels', [{}])[0]
                         .get('alternatives', [{}])[0]
                         .get('transcript', '') or '')
        return jsonify({'transcript': transcript.strip()})

    except Exception as e:
        return jsonify({'error': f'Erreur Deepgram : {str(e)}'}), 500


@app.route('/ai', methods=['POST'])
def ai_command():
    """Applique une commande IA sur un texte dicte.
    Body JSON : { "command": "corrige"|"reformule"|"traduis en anglais", "text": "..." }
    Requiert un token JWT valide (abonnement actif)."""
    user = get_current_user()
    if not user:
        return jsonify({'error': 'Non autorise'}), 401
    if not is_active(user):
        return jsonify({'error': 'Abonnement inactif'}), 403

    data    = request.json or {}
    command = data.get('command', '').strip().lower()
    text    = data.get('text', '').strip()

    if not text:
        return jsonify({'error': 'Texte manquant'}), 400
    if command not in ('correct', 'rephrase', 'translate_en', 'improve', 'summarize', 'shorten'):
        return jsonify({'error': f'Commande inconnue : {command}'}), 400
    if not OPENAI_API_KEY:
        return jsonify({'error': 'Cle OpenAI non configuree'}), 500

    # Construction du prompt selon la commande
    prompts = {
        'correct':      f'Correct all spelling and grammar mistakes in this text. Reply only with the corrected text, no explanation.\n\nText: {text}',
        'rephrase':     f'Rephrase this text in a clearer and more professional way. Reply only with the rephrased text, no explanation.\n\nText: {text}',
        'translate_en': f'Translate this text to English. Reply only with the translation, no explanation.\n\nText: {text}',
        'improve':      f'Improve this text to make it clearer, more engaging and well-structured. Reply only with the improved text, no explanation.\n\nText: {text}',
        'summarize':    f'Summarize this text concisely in 1-3 sentences. Reply only with the summary, no explanation.\n\nText: {text}',
        'shorten':      f'Shorten this text while keeping the key ideas. Reply only with the shortened text, no explanation.\n\nText: {text}',
    }

    try:
        import urllib.request as _ur
        import json as _json

        payload = _json.dumps({
            'model': 'gpt-4.1-mini',
            'messages': [{'role': 'user', 'content': prompts[command]}],
            'max_tokens': 500,
            'temperature': 0.3,
        }).encode('utf-8')

        req = _ur.Request(
            'https://api.openai.com/v1/chat/completions',
            data=payload,
            headers={
                'Content-Type':  'application/json',
                'Authorization': f'Bearer {OPENAI_API_KEY}',
            },
            method='POST'
        )
        with _ur.urlopen(req, timeout=15) as r:
            resp = _json.loads(r.read().decode('utf-8'))

        result = resp['choices'][0]['message']['content'].strip()
        return jsonify({'result': result})

    except Exception as e:
        return jsonify({'error': f'Erreur OpenAI : {str(e)}'}), 500


# ── Lancement ──────────────────────────────────────────────────
if __name__ == '__main__':
    port = int(os.environ.get('PORT', 5000))
    app.run(host='0.0.0.0', port=port, debug=False)
