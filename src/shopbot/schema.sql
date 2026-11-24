PRAGMA foreign_keys = ON;
PRAGMA journal_mode = WAL;
PRAGMA busy_timeout = 5000;
CREATE TABLE IF NOT EXISTS settings (key TEXT PRIMARY KEY, value TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS bindings (
 item_id TEXT PRIMARY KEY, title TEXT NOT NULL DEFAULT '', module TEXT NOT NULL
 CHECK(module IN ('kosell','stock','extend')), product_id INTEGER, hours INTEGER NOT NULL DEFAULT 1,
 max_cost INTEGER NOT NULL DEFAULT 0 CHECK(max_cost >= 0), instruction TEXT NOT NULL DEFAULT '',
 paused INTEGER NOT NULL DEFAULT 0, hidden_by_bot INTEGER NOT NULL DEFAULT 0,
 hide_reason TEXT, healthy_count INTEGER NOT NULL DEFAULT 0,
 auto_restore INTEGER NOT NULL DEFAULT 0, auto_bump INTEGER NOT NULL DEFAULT 0,
 bump_interval INTEGER NOT NULL DEFAULT 3600, last_bump REAL NOT NULL DEFAULT 0,
 last_listing_action REAL NOT NULL DEFAULT 0
);
CREATE TABLE IF NOT EXISTS orders (
 id TEXT PRIMARY KEY, item_id TEXT NOT NULL, buyer_id TEXT NOT NULL, buyer_name TEXT NOT NULL,
 chat_id TEXT NOT NULL, market_status TEXT NOT NULL, created_at REAL NOT NULL,
 price INTEGER NOT NULL, title TEXT NOT NULL, game TEXT NOT NULL DEFAULT '', category TEXT NOT NULL DEFAULT '',
 module TEXT, stage TEXT NOT NULL DEFAULT 'new', rental_uid TEXT, rental_number TEXT,
 expires_at REAL, cost INTEGER, problem INTEGER NOT NULL DEFAULT 0, rating INTEGER,
 completed_at REAL, delivered_at REAL, error TEXT, request_json TEXT, idem_key TEXT,
 target_order_id TEXT, historical INTEGER NOT NULL DEFAULT 0, updated_at REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS orders_buyer ON orders(buyer_id, created_at);
CREATE TABLE IF NOT EXISTS stock (
 id INTEGER PRIMARY KEY AUTOINCREMENT, item_id TEXT NOT NULL, ciphertext TEXT NOT NULL,
 cost INTEGER, order_id TEXT UNIQUE REFERENCES orders(id), state TEXT NOT NULL DEFAULT 'free'
 CHECK(state IN ('free','reserved','used')), created_at REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS stock_free ON stock(item_id,state,id);
CREATE TABLE IF NOT EXISTS outbox (
 id INTEGER PRIMARY KEY AUTOINCREMENT, dedup TEXT NOT NULL UNIQUE,
 channel TEXT NOT NULL CHECK(channel IN ('playerok','telegram')), target TEXT NOT NULL,
 ciphertext TEXT NOT NULL, state TEXT NOT NULL DEFAULT 'pending', created_at REAL NOT NULL,
 remote_id TEXT, error TEXT, order_id TEXT REFERENCES orders(id), not_before REAL NOT NULL DEFAULT 0,
 purpose TEXT NOT NULL DEFAULT 'message', attempts INTEGER NOT NULL DEFAULT 0, expires_at REAL
);
CREATE TABLE IF NOT EXISTS selections (
 buyer_id TEXT PRIMARY KEY, order_id TEXT NOT NULL REFERENCES orders(id), expires_at REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS chats (
 id TEXT PRIMARY KEY, operator_paused INTEGER NOT NULL DEFAULT 0, greeted_at REAL NOT NULL DEFAULT 0,
 last_buyer_at REAL NOT NULL DEFAULT 0, last_seller_at REAL NOT NULL DEFAULT 0,
 ignore_sent_for REAL NOT NULL DEFAULT 0
);
CREATE TABLE IF NOT EXISTS events (id TEXT PRIMARY KEY, processed_at REAL NOT NULL);
CREATE TABLE IF NOT EXISTS jobs (
 id TEXT PRIMARY KEY, kind TEXT NOT NULL, order_id TEXT REFERENCES orders(id), due_at REAL NOT NULL,
 state TEXT NOT NULL DEFAULT 'pending'
);
CREATE TABLE IF NOT EXISTS expenses (
 id TEXT PRIMARY KEY, order_id TEXT, item_id TEXT, kind TEXT NOT NULL, amount INTEGER NOT NULL,
 state TEXT NOT NULL, day TEXT NOT NULL, created_at REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS listing_actions (
 id TEXT PRIMARY KEY, item_id TEXT NOT NULL, kind TEXT NOT NULL, state TEXT NOT NULL,
 price INTEGER NOT NULL, tier_id TEXT NOT NULL, created_at REAL NOT NULL, error TEXT
);
CREATE TABLE IF NOT EXISTS audit (
 id INTEGER PRIMARY KEY AUTOINCREMENT, event TEXT NOT NULL, reference TEXT NOT NULL,
 created_at REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS own_accounts (
 id INTEGER PRIMARY KEY AUTOINCREMENT, item_id TEXT NOT NULL, label TEXT NOT NULL,
 ciphertext TEXT NOT NULL, cost INTEGER, state TEXT NOT NULL DEFAULT 'free'
 CHECK(state IN ('free','reserved','active','reset')), order_id TEXT UNIQUE, expires_at REAL
);
CREATE INDEX IF NOT EXISTS own_free ON own_accounts(item_id,state,id);
CREATE TABLE IF NOT EXISTS own_leases (
 order_id TEXT PRIMARY KEY REFERENCES orders(id), account_id INTEGER NOT NULL REFERENCES own_accounts(id),
 ciphertext TEXT NOT NULL, expires_at REAL NOT NULL, state TEXT NOT NULL DEFAULT 'reserved'
);
CREATE TABLE IF NOT EXISTS own_extensions (
 order_id TEXT PRIMARY KEY REFERENCES orders(id), rental_id TEXT NOT NULL REFERENCES own_leases(order_id)
);
CREATE TABLE IF NOT EXISTS bonus_stock (
 id INTEGER PRIMARY KEY AUTOINCREMENT, ciphertext TEXT NOT NULL, cost INTEGER,
 state TEXT NOT NULL DEFAULT 'free' CHECK(state IN ('free','reserved','used'))
);
CREATE TABLE IF NOT EXISTS bonus_claims (
 id INTEGER PRIMARY KEY AUTOINCREMENT, campaign TEXT NOT NULL, buyer_id TEXT NOT NULL,
 milestone INTEGER NOT NULL, threshold INTEGER NOT NULL, since REAL NOT NULL,
 order_id TEXT NOT NULL REFERENCES orders(id), stock_id INTEGER REFERENCES bonus_stock(id),
 state TEXT NOT NULL DEFAULT 'waiting', cost INTEGER, generation INTEGER NOT NULL DEFAULT 0,
 exposed INTEGER NOT NULL DEFAULT 0, created_at REAL NOT NULL,
 UNIQUE(campaign,buyer_id,milestone)
);
CREATE TABLE IF NOT EXISTS chat_messages (
 id TEXT PRIMARY KEY, chat_id TEXT NOT NULL, sender TEXT NOT NULL,
 ciphertext TEXT NOT NULL, created_at REAL NOT NULL, needs_reply INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS chat_history ON chat_messages(chat_id,created_at);

CREATE TABLE IF NOT EXISTS providers (
 id INTEGER PRIMARY KEY AUTOINCREMENT, preset TEXT NOT NULL UNIQUE, title TEXT NOT NULL,
 token TEXT NOT NULL, revision INTEGER NOT NULL DEFAULT 1, checked_revision INTEGER NOT NULL DEFAULT 0,
 enabled INTEGER NOT NULL DEFAULT 0, settings TEXT NOT NULL DEFAULT '{}', checked_at REAL
);
CREATE TABLE IF NOT EXISTS external_bindings (
 item_id TEXT PRIMARY KEY REFERENCES bindings(item_id), provider_id INTEGER NOT NULL REFERENCES providers(id),
 service_id TEXT NOT NULL DEFAULT '', quantity INTEGER NOT NULL CHECK(quantity>0),
 payment_method TEXT NOT NULL DEFAULT 'balance', domains TEXT NOT NULL DEFAULT '[]',
 max_native TEXT NOT NULL, currency TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS provider_orders (
 order_id TEXT PRIMARY KEY REFERENCES orders(id), provider_id INTEGER NOT NULL REFERENCES providers(id),
 revision INTEGER NOT NULL, state TEXT NOT NULL DEFAULT 'waiting_input',
 destination TEXT, confirmation TEXT UNIQUE, confirm_expires REAL,
 quote TEXT, snapshot TEXT NOT NULL, remote_id TEXT,
 native_amount TEXT, currency TEXT, rub_rate TEXT, cost_rub INTEGER,
 cost_known INTEGER NOT NULL DEFAULT 0, supplier_refund TEXT NOT NULL DEFAULT '0',
 remains INTEGER,
 error TEXT, next_poll REAL NOT NULL DEFAULT 0, created_at REAL NOT NULL, updated_at REAL NOT NULL,
 UNIQUE(provider_id,remote_id)
);
CREATE INDEX IF NOT EXISTS provider_queue ON provider_orders(state,next_poll);
CREATE TABLE IF NOT EXISTS provider_actions (
 order_id TEXT NOT NULL REFERENCES provider_orders(order_id), kind TEXT NOT NULL,
 state TEXT NOT NULL, remote_id TEXT, created_at REAL NOT NULL,
 PRIMARY KEY(order_id,kind)
);

