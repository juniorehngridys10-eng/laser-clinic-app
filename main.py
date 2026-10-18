import sqlite3
import datetime
import time
import os
import shutil
import json
import re
import hashlib
import secrets
import hmac
import base64
from typing import List, Optional
from pydantic import BaseModel
from fastapi import FastAPI, HTTPException, Depends, Header, UploadFile, File
from fastapi.staticfiles import StaticFiles
from fastapi.responses import FileResponse, JSONResponse, RedirectResponse
from fastapi.middleware.cors import CORSMiddleware

BASE_DIR = os.path.abspath(os.path.dirname(__file__))
# Suporte automático a Persistent Disk do Render (/var/data) ou pasta local
DATA_DIR = os.environ.get("DATA_DIR", "/var/data" if os.path.exists("/var/data") else BASE_DIR)
os.makedirs(DATA_DIR, exist_ok=True)

DB_PATH = os.path.join(DATA_DIR, "clinic.db")
STATIC_DIR = os.path.join(BASE_DIR, "static")
UPLOAD_DIR = os.path.join(DATA_DIR, "uploads") if os.path.exists("/var/data") else os.path.join(STATIC_DIR, "uploads")
os.makedirs(UPLOAD_DIR, exist_ok=True)

ADMIN_PIN = "1234"  # Senha padrão / fallback de acesso
SECRET_KEY = os.environ.get("SECRET_KEY", "laser-clinic-saas-secret-key-2026-xyz-token")

def slugify(text: str) -> str:
    text = (text or "").lower().strip()
    text = re.sub(r'[àáâãäå]', 'a', text)
    text = re.sub(r'[èéêë]', 'e', text)
    text = re.sub(r'[ìíîï]', 'i', text)
    text = re.sub(r'[òóôõö]', 'o', text)
    text = re.sub(r'[ùúûü]', 'u', text)
    text = re.sub(r'[ç]', 'c', text)
    text = re.sub(r'[^a-z0-9\-]', '-', text)
    text = re.sub(r'-+', '-', text).strip('-')
    return text or "clinica"

def hash_password(password: str) -> str:
    salt = secrets.token_hex(16)
    h = hashlib.pbkdf2_hmac('sha256', password.encode('utf-8'), salt.encode('utf-8'), 100000)
    return f"{salt}:{h.hex()}"

def verify_password(password: str, hashed: str) -> bool:
    if not hashed or ":" not in hashed:
        return False
    salt, h = hashed.split(":", 1)
    test_h = hashlib.pbkdf2_hmac('sha256', password.encode('utf-8'), salt.encode('utf-8'), 100000)
    return hmac.compare_digest(test_h.hex(), h)

def create_token(payload: dict, expires_in_seconds: int = 86400 * 30) -> str:
    header = {"alg": "HS256", "typ": "JWT"}
    p = payload.copy()
    p["exp"] = int(time.time()) + expires_in_seconds
    h_b64 = base64.urlsafe_b64encode(json.dumps(header).encode()).decode().rstrip("=")
    p_b64 = base64.urlsafe_b64encode(json.dumps(p).encode()).decode().rstrip("=")
    msg = f"{h_b64}.{p_b64}"
    sig = hmac.new(SECRET_KEY.encode(), msg.encode(), hashlib.sha256).digest()
    sig_b64 = base64.urlsafe_b64encode(sig).decode().rstrip("=")
    return f"{msg}.{sig_b64}"

def decode_token(token: str) -> Optional[dict]:
    try:
        parts = token.split(".")
        if len(parts) != 3:
            return None
        msg = f"{parts[0]}.{parts[1]}"
        expected_sig = hmac.new(SECRET_KEY.encode(), msg.encode(), hashlib.sha256).digest()
        sig_b64 = parts[2]
        rem = len(sig_b64) % 4
        if rem:
            sig_b64 += "=" * (4 - rem)
        sig = base64.urlsafe_b64decode(sig_b64)
        if not hmac.compare_digest(expected_sig, sig):
            return None
        rem_p = len(parts[1]) % 4
        p_str = parts[1] + ("=" * (4 - rem_p) if rem_p else "")
        p = json.loads(base64.urlsafe_b64decode(p_str).decode())
        if p.get("exp") and time.time() > p["exp"]:
            return None
        return p
    except Exception:
        return None

app = FastAPI(title="Laser Clinic Agendamento & Gestão Completa Multi-Clínica")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

def get_db():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn

def init_db():
    conn = get_db()
    cursor = conn.cursor()

    # 1. Tabela Multi-Clínica (Tenants)
    cursor.execute("""
    CREATE TABLE IF NOT EXISTS clinics (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        slug TEXT UNIQUE NOT NULL,
        name TEXT NOT NULL,
        subtitle TEXT DEFAULT 'Depilação a Laser e Estética Avançada',
        logo_url TEXT DEFAULT '',
        phone TEXT DEFAULT '',
        instagram TEXT DEFAULT '',
        opening_time TEXT DEFAULT '08:00',
        closing_time TEXT DEFAULT '18:00',
        slot_interval_minutes INTEGER DEFAULT 30,
        has_lunch_break INTEGER DEFAULT 0,
        lunch_start TEXT DEFAULT '12:00',
        lunch_end TEXT DEFAULT '13:00',
        working_days TEXT DEFAULT '["1","2","3","4","5","6"]',
        plan TEXT DEFAULT 'trial',
        subscription_status TEXT DEFAULT 'active',
        trial_ends_at TEXT,
        subscription_ends_at TEXT,
        monthly_price REAL DEFAULT 79.0,
        is_blocked INTEGER DEFAULT 0,
        notes TEXT DEFAULT '',
        segment TEXT DEFAULT 'laser',
        created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
    );
    """)

    cursor.execute("PRAGMA table_info(clinics);")
    c_cols = [c[1] for c in cursor.fetchall()]
    if "subscription_ends_at" not in c_cols:
        cursor.execute("ALTER TABLE clinics ADD COLUMN subscription_ends_at TEXT;")
    if "monthly_price" not in c_cols:
        cursor.execute("ALTER TABLE clinics ADD COLUMN monthly_price REAL DEFAULT 79.0;")
    if "is_blocked" not in c_cols:
        cursor.execute("ALTER TABLE clinics ADD COLUMN is_blocked INTEGER DEFAULT 0;")
    if "notes" not in c_cols:
        cursor.execute("ALTER TABLE clinics ADD COLUMN notes TEXT DEFAULT '';")
    if "segment" not in c_cols:
        cursor.execute("ALTER TABLE clinics ADD COLUMN segment TEXT DEFAULT 'laser';")

    # Tabela de Configuração Master do SaaS (Chave PIX e dados de recebimento do SuperAdmin)
    cursor.execute("""
    CREATE TABLE IF NOT EXISTS saas_config (
        key TEXT PRIMARY KEY,
        value TEXT
    );
    """)
    for k, v in [
        ("superadmin_pix_key", ""),
        ("superadmin_pix_name", "Administrador"),
        ("superadmin_whatsapp", "11987654321"),
        ("default_monthly_price", "79.0")
    ]:
        cursor.execute("INSERT OR IGNORE INTO saas_config (key, value) VALUES (?, ?)", (k, v))

    # 2. Tabela de Usuários (Donos e Equipe)
    cursor.execute("""
    CREATE TABLE IF NOT EXISTS users (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        clinic_id INTEGER NOT NULL,
        name TEXT NOT NULL,
        email TEXT UNIQUE NOT NULL,
        password_hash TEXT NOT NULL,
        role TEXT DEFAULT 'owner',
        is_superadmin INTEGER DEFAULT 0,
        created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
        FOREIGN KEY (clinic_id) REFERENCES clinics (id)
    );
    """)

    # Tabela Legada de Configurações para compatibilidade
    cursor.execute("""
    CREATE TABLE IF NOT EXISTS clinic_settings (
        id INTEGER PRIMARY KEY,
        name TEXT NOT NULL,
        subtitle TEXT,
        logo_url TEXT,
        phone TEXT,
        instagram TEXT
    );
    """)

    # Migração da clínica padrão id=1
    cursor.execute("SELECT COUNT(*) FROM clinics")
    if cursor.fetchone()[0] == 0:
        cursor.execute("SELECT * FROM clinic_settings WHERE id = 1")
        old_row = cursor.fetchone()
        now_dt = datetime.datetime.now()
        trial_dt = (now_dt + datetime.timedelta(days=15)).strftime("%Y-%m-%d %H:%M:%S")
        if old_row:
            d = dict(old_row)
            cursor.execute("""
            INSERT INTO clinics (id, slug, name, subtitle, logo_url, phone, instagram,
                                 opening_time, closing_time, slot_interval_minutes,
                                 has_lunch_break, lunch_start, lunch_end, working_days,
                                 plan, subscription_status, trial_ends_at)
            VALUES (1, 'laserglow', ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'pro', 'active', ?)
            """, (
                d.get("name") or "LaserGlow Clinic",
                d.get("subtitle") or "Depilação a Laser de Alta Tecnologia",
                d.get("logo_url") or "",
                d.get("phone") or "11987654321",
                d.get("instagram") or "@laser.glow",
                d.get("opening_time") or "08:00",
                d.get("closing_time") or "18:00",
                d.get("slot_interval_minutes") or 30,
                d.get("has_lunch_break") or 0,
                d.get("lunch_start") or "12:00",
                d.get("lunch_end") or "13:00",
                d.get("working_days") or '["1","2","3","4","5","6"]',
                trial_dt
            ))
        else:
            cursor.execute("""
            INSERT INTO clinics (id, slug, name, subtitle, logo_url, phone, instagram, plan, subscription_status, trial_ends_at)
            VALUES (1, 'laserglow', 'LaserGlow Clinic', 'Depilação a Laser de Alta Tecnologia', '', '11987654321', '@laser.glow', 'pro', 'active', ?)
            """, (trial_dt,))

    # Garantir usuário admin da clínica 1
    cursor.execute("SELECT COUNT(*) FROM users WHERE email = 'admin@laserglow.com'")
    if cursor.fetchone()[0] == 0:
        cursor.execute("""
        INSERT INTO users (clinic_id, name, email, password_hash, role, is_superadmin)
        VALUES (1, 'Administrador', 'admin@laserglow.com', ?, 'owner', 1)
        """, (hash_password("1234"),))

    # 3. Tabela de Serviços e Pacotes da Clínica
    cursor.execute("""
    CREATE TABLE IF NOT EXISTS services (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        clinic_id INTEGER DEFAULT 1,
        name TEXT NOT NULL,
        category TEXT DEFAULT 'Sessão Avulsa',
        sessions_count INTEGER DEFAULT 1,
        duration_minutes INTEGER DEFAULT 30,
        price REAL NOT NULL
    );
    """)

    cursor.execute("PRAGMA table_info(services);")
    cols = [col[1] for col in cursor.fetchall()]
    if "category" not in cols:
        cursor.execute("ALTER TABLE services ADD COLUMN category TEXT DEFAULT 'Sessão Avulsa';")
    if "sessions_count" not in cols:
        cursor.execute("ALTER TABLE services ADD COLUMN sessions_count INTEGER DEFAULT 1;")
    if "clinic_id" not in cols:
        cursor.execute("ALTER TABLE services ADD COLUMN clinic_id INTEGER DEFAULT 1;")

    # 4. Tabela de Clientes
    cursor.execute("""
    CREATE TABLE IF NOT EXISTS clients (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        clinic_id INTEGER DEFAULT 1,
        name TEXT NOT NULL,
        phone TEXT NOT NULL,
        email TEXT,
        cpf TEXT,
        birth_date TEXT,
        notes TEXT,
        created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
    );
    """)
    cursor.execute("PRAGMA table_info(clients);")
    client_cols = [c[1] for c in cursor.fetchall()]
    if "clinic_id" not in client_cols:
        cursor.execute("ALTER TABLE clients ADD COLUMN clinic_id INTEGER DEFAULT 1;")
    if "cpf" not in client_cols:
        cursor.execute("ALTER TABLE clients ADD COLUMN cpf TEXT;")
    if "birth_date" not in client_cols:
        cursor.execute("ALTER TABLE clients ADD COLUMN birth_date TEXT;")
    if "notes" not in client_cols:
        cursor.execute("ALTER TABLE clients ADD COLUMN notes TEXT;")

    # 5. Tabela de Agendamentos
    cursor.execute("""
    CREATE TABLE IF NOT EXISTS appointments (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        clinic_id INTEGER DEFAULT 1,
        client_id INTEGER NOT NULL,
        service_id INTEGER NOT NULL,
        appointment_date TEXT NOT NULL,
        appointment_time TEXT NOT NULL,
        status TEXT DEFAULT 'agendado',
        notes TEXT,
        created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
        FOREIGN KEY (client_id) REFERENCES clients (id),
        FOREIGN KEY (service_id) REFERENCES services (id)
    );
    """)
    cursor.execute("PRAGMA table_info(appointments);")
    app_cols = [c[1] for c in cursor.fetchall()]
    if "clinic_id" not in app_cols:
        cursor.execute("ALTER TABLE appointments ADD COLUMN clinic_id INTEGER DEFAULT 1;")

    # 6. Tabela de Controle de Pacotes Vendidos aos Clientes
    cursor.execute("""
    CREATE TABLE IF NOT EXISTS client_packages (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        clinic_id INTEGER DEFAULT 1,
        client_id INTEGER NOT NULL,
        service_id INTEGER NOT NULL,
        total_sessions INTEGER NOT NULL,
        completed_sessions INTEGER DEFAULT 0,
        price_paid REAL NOT NULL,
        payment_method TEXT DEFAULT 'Pix',
        status TEXT DEFAULT 'ativo',
        purchased_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
        notes TEXT,
        FOREIGN KEY (client_id) REFERENCES clients (id),
        FOREIGN KEY (service_id) REFERENCES services (id)
    );
    """)
    cursor.execute("PRAGMA table_info(client_packages);")
    cp_cols = [c[1] for c in cursor.fetchall()]
    if "clinic_id" not in cp_cols:
        cursor.execute("ALTER TABLE client_packages ADD COLUMN clinic_id INTEGER DEFAULT 1;")

    # 7. Tabela de Fluxo de Caixa (Entradas e Saídas)
    cursor.execute("""
    CREATE TABLE IF NOT EXISTS cash_flow (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        clinic_id INTEGER DEFAULT 1,
        type TEXT NOT NULL,
        category TEXT NOT NULL,
        description TEXT NOT NULL,
        amount REAL NOT NULL,
        payment_method TEXT DEFAULT 'Pix',
        transaction_date TEXT NOT NULL,
        client_id INTEGER,
        created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
        FOREIGN KEY (client_id) REFERENCES clients (id)
    );
    """)
    cursor.execute("PRAGMA table_info(cash_flow);")
    cf_cols = [c[1] for c in cursor.fetchall()]
    if "clinic_id" not in cf_cols:
        cursor.execute("ALTER TABLE cash_flow ADD COLUMN clinic_id INTEGER DEFAULT 1;")

    # 8. Tabela de Ficha de Avaliação e Anamnese do Cliente (Luz e Pele)
    cursor.execute("""
    CREATE TABLE IF NOT EXISTS anamnese_forms (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        clinic_id INTEGER DEFAULT 1,
        client_id INTEGER NOT NULL,
        contract_number TEXT,
        sexo TEXT,
        endereco TEXT,
        bairro TEXT,
        cep TEXT,
        cidade TEXT,
        uf TEXT,
        tel_fixo TEXT,
        instagram TEXT,
        profissao TEXT,
        como_conheceu TEXT,
        tipo_tratamento TEXT,
        areas_tratamento TEXT,
        exposicao_sol_48h TEXT,
        historico_patologias TEXT,
        questionario_medico TEXT,
        reacao_solar TEXT,
        termo_aceito INTEGER DEFAULT 1,
        signature_name TEXT,
        signature_date TEXT,
        signature_image TEXT,
        created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
        updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
        FOREIGN KEY (client_id) REFERENCES clients (id)
    );
    """)
    cursor.execute("PRAGMA table_info(anamnese_forms);")
    af_cols = [c[1] for c in cursor.fetchall()]
    if "clinic_id" not in af_cols:
        cursor.execute("ALTER TABLE anamnese_forms ADD COLUMN clinic_id INTEGER DEFAULT 1;")
    if "signature_image" not in af_cols:
        cursor.execute("ALTER TABLE anamnese_forms ADD COLUMN signature_image TEXT;")

    # Procedimentos padrão se clínica 1 estiver vazia
    cursor.execute("SELECT COUNT(*) FROM services WHERE clinic_id = 1")
    count = cursor.fetchone()[0]
    if count == 0:
        seed_default_services_for_clinic(1, cursor)

    conn.commit()
    conn.close()

