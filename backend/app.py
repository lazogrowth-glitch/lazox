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
from werkzeug.security import generate_password_hash, check_password_hash

app = Flask(__name__)

# ── Config depuis variables d'environnement Railway ───────────
STRIPE_SECRET_KEY     = os.environ.get('STRIPE_SECRET_KEY', '')
STRIPE_PUBLISHABLE_KEY= os.environ.get('STRIPE_PUBLISHABLE_KEY', '')
STRIPE_PRICE_ID       = os.environ.get('STRIPE_PRICE_ID', '')
STRIPE_WEBHOOK_SECRET = os.environ.get('STRIPE_WEBHOOK_SECRET', '')
JWT_SECRET            = os.environ.get('JWT_SECRET', 'change-me-in-railway')
FRONTEND_URL          = os.environ.get('FRONTEND_URL', 'https://lazogrowth-glitch.github.io/lazox')
DATABASE              = os.environ.get('DATABASE', 'vocaltype.db')

stripe.api_key = STRIPE_SECRET_KEY


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
        user = db.execute('SELECT * FROM users WHERE id = ?',
                          (payload['user_id'],)).fetchone()
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
        return jsonify({'token': token, 'email': email, 'active': False, 'status': 'none'})
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


# ── Lancement ──────────────────────────────────────────────────
if __name__ == '__main__':
    port = int(os.environ.get('PORT', 5000))
    app.run(host='0.0.0.0', port=port, debug=False)