def seed_default_services_for_clinic(clinic_id: int, cursor=None, segment: str = "laser"):
    segment = (segment or "laser").lower()

    if any(w in segment for w in ["salao", "salão", "cabel", "beleza", "hair"]):
        default_services = [
            ("Corte Feminino & Escova", "Cabelos", 1, 45, 89.90, clinic_id),
            ("Escova Modelada & Lavagem", "Cabelos", 1, 30, 49.90, clinic_id),
            ("Manicure & Pedicure Completa", "Unhas", 1, 60, 65.00, clinic_id),
            ("Design de Sobrancelhas", "Estética Facial", 1, 30, 45.00, clinic_id),
            ("Coloração / Retoque de Raiz", "Cabelos", 1, 60, 140.00, clinic_id),
            ("Mechas / Luzes & Matização", "Cabelos", 1, 90, 260.00, clinic_id),
            ("Hidratação Profunda & Escova", "Cabelos", 1, 45, 95.00, clinic_id),
            ("Botox Capilar / Alinhamento", "Cabelos", 1, 60, 180.00, clinic_id),
            ("Spa dos Pés & Esmaltação", "Unhas", 1, 45, 55.00, clinic_id),
            ("Pacote Fidelidade: 4 Escovas + 4 Manicures", "Pacotes Promocionais", 8, 45, 320.00, clinic_id)
        ]
    elif any(w in segment for w in ["barbearia", "barba"]):
        default_services = [
            ("Corte Degradê / Social", "Cabelo", 1, 30, 45.00, clinic_id),
            ("Barba com Toalha Quente", "Barba", 1, 30, 40.00, clinic_id),
            ("Combo: Cabelo + Barba", "Combos", 1, 45, 75.00, clinic_id),
            ("Pigmentação de Barba", "Barba", 1, 20, 30.00, clinic_id),
            ("Sobrancelha na Navalha", "Acabamento", 1, 15, 20.00, clinic_id),
            ("Lavagem & Penteado Pomada", "Cabelo", 1, 20, 25.00, clinic_id),
            ("Limpeza de Pele Express", "Estética", 1, 30, 50.00, clinic_id),
            ("Platinado / Nevou", "Química", 1, 90, 150.00, clinic_id),
            ("Selagem / Alinhamento Capilar", "Química", 1, 45, 90.00, clinic_id),
            ("Clube da Barba: 4 Cortes + 4 Barbas", "Assinatura VIP", 8, 45, 220.00, clinic_id)
        ]
    elif any(w in segment for w in ["unha", "nail", "sobrancelha"]):
        default_services = [
            ("Alongamento em Fibra de Vidro", "Unhas", 1, 90, 160.00, clinic_id),
            ("Manutenção de Fibra", "Unhas", 1, 60, 100.00, clinic_id),
            ("Esmaltação em Gel", "Unhas", 1, 45, 70.00, clinic_id),
            ("Manicure Tradicional", "Unhas", 1, 40, 35.00, clinic_id),
            ("Pedicure Tradicional", "Unhas", 1, 45, 40.00, clinic_id),
            ("Design de Sobrancelha com Henna", "Sobrancelhas", 1, 35, 55.00, clinic_id),
            ("Lash Lifting / Permanente de Cílios", "Cílios", 1, 60, 120.00, clinic_id),
            ("Extensão de Cílios Fio a Fio", "Cílios", 1, 90, 150.00, clinic_id),
            ("Spa dos Pés com Parafina", "Unhas", 1, 40, 60.00, clinic_id),
            ("Pacote 4 Manicures do Mês", "Pacotes Promocionais", 4, 40, 120.00, clinic_id)
        ]
    else:
        # Padrão Laser e Estética Avançada
        default_services = [
            ("Axilas (Avulsa)", "Sessão Avulsa", 1, 30, 89.90, clinic_id),
            ("Virilha Completa (Avulsa)", "Sessão Avulsa", 1, 30, 149.90, clinic_id),
            ("Pernas Inteiras (Avulsa)", "Sessão Avulsa", 1, 45, 199.90, clinic_id),
            ("Barba Completa (Avulsa)", "Sessão Avulsa", 1, 30, 119.90, clinic_id),
            ("Faixa de Barba (Avulsa)", "Sessão Avulsa", 1, 20, 69.90, clinic_id),
            ("Buço / Queixo (Avulsa)", "Sessão Avulsa", 1, 15, 49.90, clinic_id),
            ("Pacote Axilas (10 Sessões)", "Pacote Promocional", 10, 30, 499.00, clinic_id),
            ("Pacote Virilha Completa (10 Sessões)", "Pacote Promocional", 10, 30, 899.00, clinic_id),
            ("Pacote Pernas Inteiras (10 Sessões)", "Pacote Promocional", 10, 45, 1290.00, clinic_id),
            ("Combo VIP: Axilas + Virilha (10 Sessões)", "Pacote Promocional", 10, 45, 1190.00, clinic_id)
        ]

    if cursor:
        cursor.executemany("INSERT INTO services (name, category, sessions_count, duration_minutes, price, clinic_id) VALUES (?, ?, ?, ?, ?, ?)", default_services)
    else:
        conn = get_db()
        conn.cursor().executemany("INSERT INTO services (name, category, sessions_count, duration_minutes, price, clinic_id) VALUES (?, ?, ?, ?, ?, ?)", default_services)
        conn.commit()
        conn.close()

init_db()

# Models
class BookingRequest(BaseModel):
    name: str
    phone: str
    email: Optional[str] = None
    service_id: int
    appointment_date: str
    appointment_time: str
    notes: Optional[str] = None

class RescheduleRequest(BaseModel):
    appointment_date: str
    appointment_time: str

class StatusUpdateRequest(BaseModel):
    status: str

class ServiceItem(BaseModel):
    name: str
    category: str
    sessions_count: int = 1
    duration_minutes: int = 30
    price: float

class AdminLoginRequest(BaseModel):
    pin: Optional[str] = None

class RegisterClinicRequest(BaseModel):
    clinic_name: str
    owner_name: str
    email: str
    password: str
    phone: Optional[str] = ""
    slug: Optional[str] = None
    segment: Optional[str] = "salao"

class LoginRequest(BaseModel):
    email: Optional[str] = None
    password: Optional[str] = None
    pin: Optional[str] = None

class ClinicSettingsUpdate(BaseModel):
    name: str
    subtitle: Optional[str] = ""
    logo_url: Optional[str] = ""
    phone: Optional[str] = ""
    instagram: Optional[str] = ""
    opening_time: Optional[str] = "08:00"
    closing_time: Optional[str] = "18:00"
    slot_interval_minutes: Optional[int] = 30
    has_lunch_break: Optional[int] = 0
    lunch_start: Optional[str] = "12:00"
    lunch_end: Optional[str] = "13:00"
    working_days: Optional[str] = '["1","2","3","4","5","6"]'

class ClientCreateOrUpdate(BaseModel):
    name: str
    phone: str
    email: Optional[str] = ""
    cpf: Optional[str] = ""
    birth_date: Optional[str] = ""
    notes: Optional[str] = ""

class AnamneseFormModel(BaseModel):
    contract_number: Optional[str] = ""
    sexo: Optional[str] = ""
    endereco: Optional[str] = ""
    bairro: Optional[str] = ""
    cep: Optional[str] = ""
    cidade: Optional[str] = ""
    uf: Optional[str] = ""
    tel_fixo: Optional[str] = ""
    instagram: Optional[str] = ""
    profissao: Optional[str] = ""
    como_conheceu: Optional[str] = ""
    tipo_tratamento: Optional[str] = "Depilação"
    areas_tratamento: Optional[str] = "[]"
    exposicao_sol_48h: Optional[str] = "Não"
    historico_patologias: Optional[str] = "[]"
    questionario_medico: Optional[str] = "{}"
    reacao_solar: Optional[str] = ""
    signature_name: Optional[str] = ""
    signature_date: Optional[str] = ""
    signature_image: Optional[str] = ""

class PublicClientFichaSubmit(BaseModel):
    name: str
    phone: str
    email: Optional[str] = ""
    cpf: Optional[str] = ""
    birth_date: Optional[str] = ""
    contract_number: Optional[str] = ""
    sexo: Optional[str] = "F"
    endereco: Optional[str] = ""
    bairro: Optional[str] = ""
    cep: Optional[str] = ""
    cidade: Optional[str] = ""
    uf: Optional[str] = ""
    tel_fixo: Optional[str] = ""
    instagram: Optional[str] = ""
    profissao: Optional[str] = ""
    como_conheceu: Optional[str] = ""
    tipo_tratamento: Optional[str] = "Depilação"
    areas_tratamento: Optional[str] = "[]"
    exposicao_sol_48h: Optional[str] = "Não"
    historico_patologias: Optional[str] = "[]"
    questionario_medico: Optional[str] = "{}"
    reacao_solar: Optional[str] = ""
    signature_name: Optional[str] = ""
    signature_date: Optional[str] = ""
    signature_image: Optional[str] = ""

class SellPackageRequest(BaseModel):
    client_id: int
    service_id: int
    price_paid: float
    payment_method: str = "Pix"
    notes: Optional[str] = ""

class CashFlowEntry(BaseModel):
    type: str  # 'entrada' ou 'saida'
    category: str
    description: str
    amount: float
    payment_method: str = "Pix"
    transaction_date: str
    client_id: Optional[int] = None

class SaasConfigUpdate(BaseModel):
    pix_key: Optional[str] = ""
    pix_name: Optional[str] = ""
    whatsapp: Optional[str] = ""
    default_monthly_price: Optional[float] = 79.0

class ClinicRenewRequest(BaseModel):
    days: Optional[int] = 30
    plan: Optional[str] = "essencial"
    monthly_price: Optional[float] = None

class ClinicExtendTrialRequest(BaseModel):
    days: Optional[int] = 7

# --- Dependência de Autenticação Multi-Tenant ---

def get_current_user(
    authorization: Optional[str] = Header(None),
    x_admin_pin: Optional[str] = Header(None)
):
    token = None
    if authorization and authorization.startswith("Bearer "):
        token = authorization.split("Bearer ", 1)[1].strip()
    elif x_admin_pin and len(x_admin_pin) > 15:
        token = x_admin_pin

    if token:
        payload = decode_token(token)
        if payload and payload.get("user_id") and payload.get("clinic_id"):
            conn = get_db()
            user = conn.execute("SELECT * FROM users WHERE id = ?", (payload["user_id"],)).fetchone()
            clinic = conn.execute("SELECT * FROM clinics WHERE id = ?", (payload["clinic_id"],)).fetchone()
            conn.close()
            if user and clinic:
                c_dict = dict(clinic)
                c_dict["segment"] = resolve_clinic_segment(c_dict)
                return {
                    "id": user["id"],
                    "clinic_id": clinic["id"],
                    "name": user["name"],
                    "email": user["email"],
                    "role": user["role"],
                    "is_superadmin": user["is_superadmin"],
                    "is_blocked": int(clinic["is_blocked"]) if "is_blocked" in clinic.keys() and clinic["is_blocked"] else 0,
                    "clinic": c_dict
                }

    # Fallback para PIN legado (1234) ou Bearer 1234 (Clínica padrão 1)
    if x_admin_pin == ADMIN_PIN or authorization == f"Bearer {ADMIN_PIN}":
        conn = get_db()
        clinic = conn.execute("SELECT * FROM clinics WHERE id = 1").fetchone()
        user = conn.execute("SELECT * FROM users WHERE clinic_id = 1 LIMIT 1").fetchone()
        conn.close()
        c_dict = dict(clinic) if clinic else {"id": 1, "name": "LaserGlow Clinic", "slug": "laserglow"}
        c_dict["segment"] = resolve_clinic_segment(c_dict)
        return {
            "id": user["id"] if user else 1,
            "clinic_id": 1,
            "name": user["name"] if user else "Admin",
            "email": user["email"] if user else "admin@laserglow.com",
            "role": "owner",
            "is_superadmin": 1,
            "is_blocked": 0,
            "clinic": c_dict
        }

    raise HTTPException(status_code=401, detail="Sessão não autorizada ou expirada. Faça login.")

def verify_admin(current_user: dict = Depends(get_current_user)):
    if current_user.get("is_blocked") and not current_user.get("is_superadmin") and current_user.get("clinic_id") != 1:
        raise HTTPException(
            status_code=403,
            detail="Acesso bloqueado. A assinatura da sua clínica está suspensa por pendência financeira. Realize o pagamento via PIX para reativar."
        )
    return current_user

def verify_superadmin(current_user: dict = Depends(get_current_user)):
    if not current_user.get("is_superadmin") and current_user.get("clinic_id") != 1:
        raise HTTPException(status_code=403, detail="Acesso restrito ao SuperAdmin do sistema.")
    return current_user

# --- Funções Auxiliares de Clínica e Agenda ---

def resolve_clinic_segment(clinic: dict) -> str:
    seg = (clinic.get("segment") or "").strip().lower()
    if seg in ("salao", "barbearia", "unhas", "laser"):
        return seg
    name_and_sub = ((clinic.get("name") or "") + " " + (clinic.get("subtitle") or "")).lower()
    if any(w in name_and_sub for w in ["salao", "salão", "hair", "cabel", "beleza"]):
        return "salao"
    if any(w in name_and_sub for w in ["barba", "barbearia"]):
        return "barbearia"
    if any(w in name_and_sub for w in ["unha", "nail", "sobrancelha", "esmalte", "esmalteria"]):
        return "unhas"
    return "laser"

def get_clinic_by_slug(slug: str):
    conn = get_db()
    c = conn.execute("SELECT * FROM clinics WHERE slug = ?", (slug,)).fetchone()
    conn.close()
    if not c:
        raise HTTPException(status_code=404, detail="Clínica não encontrada. Verifique o link de agendamento.")
    d = dict(c)
    d["segment"] = resolve_clinic_segment(d)
    return d

def get_clinic_by_id(clinic_id: int):
    conn = get_db()
    c = conn.execute("SELECT * FROM clinics WHERE id = ?", (clinic_id,)).fetchone()
    conn.close()
    if not c:
        raise HTTPException(status_code=404, detail="Clínica não encontrada.")
    d = dict(c)
    d["segment"] = resolve_clinic_segment(d)
    return d

def compute_available_slots(clinic: dict, date: str):
    conn = get_db()
    booked = conn.execute(
        "SELECT appointment_time FROM appointments WHERE clinic_id = ? AND appointment_date = ? AND status != 'cancelado'",
        (clinic["id"], date)
    ).fetchall()
    conn.close()

    booked_times = set(r["appointment_time"] for r in booked)

    opening_time = clinic.get("opening_time") or "08:00"
    closing_time = clinic.get("closing_time") or "18:00"
    interval = int(clinic.get("slot_interval_minutes") or 30)
    has_lunch = bool(clinic.get("has_lunch_break"))
    lunch_s = clinic.get("lunch_start") or "12:00"
    lunch_e = clinic.get("lunch_end") or "13:00"
    working_days_raw = clinic.get("working_days") or '["1","2","3","4","5","6"]'

    try:
        working_days = [str(d) for d in json.loads(working_days_raw)]
    except Exception:
        working_days = ["1", "2", "3", "4", "5", "6"]

    try:
        dt = datetime.date.fromisoformat(date)
        weekday_num = str(dt.strftime("%w"))
        if weekday_num not in working_days:
            return {
                "date": date,
                "closed": True,
                "message": "A clínica não tem atendimento neste dia da semana.",
                "slots": []
            }
    except Exception:
        pass

    def time_to_min(t_str):
        p = t_str.split(":")
        return int(p[0]) * 60 + int(p[1])

    try:
        start_min = time_to_min(opening_time)
        end_min = time_to_min(closing_time)
        lunch_start_min = time_to_min(lunch_s) if has_lunch else None
        lunch_end_min = time_to_min(lunch_e) if has_lunch else None
    except Exception:
        start_min = 8 * 60
        end_min = 18 * 60
        lunch_start_min = None
        lunch_end_min = None

    if interval <= 0:
        interval = 30

    all_slots = []
    curr = start_min
    while curr + interval <= end_min:
        slot_start = curr
        slot_end = curr + interval

        is_lunch = False
        if has_lunch and lunch_start_min is not None and lunch_end_min is not None:
            if not (slot_end <= lunch_start_min or slot_start >= lunch_end_min):
                is_lunch = True

        if not is_lunch:
            h = slot_start // 60
            m = slot_start % 60
            all_slots.append(f"{h:02d}:{m:02d}")

        curr += interval

    slots_status = [
        {"time": slot, "available": slot not in booked_times}
        for slot in all_slots
    ]
    return {"date": date, "closed": False, "slots": slots_status}

def process_booking(clinic_id: int, data: BookingRequest):
    conn = get_db()
    cursor = conn.cursor()

    clinic = cursor.execute("SELECT * FROM clinics WHERE id = ?", (clinic_id,)).fetchone()
    if not clinic:
        conn.close()
        raise HTTPException(status_code=404, detail="Clínica não encontrada.")

    if clinic["working_days"]:
        try:
            working_days = [str(d) for d in json.loads(clinic["working_days"])]
            dt = datetime.date.fromisoformat(data.appointment_date)
            if str(dt.strftime("%w")) not in working_days:
                conn.close()
                raise HTTPException(status_code=400, detail="A clínica não possui atendimento na data escolhida.")
        except HTTPException:
            raise
        except Exception:
            pass

    clean_phone = "".join(filter(str.isdigit, data.phone))
    if len(clean_phone) < 10:
        raise HTTPException(status_code=400, detail="Telefone/WhatsApp inválido. Inclua o DDD.")
    if not clean_phone.startswith("55") and len(clean_phone) in (10, 11):
        clean_phone = "55" + clean_phone

    conflict = cursor.execute(
        "SELECT id FROM appointments WHERE clinic_id = ? AND appointment_date = ? AND appointment_time = ? AND status != 'cancelado'",
        (clinic_id, data.appointment_date, data.appointment_time)
    ).fetchone()
    if conflict:
        conn.close()
        raise HTTPException(status_code=400, detail="Este horário acabou de ser reservado. Por favor, escolha outro.")

    client = cursor.execute("SELECT id FROM clients WHERE clinic_id = ? AND phone = ?", (clinic_id, clean_phone)).fetchone()
    if client:
        client_id = client["id"]
        cursor.execute("UPDATE clients SET name = ?, email = COALESCE(?, email) WHERE id = ? AND clinic_id = ?", (data.name, data.email, client_id, clinic_id))
    else:
        cursor.execute("INSERT INTO clients (clinic_id, name, phone, email) VALUES (?, ?, ?, ?)", (clinic_id, data.name, clean_phone, data.email))
        client_id = cursor.lastrowid

    cursor.execute("""
        INSERT INTO appointments (clinic_id, client_id, service_id, appointment_date, appointment_time, status, notes)
        VALUES (?, ?, ?, ?, ?, 'agendado', ?)
    """, (clinic_id, client_id, data.service_id, data.appointment_date, data.appointment_time, data.notes or ""))

    app_id = cursor.lastrowid
    conn.commit()

    details = cursor.execute("""
        SELECT a.id, a.appointment_date, a.appointment_time, a.status, s.name as service_name, s.price, c.name as client_name, c.phone
        FROM appointments a
        JOIN services s ON a.service_id = s.id
        JOIN clients c ON a.client_id = c.id
        WHERE a.id = ? AND a.clinic_id = ?
    """, (app_id, clinic_id)).fetchone()
    conn.close()

    return dict(details)

def process_client_ficha(clinic_id: int, form: PublicClientFichaSubmit):
    name = form.name.strip()
    if not name:
        raise HTTPException(status_code=400, detail="Por favor, preencha o seu nome completo.")

    clean_phone = "".join(filter(str.isdigit, form.phone))
    if len(clean_phone) < 10:
        raise HTTPException(status_code=400, detail="WhatsApp/Telefone inválido. Inclua o DDD com 2 dígitos.")
    if not clean_phone.startswith("55") and len(clean_phone) in (10, 11):
        clean_phone = "55" + clean_phone

    conn = get_db()
    cursor = conn.cursor()

    client = cursor.execute("SELECT id FROM clients WHERE clinic_id = ? AND phone = ?", (clinic_id, clean_phone)).fetchone()
    if client:
        client_id = client["id"]
        cursor.execute("""
            UPDATE clients 
            SET name = ?, 
                email = CASE WHEN ? != '' THEN ? ELSE email END,
                cpf = CASE WHEN ? != '' THEN ? ELSE cpf END,
                birth_date = CASE WHEN ? != '' THEN ? ELSE birth_date END
            WHERE id = ? AND clinic_id = ?
        """, (name, form.email or "", form.email or "", form.cpf or "", form.cpf or "", form.birth_date or "", form.birth_date or "", client_id, clinic_id))
    else:
        cursor.execute("""
            INSERT INTO clients (clinic_id, name, phone, email, cpf, birth_date)
            VALUES (?, ?, ?, ?, ?, ?)
        """, (clinic_id, name, clean_phone, form.email or "", form.cpf or "", form.birth_date or ""))
        client_id = cursor.lastrowid

    now_str = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    existing_anamnese = cursor.execute("SELECT id FROM anamnese_forms WHERE clinic_id = ? AND client_id = ?", (clinic_id, client_id)).fetchone()

    if existing_anamnese:
        cursor.execute("""
            UPDATE anamnese_forms
            SET contract_number = ?, sexo = ?, endereco = ?, bairro = ?, cep = ?, cidade = ?, uf = ?,
                tel_fixo = ?, instagram = ?, profissao = ?, como_conheceu = ?, tipo_tratamento = ?,
                areas_tratamento = ?, exposicao_sol_48h = ?, historico_patologias = ?,
                questionario_medico = ?, reacao_solar = ?, signature_name = ?, signature_date = ?,
                signature_image = CASE WHEN ? != '' THEN ? ELSE signature_image END,
                updated_at = ?
            WHERE id = ? AND clinic_id = ?
        """, (
            form.contract_number, form.sexo, form.endereco, form.bairro, form.cep, form.cidade, form.uf,
            form.tel_fixo, form.instagram, form.profissao, form.como_conheceu, form.tipo_tratamento,
            form.areas_tratamento, form.exposicao_sol_48h, form.historico_patologias,
            form.questionario_medico, form.reacao_solar, form.signature_name or name,
            form.signature_date or now_str, form.signature_image or "", form.signature_image or "", now_str, existing_anamnese["id"], clinic_id
        ))
    else:
        cursor.execute("""
            INSERT INTO anamnese_forms (
                clinic_id, client_id, contract_number, sexo, endereco, bairro, cep, cidade, uf,
                tel_fixo, instagram, profissao, como_conheceu, tipo_tratamento,
                areas_tratamento, exposicao_sol_48h, historico_patologias,
                questionario_medico, reacao_solar, signature_name, signature_date,
                signature_image, created_at, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """, (
            clinic_id, client_id, form.contract_number, form.sexo, form.endereco, form.bairro, form.cep, form.cidade, form.uf,
            form.tel_fixo, form.instagram, form.profissao, form.como_conheceu, form.tipo_tratamento,
            form.areas_tratamento, form.exposicao_sol_48h, form.historico_patologias,
            form.questionario_medico, form.reacao_solar, form.signature_name or name,
            form.signature_date or now_str, form.signature_image or "", now_str, now_str
        ))

    conn.commit()
    conn.close()
    return {"success": True, "client_id": client_id, "name": name, "message": "Ficha e assinatura digital registradas com sucesso!"}

def process_client_get_ficha(clinic_id: int, phone: Optional[str] = None, client_id: Optional[int] = None):
    if not phone and not client_id:
        return {"client": None, "anamnese": None}
    conn = get_db()
    client = None
    if client_id:
        client = conn.execute("SELECT * FROM clients WHERE clinic_id = ? AND id = ?", (clinic_id, client_id)).fetchone()
    elif phone:
        clean_phone = "".join(filter(str.isdigit, phone))
        if not clean_phone.startswith("55") and len(clean_phone) in (10, 11):
            clean_phone = "55" + clean_phone
        client = conn.execute("SELECT * FROM clients WHERE clinic_id = ? AND (phone = ? OR phone LIKE ?)", (clinic_id, clean_phone, f"%{clean_phone}%")).fetchone()

    if not client:
        conn.close()
        return {"client": None, "anamnese": None}

    c_id = client["id"]
    anamnese = conn.execute("SELECT * FROM anamnese_forms WHERE clinic_id = ? AND client_id = ?", (clinic_id, c_id)).fetchone()
    conn.close()
    return {
        "client": dict(client),
        "anamnese": dict(anamnese) if anamnese else None
    }

# --- Endpoints de Autenticação & Cadastro de Clínicas (SaaS) ---

@app.post("/api/auth/register-clinic")
def register_clinic(data: RegisterClinicRequest):
    clinic_name = data.clinic_name.strip()
    owner_name = data.owner_name.strip()
    email = data.email.strip().lower()
    password = data.password.strip()

    if not clinic_name or not owner_name or not email or not password:
        raise HTTPException(status_code=400, detail="Por favor, preencha todos os campos obrigatórios.")
    if len(password) < 4:
        raise HTTPException(status_code=400, detail="A senha deve ter no mínimo 4 caracteres.")

    raw_slug = data.slug.strip() if data.slug else clinic_name
    base_slug = slugify(raw_slug)
    slug = base_slug

    conn = get_db()
    cursor = conn.cursor()

    existing_user = cursor.execute("SELECT id FROM users WHERE email = ?", (email,)).fetchone()
    if existing_user:
        conn.close()
        raise HTTPException(status_code=400, detail="Este e-mail já está cadastrado. Por favor faça login.")

    idx = 1
    while cursor.execute("SELECT id FROM clinics WHERE slug = ?", (slug,)).fetchone():
        slug = f"{base_slug}-{idx}"
        idx += 1

    segment = (data.segment or "").strip().lower()
    if not segment:
        name_lower = clinic_name.lower()
        if any(w in name_lower for w in ["salao", "salão", "hair", "cabel", "beleza"]):
            segment = "salao"
        elif any(w in name_lower for w in ["barba", "barbearia"]):
            segment = "barbearia"
        elif any(w in name_lower for w in ["unha", "nail", "sobrancelha"]):
            segment = "unhas"
        else:
            segment = "laser"

    if segment == "salao":
        subtitle = "Salão de Beleza, Cabelos & Estética"
    elif segment == "barbearia":
        subtitle = "Barbearia & Cuidados Masculinos"
    elif segment == "unhas":
        subtitle = "Studio de Unhas & Sobrancelhas"
    else:
        subtitle = "Depilação a Laser e Estética Avançada"

    trial_end = (datetime.datetime.now() + datetime.timedelta(days=15)).strftime("%Y-%m-%d %H:%M:%S")
    cursor.execute("""
        INSERT INTO clinics (name, subtitle, slug, phone, plan, subscription_status, trial_ends_at, segment)
        VALUES (?, ?, ?, ?, 'trial', 'active', ?, ?)
    """, (clinic_name, subtitle, slug, data.phone or "", trial_end, segment))
    clinic_id = cursor.lastrowid

    pwd_hash = hash_password(password)
    cursor.execute("""
        INSERT INTO users (clinic_id, name, email, password_hash, role, is_superadmin)
        VALUES (?, ?, ?, ?, 'owner', 0)
    """, (clinic_id, owner_name, email, pwd_hash))
    user_id = cursor.lastrowid
    conn.commit()
    conn.close()

    seed_default_services_for_clinic(clinic_id, segment=segment)

    token = create_token({
        "user_id": user_id,
        "clinic_id": clinic_id,
        "role": "owner",
        "name": owner_name,
        "email": email
    })

    return {
        "success": True,
        "token": token,
        "user": {"id": user_id, "name": owner_name, "email": email, "role": "owner"},
        "clinic": {"id": clinic_id, "name": clinic_name, "slug": slug, "plan": "trial", "trial_ends_at": trial_end, "segment": segment}
    }

@app.post("/api/auth/login")
def auth_login(data: LoginRequest):
    conn = get_db()
    if data.pin:
        if data.pin == ADMIN_PIN:
            user = conn.execute("SELECT * FROM users WHERE clinic_id = 1 LIMIT 1").fetchone()
            clinic = conn.execute("SELECT * FROM clinics WHERE id = 1").fetchone()
            conn.close()
            user_id = user["id"] if user else 1
            user_name = user["name"] if user else "Administrador"
            token = create_token({"user_id": user_id, "clinic_id": 1, "role": "owner", "name": user_name, "email": "admin@laserglow.com"})
            return {
                "authenticated": True,
                "token": token,
                "user": {"id": user_id, "name": user_name, "email": "admin@laserglow.com", "role": "owner"},
                "clinic": dict(clinic) if clinic else {"id": 1, "name": "LaserGlow Clinic", "slug": "laserglow"}
            }
        else:
            conn.close()
            raise HTTPException(status_code=401, detail="Senha PIN incorreta.")

    email = (data.email or "").strip().lower()
    if email in ("admin", "administrador", "root"):
        email = "admin@laserglow.com"

    password = data.password or ""
    if not email or not password:
        conn.close()
        raise HTTPException(status_code=400, detail="Informe o e-mail/usuário e a senha de acesso.")

    user = conn.execute("SELECT * FROM users WHERE email = ?", (email,)).fetchone()
    if not user:
        conn.close()
        raise HTTPException(status_code=401, detail="Usuário ou senha incorretos.")

    if not verify_password(password, user["password_hash"]):
        conn.close()
        raise HTTPException(status_code=401, detail="E-mail ou senha incorretos.")

    clinic = conn.execute("SELECT * FROM clinics WHERE id = ?", (user["clinic_id"],)).fetchone()
    conn.close()

    token = create_token({
        "user_id": user["id"],
        "clinic_id": user["clinic_id"],
        "role": user["role"],
        "name": user["name"],
        "email": user["email"]
    })

    return {
        "authenticated": True,
        "token": token,
        "user": {"id": user["id"], "name": user["name"], "email": user["email"], "role": user["role"]},
        "clinic": dict(clinic) if clinic else {"id": user["clinic_id"], "name": "Clínica", "slug": "clinica"}
    }

@app.get("/api/admin/me")
def get_current_user_and_clinic(current_user: dict = Depends(get_current_user)):
    conn = get_db()
    clinic = conn.execute("SELECT * FROM clinics WHERE id = ?", (current_user["clinic_id"],)).fetchone()
    
    pix_key_row = conn.execute("SELECT value FROM saas_config WHERE key = 'superadmin_pix_key'").fetchone()
    pix_name_row = conn.execute("SELECT value FROM saas_config WHERE key = 'superadmin_pix_name'").fetchone()
    whatsapp_row = conn.execute("SELECT value FROM saas_config WHERE key = 'superadmin_whatsapp'").fetchone()
    conn.close()

    clinic_dict = dict(clinic) if clinic else current_user.get("clinic")
    
    # Cálculo preciso de dias restantes de teste ou assinatura
    days_left = 15
    now_dt = datetime.datetime.now()
    if clinic_dict.get("subscription_ends_at"):
        try:
            sub_end = datetime.datetime.fromisoformat(clinic_dict["subscription_ends_at"])
            days_left = (sub_end - now_dt).days
        except Exception:
            pass
    elif clinic_dict.get("trial_ends_at"):
        try:
            trial_end = datetime.datetime.fromisoformat(clinic_dict["trial_ends_at"])
            days_left = (trial_end - now_dt).days
        except Exception:
            pass

    is_blocked = int(clinic_dict.get("is_blocked") or 0)
    is_expired = (days_left < 0) and (current_user["clinic_id"] != 1)

    clinic_dict["days_left"] = days_left
    clinic_dict["is_blocked"] = is_blocked
    clinic_dict["is_expired"] = is_expired

    return {
        "user": {
            "id": current_user["id"],
            "name": current_user["name"],
            "email": current_user["email"],
            "role": current_user["role"],
            "is_superadmin": current_user.get("is_superadmin", 0)
        },
        "clinic": clinic_dict,
        "saas_payment_info": {
            "pix_key": pix_key_row["value"] if pix_key_row else "",
            "pix_name": pix_name_row["value"] if pix_name_row else "Administrador",
            "whatsapp": whatsapp_row["value"] if whatsapp_row else ""
        }
    }

@app.post("/api/admin/login")
def admin_login_legacy(data: AdminLoginRequest):
    return auth_login(LoginRequest(pin=data.pin))

# --- Endpoints Públicos Multi-Clínica (/api/public/{slug}/...) ---

@app.get("/api/public/{slug}/settings")
def public_get_clinic_settings(slug: str):
    return get_clinic_by_slug(slug)

@app.get("/api/public/{slug}/services")
def public_list_services(slug: str):
    clinic = get_clinic_by_slug(slug)
    conn = get_db()
    rows = conn.execute("SELECT * FROM services WHERE clinic_id = ? ORDER BY category DESC, price ASC", (clinic["id"],)).fetchall()
    conn.close()
    return [dict(r) for r in rows]

@app.get("/api/public/{slug}/available-slots")
def public_get_available_slots(slug: str, date: str):
    clinic = get_clinic_by_slug(slug)
    return compute_available_slots(clinic, date)

@app.post("/api/public/{slug}/book")
def public_book_appointment(slug: str, data: BookingRequest):
    clinic = get_clinic_by_slug(slug)
    return process_booking(clinic["id"], data)

@app.post("/api/public/{slug}/ficha")
def public_submit_ficha(slug: str, form: PublicClientFichaSubmit):
    clinic = get_clinic_by_slug(slug)
    return process_client_ficha(clinic["id"], form)

@app.get("/api/public/{slug}/ficha")
def public_get_ficha(slug: str, phone: Optional[str] = None, client_id: Optional[int] = None):
    clinic = get_clinic_by_slug(slug)
    return process_client_get_ficha(clinic["id"], phone, client_id)

# --- Endpoints Legados (Clínica 1) ---

@app.get("/api/settings")
def get_clinic_settings():
    conn = get_db()
    row = conn.execute("SELECT * FROM clinics WHERE id = 1").fetchone()
    conn.close()
    if row:
        return dict(row)
    return public_get_clinic_settings("laserglow")

@app.get("/api/services")
def list_services():
    conn = get_db()
    rows = conn.execute("SELECT * FROM services WHERE clinic_id = 1 ORDER BY category DESC, price ASC").fetchall()
    conn.close()
    return [dict(r) for r in rows]

@app.get("/api/available-slots")
def get_available_slots(date: str):
    clinic = get_clinic_by_id(1)
    return compute_available_slots(clinic, date)

@app.post("/api/book")
def book_appointment(data: BookingRequest):
    return process_booking(1, data)

@app.post("/api/client/ficha")
def client_submit_ficha(form: PublicClientFichaSubmit):
    return process_client_ficha(1, form)

@app.get("/api/client/ficha")
def client_get_ficha(phone: Optional[str] = None, client_id: Optional[int] = None):
    return process_client_get_ficha(1, phone, client_id)

# --- Endpoints Administrativos Protegidos (Isolados por Tenant) ---

@app.get("/api/admin/settings")
def get_admin_clinic_settings(admin: dict = Depends(verify_admin)):
    conn = get_db()
    row = conn.execute("SELECT * FROM clinics WHERE id = ?", (admin["clinic_id"],)).fetchone()
    conn.close()
    if not row:
        raise HTTPException(status_code=404, detail="Clínica não encontrada.")
    return dict(row)

@app.put("/api/admin/settings")
def update_clinic_settings(settings: ClinicSettingsUpdate, admin: dict = Depends(verify_admin)):
    conn = get_db()
    conn.execute("""
        UPDATE clinics
        SET name = ?, subtitle = ?, logo_url = ?, phone = ?, instagram = ?,
            opening_time = ?, closing_time = ?, slot_interval_minutes = ?,
            has_lunch_break = ?, lunch_start = ?, lunch_end = ?, working_days = ?
        WHERE id = ?
    """, (
        settings.name, settings.subtitle, settings.logo_url, settings.phone, settings.instagram,
        settings.opening_time or '08:00',
        settings.closing_time or '18:00',
        settings.slot_interval_minutes or 30,
        settings.has_lunch_break or 0,
        settings.lunch_start or '12:00',
        settings.lunch_end or '13:00',
        settings.working_days or '["1","2","3","4","5","6"]',
        admin["clinic_id"]
    ))
    conn.commit()
    conn.close()
    return {"success": True, "message": "Configurações e horários salvos com sucesso!"}

@app.post("/api/admin/upload-logo")
async def upload_logo(file: UploadFile = File(...), admin: dict = Depends(verify_admin)):
    extension = os.path.splitext(file.filename)[1].lower()
    if extension not in [".png", ".jpg", ".jpeg", ".svg", ".webp"]:
        raise HTTPException(status_code=400, detail="Formato de imagem inválido. Use PNG, JPG ou SVG.")

    filename = f"logo_{admin['clinic_id']}_{int(datetime.datetime.now().timestamp())}{extension}"
    file_path = os.path.join(UPLOAD_DIR, filename)

    with open(file_path, "wb") as buffer:
        shutil.copyfileobj(file.file, buffer)

    logo_url = f"/static/uploads/{filename}"

    conn = get_db()
    conn.execute("UPDATE clinics SET logo_url = ? WHERE id = ?", (logo_url, admin["clinic_id"]))
    conn.commit()
    conn.close()

    return {"success": True, "logo_url": logo_url}

@app.get("/api/appointments")
def list_appointments(date: Optional[str] = None, status: Optional[str] = None, admin: dict = Depends(verify_admin)):
    conn = get_db()
    query = """
        SELECT a.id, a.appointment_date, a.appointment_time, a.status, a.notes, a.created_at,
               c.id as client_id, c.name as client_name, c.phone as client_phone, c.email as client_email,
               s.id as service_id, s.name as service_name, s.category as service_category, s.price as service_price, s.duration_minutes
        FROM appointments a
        JOIN clients c ON a.client_id = c.id
        JOIN services s ON a.service_id = s.id
        WHERE a.clinic_id = ?
    """
    params = [admin["clinic_id"]]
    if date:
        query += " AND a.appointment_date = ?"
        params.append(date)
    if status and status != 'todos':
        query += " AND a.status = ?"
        params.append(status)

    query += " ORDER BY a.appointment_date DESC, a.appointment_time ASC"
    rows = conn.execute(query, params).fetchall()
    conn.close()
    return [dict(r) for r in rows]

# --- Gestão de Clientes do Tenant ---
@app.get("/api/clients")
def list_clients(admin: dict = Depends(verify_admin)):
    conn = get_db()
    clinic_id = admin["clinic_id"]
    query = """
        SELECT c.*,
               COUNT(DISTINCT a.id) as total_appointments,
               MAX(a.appointment_date) as last_appointment_date,
               COALESCE((
                   SELECT COUNT(*) FROM client_packages cp WHERE cp.client_id = c.id AND cp.status = 'ativo' AND cp.clinic_id = ?
               ), 0) as active_packages_count,
               COALESCE((
                   SELECT SUM(cf.amount) FROM cash_flow cf WHERE cf.client_id = c.id AND cf.type = 'entrada' AND cf.clinic_id = ?
               ), 0) as total_spent
        FROM clients c
        LEFT JOIN appointments a ON (c.id = a.client_id AND a.clinic_id = ?)
        WHERE c.clinic_id = ?
        GROUP BY c.id
        ORDER BY c.name ASC
    """
    rows = conn.execute(query, (clinic_id, clinic_id, clinic_id, clinic_id)).fetchall()
    conn.close()
    return [dict(r) for r in rows]

@app.post("/api/admin/clients")
def create_client(c: ClientCreateOrUpdate, admin: dict = Depends(verify_admin)):
    conn = get_db()
    cursor = conn.cursor()
    clinic_id = admin["clinic_id"]

    clean_phone = "".join(filter(str.isdigit, c.phone))
    if not clean_phone.startswith("55") and len(clean_phone) in (10, 11):
        clean_phone = "55" + clean_phone

    existing = cursor.execute("SELECT id FROM clients WHERE clinic_id = ? AND phone = ?", (clinic_id, clean_phone)).fetchone()
    if existing:
        conn.close()
        raise HTTPException(status_code=400, detail="Já existe um cliente cadastrado com este telefone/WhatsApp na sua clínica.")

    cursor.execute("""
        INSERT INTO clients (clinic_id, name, phone, email, cpf, birth_date, notes)
        VALUES (?, ?, ?, ?, ?, ?, ?)
    """, (clinic_id, c.name, clean_phone, c.email, c.cpf, c.birth_date, c.notes))
    new_id = cursor.lastrowid
    conn.commit()
    conn.close()
    return {"success": True, "id": new_id, "name": c.name}

@app.put("/api/admin/clients/{client_id}")
def update_client(client_id: int, c: ClientCreateOrUpdate, admin: dict = Depends(verify_admin)):
    conn = get_db()
    clinic_id = admin["clinic_id"]
    clean_phone = "".join(filter(str.isdigit, c.phone))
    if not clean_phone.startswith("55") and len(clean_phone) in (10, 11):
        clean_phone = "55" + clean_phone

    conn.execute("""
        UPDATE clients
        SET name = ?, phone = ?, email = ?, cpf = ?, birth_date = ?, notes = ?
        WHERE id = ? AND clinic_id = ?
    """, (c.name, clean_phone, c.email, c.cpf, c.birth_date, c.notes, client_id, clinic_id))
    conn.commit()
    conn.close()
    return {"success": True, "id": client_id}

@app.delete("/api/admin/clients/{client_id}")
def delete_client(client_id: int, admin: dict = Depends(verify_admin)):
    conn = get_db()
    clinic_id = admin["clinic_id"]
    conn.execute("DELETE FROM appointments WHERE client_id = ? AND clinic_id = ?", (client_id, clinic_id))
    conn.execute("DELETE FROM client_packages WHERE client_id = ? AND clinic_id = ?", (client_id, clinic_id))
    conn.execute("DELETE FROM anamnese_forms WHERE client_id = ? AND clinic_id = ?", (client_id, clinic_id))
    conn.execute("DELETE FROM clients WHERE id = ? AND clinic_id = ?", (client_id, clinic_id))
    conn.commit()
    conn.close()
    return {"success": True, "id": client_id}

# --- Ficha de Anamnese do Tenant ---

@app.get("/api/admin/clients/{client_id}/anamnese")
def get_client_anamnese(client_id: int, admin: dict = Depends(verify_admin)):
    conn = get_db()
    clinic_id = admin["clinic_id"]
    client = conn.execute("SELECT * FROM clients WHERE id = ? AND clinic_id = ?", (client_id, clinic_id)).fetchone()
    if not client:
        conn.close()
        raise HTTPException(status_code=404, detail="Cliente não encontrado.")

    form = conn.execute("SELECT * FROM anamnese_forms WHERE client_id = ? AND clinic_id = ?", (client_id, clinic_id)).fetchone()
    conn.close()

    return {
        "client": dict(client),
        "anamnese": dict(form) if form else None
    }

@app.post("/api/admin/clients/{client_id}/anamnese")
def save_client_anamnese(client_id: int, form: AnamneseFormModel, admin: dict = Depends(verify_admin)):
    conn = get_db()
    cursor = conn.cursor()
    clinic_id = admin["clinic_id"]

    client = cursor.execute("SELECT * FROM clients WHERE id = ? AND clinic_id = ?", (client_id, clinic_id)).fetchone()
    if not client:
        conn.close()
        raise HTTPException(status_code=404, detail="Cliente não encontrado.")

    existing = cursor.execute("SELECT id FROM anamnese_forms WHERE client_id = ? AND clinic_id = ?", (client_id, clinic_id)).fetchone()
    now_str = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")

    if existing:
        cursor.execute("""
            UPDATE anamnese_forms
            SET contract_number = ?, sexo = ?, endereco = ?, bairro = ?, cep = ?, cidade = ?, uf = ?,
                tel_fixo = ?, instagram = ?, profissao = ?, como_conheceu = ?, tipo_tratamento = ?,
                areas_tratamento = ?, exposicao_sol_48h = ?, historico_patologias = ?,
                questionario_medico = ?, reacao_solar = ?, signature_name = ?, signature_date = ?,
                signature_image = COALESCE(NULLIF(?, ''), signature_image),
                updated_at = ?
            WHERE id = ? AND clinic_id = ?
        """, (
            form.contract_number, form.sexo, form.endereco, form.bairro, form.cep, form.cidade, form.uf,
            form.tel_fixo, form.instagram, form.profissao, form.como_conheceu, form.tipo_tratamento,
            form.areas_tratamento, form.exposicao_sol_48h, form.historico_patologias,
            form.questionario_medico, form.reacao_solar, form.signature_name, form.signature_date or now_str,
            form.signature_image or "", now_str, existing["id"], clinic_id
        ))
    else:
        cursor.execute("""
            INSERT INTO anamnese_forms (
                clinic_id, client_id, contract_number, sexo, endereco, bairro, cep, cidade, uf,
                tel_fixo, instagram, profissao, como_conheceu, tipo_tratamento,
                areas_tratamento, exposicao_sol_48h, historico_patologias,
                questionario_medico, reacao_solar, signature_name, signature_date,
                signature_image, created_at, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """, (
            clinic_id, client_id, form.contract_number, form.sexo, form.endereco, form.bairro, form.cep, form.cidade, form.uf,
            form.tel_fixo, form.instagram, form.profissao, form.como_conheceu, form.tipo_tratamento,
            form.areas_tratamento, form.exposicao_sol_48h, form.historico_patologias,
            form.questionario_medico, form.reacao_solar, form.signature_name, form.signature_date or now_str,
            form.signature_image or "", now_str, now_str
        ))

    conn.commit()
    conn.close()
    return {"success": True, "message": "Ficha de Anamnese salva com sucesso!"}

# --- Controle de Pacotes de Clientes do Tenant ---

@app.get("/api/admin/client-packages")
def list_client_packages(client_id: Optional[int] = None, admin: dict = Depends(verify_admin)):
    conn = get_db()
    clinic_id = admin["clinic_id"]
    query = """
        SELECT cp.*, c.name as client_name, c.phone as client_phone, s.name as service_name
        FROM client_packages cp
        JOIN clients c ON cp.client_id = c.id
        JOIN services s ON cp.service_id = s.id
        WHERE cp.clinic_id = ?
    """
    params = [clinic_id]
    if client_id:
        query += " AND cp.client_id = ?"
        params.append(client_id)
    query += " ORDER BY cp.id DESC"
    rows = conn.execute(query, params).fetchall()
    conn.close()
    return [dict(r) for r in rows]

@app.post("/api/admin/client-packages")
def sell_client_package(req: SellPackageRequest, admin: dict = Depends(verify_admin)):
    conn = get_db()
    cursor = conn.cursor()
    clinic_id = admin["clinic_id"]

    service = cursor.execute("SELECT * FROM services WHERE id = ? AND clinic_id = ?", (req.service_id, clinic_id)).fetchone()
    if not service:
        conn.close()
        raise HTTPException(status_code=404, detail="Pacote/Procedimento não encontrado.")

    total_sessions = service["sessions_count"] or 10

    cursor.execute("""
        INSERT INTO client_packages (clinic_id, client_id, service_id, total_sessions, completed_sessions, price_paid, payment_method, status, notes)
        VALUES (?, ?, ?, ?, 0, ?, ?, 'ativo', ?)
    """, (clinic_id, req.client_id, req.service_id, total_sessions, req.price_paid, req.payment_method, req.notes))
    pkg_id = cursor.lastrowid

    client = cursor.execute("SELECT name FROM clients WHERE id = ? AND clinic_id = ?", (req.client_id, clinic_id)).fetchone()
    client_name = client["name"] if client else "Cliente"
    today_str = datetime.date.today().strftime("%Y-%m-%d")

    cursor.execute("""
        INSERT INTO cash_flow (clinic_id, type, category, description, amount, payment_method, transaction_date, client_id)
        VALUES (?, 'entrada', 'Venda de Pacote', ?, ?, ?, ?, ?)
    """, (clinic_id, f"Pacote {service['name']} - {client_name}", req.price_paid, req.payment_method, today_str, req.client_id))

    conn.commit()
    conn.close()
    return {"success": True, "id": pkg_id}

@app.post("/api/admin/client-packages/{pkg_id}/consume-session")
def consume_package_session(pkg_id: int, admin: dict = Depends(verify_admin)):
    conn = get_db()
    cursor = conn.cursor()
    clinic_id = admin["clinic_id"]

    pkg = cursor.execute("SELECT * FROM client_packages WHERE id = ? AND clinic_id = ?", (pkg_id, clinic_id)).fetchone()
    if not pkg:
        conn.close()
        raise HTTPException(status_code=404, detail="Pacote não encontrado.")

    completed = pkg["completed_sessions"] + 1
    total = pkg["total_sessions"]
    new_status = 'concluido' if completed >= total else 'ativo'

    cursor.execute("""
        UPDATE client_packages
        SET completed_sessions = ?, status = ?
        WHERE id = ? AND clinic_id = ?
    """, (completed, new_status, pkg_id, clinic_id))
    conn.commit()
    conn.close()
    return {"success": True, "completed_sessions": completed, "total_sessions": total, "status": new_status}

# --- Fluxo de Caixa do Tenant ---

@app.get("/api/admin/cash-flow")
def list_cash_flow(month: Optional[str] = None, admin: dict = Depends(verify_admin)):
    conn = get_db()
    clinic_id = admin["clinic_id"]
    query = """
        SELECT cf.*, c.name as client_name
        FROM cash_flow cf
        LEFT JOIN clients c ON cf.client_id = c.id
        WHERE cf.clinic_id = ?
    """
    params = [clinic_id]
    if month:
        query += " AND cf.transaction_date LIKE ?"
        params.append(f"{month}%")
    query += " ORDER BY cf.transaction_date DESC, cf.id DESC"
    rows = conn.execute(query, params).fetchall()

    total_entradas = sum(r["amount"] for r in rows if r["type"] == "entrada")
    total_saidas = sum(r["amount"] for r in rows if r["type"] == "saida")
    saldo_liquido = total_entradas - total_saidas

    conn.close()
    return {
        "transactions": [dict(r) for r in rows],
        "summary": {
            "total_entradas": total_entradas,
            "total_saidas": total_saidas,
            "saldo_liquido": saldo_liquido
        }
    }

@app.post("/api/admin/cash-flow")
def add_cash_flow_entry(entry: CashFlowEntry, admin: dict = Depends(verify_admin)):
    conn = get_db()
    cursor = conn.cursor()
    clinic_id = admin["clinic_id"]

    cursor.execute("""
        INSERT INTO cash_flow (clinic_id, type, category, description, amount, payment_method, transaction_date, client_id)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?)
    """, (clinic_id, entry.type, entry.category, entry.description, entry.amount, entry.payment_method, entry.transaction_date, entry.client_id))
    new_id = cursor.lastrowid
    conn.commit()
    conn.close()
    return {"success": True, "id": new_id}

@app.delete("/api/admin/cash-flow/{entry_id}")
def delete_cash_flow_entry(entry_id: int, admin: dict = Depends(verify_admin)):
    conn = get_db()
    clinic_id = admin["clinic_id"]
    conn.execute("DELETE FROM cash_flow WHERE id = ? AND clinic_id = ?", (entry_id, clinic_id))
    conn.commit()
    conn.close()
    return {"success": True, "id": entry_id}

# --- Inteligência de Clínica e Análises do Tenant ---

@app.get("/api/admin/analytics")
def get_clinic_analytics(admin: dict = Depends(verify_admin)):
    conn = get_db()
    clinic_id = admin["clinic_id"]

    total_revenue_row = conn.execute("SELECT SUM(amount) as total, COUNT(*) as count FROM cash_flow WHERE clinic_id = ? AND type = 'entrada'", (clinic_id,)).fetchone()
    total_revenue = total_revenue_row["total"] or 0
    total_sales = total_revenue_row["count"] or 0

    buyers_count = conn.execute("SELECT COUNT(DISTINCT client_id) FROM cash_flow WHERE clinic_id = ? AND type = 'entrada' AND client_id IS NOT NULL", (clinic_id,)).fetchone()[0] or 1
    ticket_medio_cliente = round(total_revenue / buyers_count, 2) if buyers_count > 0 else 0
    ticket_medio_venda = round(total_revenue / total_sales, 2) if total_sales > 0 else 0

    total_apps = conn.execute("SELECT COUNT(*) FROM appointments WHERE clinic_id = ?", (clinic_id,)).fetchone()[0] or 1
    cancelled_apps = conn.execute("SELECT COUNT(*) FROM appointments WHERE clinic_id = ? AND status = 'cancelado'", (clinic_id,)).fetchone()[0] or 0
    no_show_rate = round((cancelled_apps / total_apps) * 100, 1)

    renewal_opportunities = conn.execute("""
        SELECT cp.id, cp.completed_sessions, cp.total_sessions, c.id as client_id, c.name, c.phone, s.name as service_name
        FROM client_packages cp
        JOIN clients c ON cp.client_id = c.id
        JOIN services s ON cp.service_id = s.id
        WHERE cp.clinic_id = ? AND cp.status = 'ativo' AND cp.completed_sessions >= (cp.total_sessions - 3)
    """, (clinic_id,)).fetchall()

    cutoff_date = (datetime.date.today() - datetime.timedelta(days=30)).strftime("%Y-%m-%d")
    at_risk_clients = conn.execute("""
        SELECT c.id, c.name, c.phone, MAX(a.appointment_date) as last_date,
               CAST((julianday('now') - julianday(MAX(a.appointment_date))) AS INTEGER) as days_inactive
        FROM clients c
        JOIN appointments a ON c.id = a.client_id
        WHERE c.clinic_id = ? AND a.clinic_id = ? AND a.status IN ('concluido', 'agendado', 'confirmado')
        GROUP BY c.id
        HAVING MAX(a.appointment_date) < ? AND MAX(a.appointment_date) IS NOT NULL
        ORDER BY last_date ASC
    """, (clinic_id, clinic_id, cutoff_date)).fetchall()

    top_services = conn.execute("""
        SELECT s.name, COUNT(a.id) as total_bookings, SUM(s.price) as generated_revenue
        FROM appointments a
        JOIN services s ON a.service_id = s.id
        WHERE a.clinic_id = ?
        GROUP BY s.id
        ORDER BY total_bookings DESC
        LIMIT 5
    """, (clinic_id,)).fetchall()

    conn.close()

    return {
        "metrics": {
            "total_revenue": total_revenue,
            "ticket_medio_cliente": ticket_medio_cliente,
            "ticket_medio_venda": ticket_medio_venda,
            "no_show_rate": no_show_rate,
            "at_risk_count": len(at_risk_clients),
            "renewal_count": len(renewal_opportunities)
        },
        "renewal_opportunities": [dict(r) for r in renewal_opportunities],
        "at_risk_clients": [dict(r) for r in at_risk_clients],
        "top_services": [dict(r) for r in top_services]
    }

# --- Atualização de Status & Reagendamentos do Tenant ---

@app.put("/api/appointments/{app_id}/status")
def update_status(app_id: int, req: StatusUpdateRequest, admin: dict = Depends(verify_admin)):
    conn = get_db()
    conn.execute("UPDATE appointments SET status = ? WHERE id = ? AND clinic_id = ?", (req.status, app_id, admin["clinic_id"]))
    conn.commit()
    conn.close()
    return {"success": True, "id": app_id, "status": req.status}

@app.put("/api/appointments/{app_id}/reschedule")
def reschedule_appointment(app_id: int, req: RescheduleRequest, admin: dict = Depends(verify_admin)):
    conn = get_db()
    cursor = conn.cursor()
    clinic_id = admin["clinic_id"]

    conflict = cursor.execute(
        "SELECT id FROM appointments WHERE clinic_id = ? AND appointment_date = ? AND appointment_time = ? AND status != 'cancelado' AND id != ?",
        (clinic_id, req.appointment_date, req.appointment_time, app_id)
    ).fetchone()
    if conflict:
        conn.close()
        raise HTTPException(status_code=400, detail="Este horário já está ocupado. Escolha outro.")

    cursor.execute(
        "UPDATE appointments SET appointment_date = ?, appointment_time = ?, status = 'agendado' WHERE id = ? AND clinic_id = ?",
        (req.appointment_date, req.appointment_time, app_id, clinic_id)
    )
    conn.commit()
    conn.close()
    return {"success": True, "id": app_id, "appointment_date": req.appointment_date, "appointment_time": req.appointment_time}

# --- Catálogo de Serviços do Tenant ---

@app.post("/api/admin/services")
def create_service(s: ServiceItem, admin: dict = Depends(verify_admin)):
    conn = get_db()
    cursor = conn.cursor()
    clinic_id = admin["clinic_id"]
    cursor.execute(
        "INSERT INTO services (clinic_id, name, category, sessions_count, duration_minutes, price) VALUES (?, ?, ?, ?, ?, ?)",
        (clinic_id, s.name, s.category, s.sessions_count, s.duration_minutes, s.price)
    )
    new_id = cursor.lastrowid
    conn.commit()
    conn.close()
    return {"success": True, "id": new_id, "name": s.name}

@app.put("/api/admin/services/{service_id}")
def update_service(service_id: int, s: ServiceItem, admin: dict = Depends(verify_admin)):
    conn = get_db()
    cursor = conn.cursor()
    clinic_id = admin["clinic_id"]
    cursor.execute("""
        UPDATE services 
        SET name = ?, category = ?, sessions_count = ?, duration_minutes = ?, price = ?
        WHERE id = ? AND clinic_id = ?
    """, (s.name, s.category, s.sessions_count, s.duration_minutes, s.price, service_id, clinic_id))
    conn.commit()
    conn.close()
    return {"success": True, "id": service_id, "name": s.name}

@app.delete("/api/admin/services/{service_id}")
def delete_service(service_id: int, admin: dict = Depends(verify_admin)):
    conn = get_db()
    clinic_id = admin["clinic_id"]
    conn.execute("DELETE FROM services WHERE id = ? AND clinic_id = ?", (service_id, clinic_id))
    conn.commit()
    conn.close()
    return {"success": True, "id": service_id}

# --- Endpoints de Gestão Master SaaS (SuperAdmin) ---

@app.get("/api/superadmin/config")
def get_saas_config(admin: dict = Depends(verify_superadmin)):
    conn = get_db()
    rows = conn.execute("SELECT key, value FROM saas_config").fetchall()
    conn.close()
    cfg = {r["key"]: r["value"] for r in rows}
    return {
        "pix_key": cfg.get("superadmin_pix_key", ""),
        "pix_name": cfg.get("superadmin_pix_name", "Administrador"),
        "whatsapp": cfg.get("superadmin_whatsapp", ""),
        "default_monthly_price": float(cfg.get("default_monthly_price", 79.0))
    }

@app.post("/api/superadmin/config")
def update_saas_config(data: SaasConfigUpdate, admin: dict = Depends(verify_superadmin)):
    conn = get_db()
    if data.pix_key is not None:
        conn.execute("INSERT OR REPLACE INTO saas_config (key, value) VALUES ('superadmin_pix_key', ?)", (data.pix_key.strip(),))
    if data.pix_name is not None:
        conn.execute("INSERT OR REPLACE INTO saas_config (key, value) VALUES ('superadmin_pix_name', ?)", (data.pix_name.strip(),))
    if data.whatsapp is not None:
        conn.execute("INSERT OR REPLACE INTO saas_config (key, value) VALUES ('superadmin_whatsapp', ?)", (data.whatsapp.strip(),))
    if data.default_monthly_price is not None:
        conn.execute("INSERT OR REPLACE INTO saas_config (key, value) VALUES ('default_monthly_price', ?)", (str(data.default_monthly_price),))
    conn.commit()
    conn.close()
    return {"success": True, "message": "Configurações SaaS salvas com sucesso!"}

@app.get("/api/superadmin/clinics")
def list_saas_clinics(admin: dict = Depends(verify_superadmin)):
    conn = get_db()
    clinics_rows = conn.execute("""
        SELECT c.*, 
               u.name as owner_name, 
               u.email as owner_email,
               (SELECT COUNT(*) FROM appointments a WHERE a.clinic_id = c.id) as appointments_count,
               (SELECT COUNT(*) FROM clients cl WHERE cl.clinic_id = c.id) as clients_count
        FROM clinics c
        LEFT JOIN users u ON u.clinic_id = c.id AND u.role = 'owner'
        GROUP BY c.id
        ORDER BY c.id DESC
    """).fetchall()
    conn.close()

    now_dt = datetime.datetime.now()
    clinics_list = []
    total_active = 0
    total_trial = 0
    total_expired = 0
    total_blocked = 0
    total_mrr = 0.0

    for row in clinics_rows:
        c = dict(row)
        c_id = c["id"]
        plan = c.get("plan") or "trial"
        monthly_price = float(c.get("monthly_price") or 79.0)
        is_blocked = int(c.get("is_blocked") or 0)
        
        days_left = 0
        expiration_date_str = ""
        
        if c.get("subscription_ends_at"):
            try:
                sub_end = datetime.datetime.fromisoformat(c["subscription_ends_at"])
                days_left = (sub_end - now_dt).days
                expiration_date_str = sub_end.strftime("%d/%m/%Y")
            except Exception:
                pass
        elif c.get("trial_ends_at"):
            try:
                tr_end = datetime.datetime.fromisoformat(c["trial_ends_at"])
                days_left = (tr_end - now_dt).days
                expiration_date_str = tr_end.strftime("%d/%m/%Y")
            except Exception:
                pass

        if is_blocked:
            status = "blocked"
            total_blocked += 1
        elif days_left < 0 and c_id != 1:
            status = "expired"
            total_expired += 1
        elif plan == "trial" and c_id != 1:
            status = "trial"
            total_trial += 1
        else:
            status = "active"
            total_active += 1
            if c_id != 1:
                total_mrr += monthly_price

        c["calculated_status"] = status
        c["days_left"] = days_left
        c["expiration_date_formatted"] = expiration_date_str
        c["monthly_price"] = monthly_price
        clinics_list.append(c)

    return {
        "summary": {
            "total_clinics": len(clinics_list),
            "active_clinics": total_active,
            "trial_clinics": total_trial,
            "expired_clinics": total_expired,
            "blocked_clinics": total_blocked,
            "estimated_mrr": round(total_mrr, 2)
        },
        "clinics": clinics_list
    }

@app.post("/api/superadmin/clinics/{clinic_id}/renew")
def renew_clinic_subscription(clinic_id: int, req: ClinicRenewRequest, admin: dict = Depends(verify_superadmin)):
    conn = get_db()
    c = conn.execute("SELECT * FROM clinics WHERE id = ?", (clinic_id,)).fetchone()
    if not c:
        conn.close()
        raise HTTPException(status_code=404, detail="Clínica não encontrada.")

    now_dt = datetime.datetime.now()
    base_dt = now_dt
    if c["subscription_ends_at"]:
        try:
            curr_end = datetime.datetime.fromisoformat(c["subscription_ends_at"])
            if curr_end > now_dt:
                base_dt = curr_end
        except Exception:
            pass

    days = req.days or 30
    new_end_dt = base_dt + datetime.timedelta(days=days)
    new_end_str = new_end_dt.strftime("%Y-%m-%d %H:%M:%S")

    plan = req.plan or c["plan"] or "essencial"
    price = req.monthly_price if req.monthly_price is not None else c["monthly_price"]

    conn.execute("""
        UPDATE clinics
        SET subscription_ends_at = ?,
            plan = ?,
            monthly_price = ?,
            subscription_status = 'active',
            is_blocked = 0
        WHERE id = ?
    """, (new_end_str, plan, price, clinic_id))
    conn.commit()
    conn.close()

    return {
        "success": True,
        "message": f"Assinatura renovada por +{days} dias até {new_end_dt.strftime('%d/%m/%Y')}!",
        "new_end": new_end_str
    }

@app.post("/api/superadmin/clinics/{clinic_id}/extend-trial")
def extend_clinic_trial(clinic_id: int, req: ClinicExtendTrialRequest, admin: dict = Depends(verify_superadmin)):
    conn = get_db()
    c = conn.execute("SELECT * FROM clinics WHERE id = ?", (clinic_id,)).fetchone()
    if not c:
        conn.close()
        raise HTTPException(status_code=404, detail="Clínica não encontrada.")

    now_dt = datetime.datetime.now()
    base_dt = now_dt
    if c["trial_ends_at"]:
        try:
            curr_trial = datetime.datetime.fromisoformat(c["trial_ends_at"])
            if curr_trial > now_dt:
                base_dt = curr_trial
        except Exception:
            pass

    days = req.days or 7
    new_trial_dt = base_dt + datetime.timedelta(days=days)
    new_trial_str = new_trial_dt.strftime("%Y-%m-%d %H:%M:%S")

    conn.execute("""
        UPDATE clinics
        SET trial_ends_at = ?,
            plan = 'trial',
            subscription_status = 'trial',
            is_blocked = 0
        WHERE id = ?
    """, (new_trial_str, clinic_id))
    conn.commit()
    conn.close()

    return {
        "success": True,
        "message": f"Período de teste estendido em +{days} dias até {new_trial_dt.strftime('%d/%m/%Y')}!",
        "new_trial": new_trial_str
    }

@app.post("/api/superadmin/clinics/{clinic_id}/block")
def block_clinic_access(clinic_id: int, admin: dict = Depends(verify_superadmin)):
    if clinic_id == 1:
        raise HTTPException(status_code=400, detail="Não é permitido bloquear a clínica matriz.")
    conn = get_db()
    conn.execute("UPDATE clinics SET is_blocked = 1, subscription_status = 'blocked' WHERE id = ?", (clinic_id,))
    conn.commit()
    conn.close()
    return {"success": True, "message": "Acesso da clínica bloqueado com sucesso!"}

@app.post("/api/superadmin/clinics/{clinic_id}/unblock")
def unblock_clinic_access(clinic_id: int, admin: dict = Depends(verify_superadmin)):
    conn = get_db()
    conn.execute("UPDATE clinics SET is_blocked = 0, subscription_status = 'active' WHERE id = ?", (clinic_id,))
    conn.commit()
    conn.close()
    return {"success": True, "message": "Acesso da clínica desbloqueado com sucesso!"}

# --- Rotas de Páginas HTML (Landing, Painel e Agendamento por Clínica) ---

os.makedirs(STATIC_DIR, exist_ok=True)
os.makedirs(UPLOAD_DIR, exist_ok=True)
app.mount("/static/uploads", StaticFiles(directory=UPLOAD_DIR), name="uploads")
app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")

@app.get("/")
def home_page():
    landing_file = os.path.join(STATIC_DIR, "landing.html")
    if os.path.exists(landing_file):
        return FileResponse(landing_file)
    agendar_file = os.path.join(STATIC_DIR, "agendar.html")
    if os.path.exists(agendar_file):
        return FileResponse(agendar_file)
    return JSONResponse({"status": "Laser Clinic SaaS está online!"})

@app.get("/criar-conta")
@app.get("/cadastro-clinica")
def signup_page():
    signup_file = os.path.join(STATIC_DIR, "signup.html")
    if os.path.exists(signup_file):
        return FileResponse(signup_file)
    admin_file = os.path.join(STATIC_DIR, "admin.html")
    if os.path.exists(admin_file):
        return FileResponse(admin_file)
    raise HTTPException(status_code=404, detail="Página signup.html não encontrada.")

@app.get("/login")
def login_redirect():
    return RedirectResponse(url="/admin")

@app.get("/admin")
def admin_panel_page():
    admin_file = os.path.join(STATIC_DIR, "admin.html")
    if os.path.exists(admin_file):
        return FileResponse(admin_file)
    raise HTTPException(status_code=404, detail="Página admin.html não encontrada na pasta static.")

# Rotas com slug da clínica
@app.get("/c/{slug}/agendar")
def clinic_custom_booking_page(slug: str):
    get_clinic_by_slug(slug)
    agendar_file = os.path.join(STATIC_DIR, "agendar.html")
    if os.path.exists(agendar_file):
        return FileResponse(agendar_file)
    raise HTTPException(status_code=404, detail="Página agendar.html não encontrada.")

@app.get("/c/{slug}/ficha")
@app.get("/c/{slug}/anamnese")
@app.get("/c/{slug}/cadastro")
def clinic_custom_ficha_page(slug: str):
    get_clinic_by_slug(slug)
    ficha_file = os.path.join(STATIC_DIR, "ficha.html")
    if os.path.exists(ficha_file):
        return FileResponse(ficha_file)
    raise HTTPException(status_code=404, detail="Página ficha.html não encontrada.")

# Rotas legadas para clínica padrão
@app.get("/agendar")
def client_booking_page_legacy():
    agendar_file = os.path.join(STATIC_DIR, "agendar.html")
    if os.path.exists(agendar_file):
        return FileResponse(agendar_file)
    raise HTTPException(status_code=404, detail="Página agendar.html não encontrada.")

@app.get("/ficha")
@app.get("/anamnese")
@app.get("/cadastro")
def client_ficha_page_legacy():
    ficha_file = os.path.join(STATIC_DIR, "ficha.html")
    if os.path.exists(ficha_file):
        return FileResponse(ficha_file)
    raise HTTPException(status_code=404, detail="Página ficha.html não encontrada.")

if __name__ == "__main__":
    import uvicorn
    uvicorn.run("main:app", host="0.0.0.0", port=8000, reload=True)
