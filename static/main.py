import sqlite3
import datetime
from typing import List, Optional
from pydantic import BaseModel
from fastapi import FastAPI, HTTPException, Depends, Header, UploadFile, File
from fastapi.staticfiles import StaticFiles
from fastapi.responses import FileResponse, JSONResponse
from fastapi.middleware.cors import CORSMiddleware
import os
import shutil
import json

BASE_DIR = os.path.abspath(os.path.dirname(__file__))
# Suporte automático a Persistent Disk do Render (/var/data) ou pasta local
DATA_DIR = os.environ.get("DATA_DIR", "/var/data" if os.path.exists("/var/data") else BASE_DIR)
os.makedirs(DATA_DIR, exist_ok=True)

DB_PATH = os.path.join(DATA_DIR, "clinic.db")
STATIC_DIR = os.path.join(BASE_DIR, "static")
UPLOAD_DIR = os.path.join(DATA_DIR, "uploads") if os.path.exists("/var/data") else os.path.join(STATIC_DIR, "uploads")
os.makedirs(UPLOAD_DIR, exist_ok=True)

ADMIN_PIN = "1234"  # Senha padrão de acesso ao Painel da Clínica

app = FastAPI(title="Laser Clinic Agendamento & Gestão Completa")

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

    # 1. Tabela de Configurações da Clínica
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

    cursor.execute("SELECT COUNT(*) FROM clinic_settings")
    if cursor.fetchone()[0] == 0:
        cursor.execute("""
        INSERT INTO clinic_settings (id, name, subtitle, logo_url, phone, instagram)
        VALUES (1, 'LaserGlow Clinic', 'Depilação a Laser de Alta Tecnologia', '', '11987654321', '@laser.glow')
        """)

    cursor.execute("PRAGMA table_info(clinic_settings);")
    c_settings_cols = [c[1] for c in cursor.fetchall()]
    if "opening_time" not in c_settings_cols:
        cursor.execute("ALTER TABLE clinic_settings ADD COLUMN opening_time TEXT DEFAULT '08:00';")
    if "closing_time" not in c_settings_cols:
        cursor.execute("ALTER TABLE clinic_settings ADD COLUMN closing_time TEXT DEFAULT '18:00';")
    if "slot_interval_minutes" not in c_settings_cols:
        cursor.execute("ALTER TABLE clinic_settings ADD COLUMN slot_interval_minutes INTEGER DEFAULT 30;")
    if "has_lunch_break" not in c_settings_cols:
        cursor.execute("ALTER TABLE clinic_settings ADD COLUMN has_lunch_break INTEGER DEFAULT 0;")
    if "lunch_start" not in c_settings_cols:
        cursor.execute("ALTER TABLE clinic_settings ADD COLUMN lunch_start TEXT DEFAULT '12:00';")
    if "lunch_end" not in c_settings_cols:
        cursor.execute("ALTER TABLE clinic_settings ADD COLUMN lunch_end TEXT DEFAULT '13:00';")
    if "working_days" not in c_settings_cols:
        cursor.execute("ALTER TABLE clinic_settings ADD COLUMN working_days TEXT DEFAULT '[\"1\",\"2\",\"3\",\"4\",\"5\",\"6\"]';")

    # 2. Tabela de Serviços e Pacotes da Clínica
    cursor.execute("""
    CREATE TABLE IF NOT EXISTS services (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
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

    # 3. Tabela de Clientes com campos completos
    cursor.execute("""
    CREATE TABLE IF NOT EXISTS clients (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        name TEXT NOT NULL,
        phone TEXT NOT NULL UNIQUE,
        email TEXT,
        cpf TEXT,
        birth_date TEXT,
        notes TEXT,
        created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
    );
    """)
    cursor.execute("PRAGMA table_info(clients);")
    client_cols = [c[1] for c in cursor.fetchall()]
    if "cpf" not in client_cols:
        cursor.execute("ALTER TABLE clients ADD COLUMN cpf TEXT;")
    if "birth_date" not in client_cols:
        cursor.execute("ALTER TABLE clients ADD COLUMN birth_date TEXT;")
    if "notes" not in client_cols:
        cursor.execute("ALTER TABLE clients ADD COLUMN notes TEXT;")

    # 4. Tabela de Agendamentos
    cursor.execute("""
    CREATE TABLE IF NOT EXISTS appointments (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
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

    # 5. Tabela de Controle de Pacotes Vendidos aos Clientes (Sessões Restantes)
    cursor.execute("""
    CREATE TABLE IF NOT EXISTS client_packages (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
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

    # 6. Tabela de Fluxo de Caixa (Entradas e Saídas)
    cursor.execute("""
    CREATE TABLE IF NOT EXISTS cash_flow (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
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

    # 7. Tabela de Ficha de Avaliação e Anamnese do Cliente (Luz e Pele)
    cursor.execute("""
    CREATE TABLE IF NOT EXISTS anamnese_forms (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        client_id INTEGER NOT NULL UNIQUE,
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

    try:
        cursor.execute("ALTER TABLE anamnese_forms ADD COLUMN signature_image TEXT")
    except Exception:
        pass

    # Procedimentos padrão se vazio
    cursor.execute("SELECT COUNT(*) FROM services")
    count = cursor.fetchone()[0]
    if count == 0:
        default_services = [
            ("Axilas (Avulsa)", "Sessão Avulsa", 1, 30, 89.90),
            ("Virilha Completa (Avulsa)", "Sessão Avulsa", 1, 30, 149.90),
            ("Pernas Inteiras (Avulsa)", "Sessão Avulsa", 1, 45, 199.90),
            ("Barba Completa (Avulsa)", "Sessão Avulsa", 1, 30, 119.90),
            ("Faixa de Barba (Avulsa)", "Sessão Avulsa", 1, 20, 69.90),
            ("Buço / Queixo (Avulsa)", "Sessão Avulsa", 1, 15, 49.90),
            ("Pacote Axilas (10 Sessões)", "Pacote Promocional", 10, 30, 499.00),
            ("Pacote Virilha Completa (10 Sessões)", "Pacote Promocional", 10, 30, 899.00),
            ("Pacote Pernas Inteiras (10 Sessões)", "Pacote Promocional", 10, 45, 1290.00),
            ("Combo VIP: Axilas + Virilha (10 Sessões)", "Pacote Promocional", 10, 45, 1190.00)
        ]
        cursor.executemany("INSERT INTO services (name, category, sessions_count, duration_minutes, price) VALUES (?, ?, ?, ?, ?)", default_services)

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
    pin: str

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

def verify_admin(x_admin_pin: Optional[str] = Header(None)):
    if x_admin_pin != ADMIN_PIN:
        raise HTTPException(status_code=401, detail="Acesso não autorizado. Digite o PIN da clínica.")
    return True

# --- Endpoints Públicos (Clientes e Agendamento) ---

@app.get("/api/settings")
def get_clinic_settings():
    conn = get_db()
    row = conn.execute("SELECT * FROM clinic_settings WHERE id = 1").fetchone()
    conn.close()
    if row:
        d = dict(row)
        if not d.get("opening_time"): d["opening_time"] = "08:00"
        if not d.get("closing_time"): d["closing_time"] = "18:00"
        if not d.get("slot_interval_minutes"): d["slot_interval_minutes"] = 30
        if d.get("has_lunch_break") is None: d["has_lunch_break"] = 0
        if not d.get("lunch_start"): d["lunch_start"] = "12:00"
        if not d.get("lunch_end"): d["lunch_end"] = "13:00"
        if not d.get("working_days"): d["working_days"] = '["1","2","3","4","5","6"]'
        return d
    return {
        "name": "LaserGlow Clinic",
        "subtitle": "Depilação a Laser de Alta Tecnologia",
        "logo_url": "",
        "phone": "",
        "instagram": "",
        "opening_time": "08:00",
        "closing_time": "18:00",
        "slot_interval_minutes": 30,
        "has_lunch_break": 0,
        "lunch_start": "12:00",
        "lunch_end": "13:00",
        "working_days": '["1","2","3","4","5","6"]'
    }

@app.get("/api/services")
def list_services():
    conn = get_db()
    rows = conn.execute("SELECT * FROM services ORDER BY category DESC, price ASC").fetchall()
    conn.close()
    return [dict(r) for r in rows]

@app.get("/api/available-slots")
def get_available_slots(date: str):
    conn = get_db()
    settings = conn.execute("SELECT * FROM clinic_settings WHERE id = 1").fetchone()
    booked = conn.execute(
        "SELECT appointment_time FROM appointments WHERE appointment_date = ? AND status != 'cancelado'",
        (date,)
    ).fetchall()
    conn.close()

    booked_times = set(r["appointment_time"] for r in booked)

    # Configurações de horários
    opening_time = (settings["opening_time"] if settings and "opening_time" in settings.keys() and settings["opening_time"] else "08:00")
    closing_time = (settings["closing_time"] if settings and "closing_time" in settings.keys() and settings["closing_time"] else "18:00")
    interval = (int(settings["slot_interval_minutes"]) if settings and "slot_interval_minutes" in settings.keys() and settings["slot_interval_minutes"] else 30)
    has_lunch = bool(settings["has_lunch_break"]) if settings and "has_lunch_break" in settings.keys() else False
    lunch_s = (settings["lunch_start"] if settings and "lunch_start" in settings.keys() and settings["lunch_start"] else "12:00")
    lunch_e = (settings["lunch_end"] if settings and "lunch_end" in settings.keys() and settings["lunch_end"] else "13:00")
    working_days_raw = (settings["working_days"] if settings and "working_days" in settings.keys() and settings["working_days"] else '["1","2","3","4","5","6"]')

    try:
        working_days = [str(d) for d in json.loads(working_days_raw)]
    except Exception:
        working_days = ["1", "2", "3", "4", "5", "6"]

    # Checar se dia da semana está ativo (0=Domingo, 1=Segunda, ..., 6=Sábado)
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

        # Pular slots que conflitem com intervalo de almoço
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

@app.post("/api/book")
def book_appointment(data: BookingRequest):
    conn = get_db()
    cursor = conn.cursor()

    # Verificar dia de funcionamento
    settings = cursor.execute("SELECT * FROM clinic_settings WHERE id = 1").fetchone()
    if settings and "working_days" in settings.keys() and settings["working_days"]:
        try:
            working_days = [str(d) for d in json.loads(settings["working_days"])]
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
        "SELECT id FROM appointments WHERE appointment_date = ? AND appointment_time = ? AND status != 'cancelado'",
        (data.appointment_date, data.appointment_time)
    ).fetchone()
    if conflict:
        conn.close()
        raise HTTPException(status_code=400, detail="Este horário acabou de ser reservado. Por favor, escolha outro.")

    client = cursor.execute("SELECT id FROM clients WHERE phone = ?", (clean_phone,)).fetchone()
    if client:
        client_id = client["id"]
        cursor.execute("UPDATE clients SET name = ?, email = COALESCE(?, email) WHERE id = ?", (data.name, data.email, client_id))
    else:
        cursor.execute("INSERT INTO clients (name, phone, email) VALUES (?, ?, ?)", (data.name, clean_phone, data.email))
        client_id = cursor.lastrowid

    cursor.execute("""
        INSERT INTO appointments (client_id, service_id, appointment_date, appointment_time, status, notes)
        VALUES (?, ?, ?, ?, 'agendado', ?)
    """, (client_id, data.service_id, data.appointment_date, data.appointment_time, data.notes or ""))

    app_id = cursor.lastrowid
    conn.commit()

    details = cursor.execute("""
        SELECT a.id, a.appointment_date, a.appointment_time, a.status, s.name as service_name, s.price, c.name as client_name, c.phone
        FROM appointments a
        JOIN services s ON a.service_id = s.id
        JOIN clients c ON a.client_id = c.id
        WHERE a.id = ?
    """, (app_id,)).fetchone()
    conn.close()

    return dict(details)

# --- Endpoints Administrativos (Protegidos por PIN) ---

@app.post("/api/admin/login")
def admin_login(data: AdminLoginRequest):
    if data.pin == ADMIN_PIN:
        return {"authenticated": True, "token": ADMIN_PIN}
    raise HTTPException(status_code=401, detail="Senha PIN incorreta.")

@app.put("/api/admin/settings")
def update_clinic_settings(settings: ClinicSettingsUpdate, _=Depends(verify_admin)):
    conn = get_db()
    conn.execute("""
        UPDATE clinic_settings
        SET name = ?, subtitle = ?, logo_url = ?, phone = ?, instagram = ?,
            opening_time = ?, closing_time = ?, slot_interval_minutes = ?,
            has_lunch_break = ?, lunch_start = ?, lunch_end = ?, working_days = ?
        WHERE id = 1
    """, (
        settings.name, settings.subtitle, settings.logo_url, settings.phone, settings.instagram,
        settings.opening_time or '08:00',
        settings.closing_time or '18:00',
        settings.slot_interval_minutes or 30,
        settings.has_lunch_break or 0,
        settings.lunch_start or '12:00',
        settings.lunch_end or '13:00',
        settings.working_days or '["1","2","3","4","5","6"]'
    ))
    conn.commit()
    conn.close()
    return {"success": True, "message": "Configurações e horários salvos com sucesso!"}

@app.post("/api/admin/upload-logo")
async def upload_logo(file: UploadFile = File(...), _=Depends(verify_admin)):
    extension = os.path.splitext(file.filename)[1].lower()
    if extension not in [".png", ".jpg", ".jpeg", ".svg", ".webp"]:
        raise HTTPException(status_code=400, detail="Formato de imagem inválido. Use PNG, JPG ou SVG.")

    filename = f"logo_{int(datetime.datetime.now().timestamp())}{extension}"
    file_path = os.path.join(UPLOAD_DIR, filename)

    with open(file_path, "wb") as buffer:
        shutil.copyfileobj(file.file, buffer)

    logo_url = f"/static/uploads/{filename}"

    conn = get_db()
    conn.execute("UPDATE clinic_settings SET logo_url = ? WHERE id = 1", (logo_url,))
    conn.commit()
    conn.close()

    return {"success": True, "logo_url": logo_url}

@app.get("/api/appointments")
def list_appointments(date: Optional[str] = None, status: Optional[str] = None, _=Depends(verify_admin)):
    conn = get_db()
    query = """
        SELECT a.id, a.appointment_date, a.appointment_time, a.status, a.notes, a.created_at,
               c.id as client_id, c.name as client_name, c.phone as client_phone, c.email as client_email,
               s.id as service_id, s.name as service_name, s.category as service_category, s.price as service_price, s.duration_minutes
        FROM appointments a
        JOIN clients c ON a.client_id = c.id
        JOIN services s ON a.service_id = s.id
        WHERE 1=1
    """
    params = []
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

# --- Gestão Completa de Clientes ---
@app.get("/api/clients")
def list_clients(_=Depends(verify_admin)):
    conn = get_db()
    query = """
        SELECT c.*,
               COUNT(DISTINCT a.id) as total_appointments,
               MAX(a.appointment_date) as last_appointment_date,
               COALESCE((
                   SELECT COUNT(*) FROM client_packages cp WHERE cp.client_id = c.id AND cp.status = 'ativo'
               ), 0) as active_packages_count,
               COALESCE((
                   SELECT SUM(cf.amount) FROM cash_flow cf WHERE cf.client_id = c.id AND cf.type = 'entrada'
               ), 0) as total_spent
        FROM clients c
        LEFT JOIN appointments a ON c.id = a.client_id
        GROUP BY c.id
        ORDER BY c.name ASC
    """
    rows = conn.execute(query).fetchall()
    conn.close()
    return [dict(r) for r in rows]

@app.post("/api/admin/clients")
def create_client(c: ClientCreateOrUpdate, _=Depends(verify_admin)):
    conn = get_db()
    cursor = conn.cursor()
    clean_phone = "".join(filter(str.isdigit, c.phone))
    if not clean_phone.startswith("55") and len(clean_phone) in (10, 11):
        clean_phone = "55" + clean_phone

    existing = cursor.execute("SELECT id FROM clients WHERE phone = ?", (clean_phone,)).fetchone()
    if existing:
        conn.close()
        raise HTTPException(status_code=400, detail="Já existe um cliente cadastrado com este telefone/WhatsApp.")

    cursor.execute("""
        INSERT INTO clients (name, phone, email, cpf, birth_date, notes)
        VALUES (?, ?, ?, ?, ?, ?)
    """, (c.name, clean_phone, c.email, c.cpf, c.birth_date, c.notes))
    new_id = cursor.lastrowid
    conn.commit()
    conn.close()
    return {"success": True, "id": new_id, "name": c.name}

@app.put("/api/admin/clients/{client_id}")
def update_client(client_id: int, c: ClientCreateOrUpdate, _=Depends(verify_admin)):
    conn = get_db()
    clean_phone = "".join(filter(str.isdigit, c.phone))
    if not clean_phone.startswith("55") and len(clean_phone) in (10, 11):
        clean_phone = "55" + clean_phone

    conn.execute("""
        UPDATE clients
        SET name = ?, phone = ?, email = ?, cpf = ?, birth_date = ?, notes = ?
        WHERE id = ?
    """, (c.name, clean_phone, c.email, c.cpf, c.birth_date, c.notes, client_id))
    conn.commit()
    conn.close()
    return {"success": True, "id": client_id}

@app.delete("/api/admin/clients/{client_id}")
def delete_client(client_id: int, _=Depends(verify_admin)):
    conn = get_db()
    conn.execute("DELETE FROM appointments WHERE client_id = ?", (client_id,))
    conn.execute("DELETE FROM client_packages WHERE client_id = ?", (client_id,))
    conn.execute("DELETE FROM anamnese_forms WHERE client_id = ?", (client_id,))
    conn.execute("DELETE FROM clients WHERE id = ?", (client_id,))
    conn.commit()
    conn.close()
    return {"success": True, "id": client_id}

# --- Ficha de Anamnese / Avaliação de Depilação a Laser (Luz e Pele) ---

@app.get("/api/admin/clients/{client_id}/anamnese")
def get_client_anamnese(client_id: int, _=Depends(verify_admin)):
    conn = get_db()
    client = conn.execute("SELECT * FROM clients WHERE id = ?", (client_id,)).fetchone()
    if not client:
        conn.close()
        raise HTTPException(status_code=404, detail="Cliente não encontrado.")

    form = conn.execute("SELECT * FROM anamnese_forms WHERE client_id = ?", (client_id,)).fetchone()
    conn.close()

    return {
        "client": dict(client),
        "anamnese": dict(form) if form else None
    }

@app.post("/api/admin/clients/{client_id}/anamnese")
def save_client_anamnese(client_id: int, form: AnamneseFormModel, _=Depends(verify_admin)):
    conn = get_db()
    cursor = conn.cursor()

    client = cursor.execute("SELECT * FROM clients WHERE id = ?", (client_id,)).fetchone()
    if not client:
        conn.close()
        raise HTTPException(status_code=404, detail="Cliente não encontrado.")

    # Atualiza também dados cadastrais básicos no cliente se vierem da ficha
    if form.instagram:
        cursor.execute("UPDATE clients SET notes = COALESCE(notes, '') WHERE id = ?", (client_id,))

    existing = cursor.execute("SELECT id FROM anamnese_forms WHERE client_id = ?", (client_id,)).fetchone()
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
            WHERE client_id = ?
        """, (
            form.contract_number, form.sexo, form.endereco, form.bairro, form.cep, form.cidade, form.uf,
            form.tel_fixo, form.instagram, form.profissao, form.como_conheceu, form.tipo_tratamento,
            form.areas_tratamento, form.exposicao_sol_48h, form.historico_patologias,
            form.questionario_medico, form.reacao_solar, form.signature_name, form.signature_date or now_str,
            form.signature_image or "", now_str, client_id
        ))
    else:
        cursor.execute("""
            INSERT INTO anamnese_forms (
                client_id, contract_number, sexo, endereco, bairro, cep, cidade, uf,
                tel_fixo, instagram, profissao, como_conheceu, tipo_tratamento,
                areas_tratamento, exposicao_sol_48h, historico_patologias,
                questionario_medico, reacao_solar, signature_name, signature_date,
                signature_image, created_at, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """, (
            client_id, form.contract_number, form.sexo, form.endereco, form.bairro, form.cep, form.cidade, form.uf,
            form.tel_fixo, form.instagram, form.profissao, form.como_conheceu, form.tipo_tratamento,
            form.areas_tratamento, form.exposicao_sol_48h, form.historico_patologias,
            form.questionario_medico, form.reacao_solar, form.signature_name, form.signature_date or now_str,
            form.signature_image or "", now_str, now_str
        ))

    conn.commit()
    conn.close()
    return {"success": True, "message": "Ficha de Anamnese salva com sucesso!"}

# --- Endpoint Público de Preenchimento e Assinatura de Ficha pelo Cliente ---

@app.post("/api/client/ficha")
def client_submit_ficha(form: PublicClientFichaSubmit):
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

    # Procura se o cliente já existe por WhatsApp ou cadastra novo
    client = cursor.execute("SELECT id FROM clients WHERE phone = ?", (clean_phone,)).fetchone()
    if client:
        client_id = client["id"]
        cursor.execute("""
            UPDATE clients 
            SET name = ?, 
                email = CASE WHEN ? != '' THEN ? ELSE email END,
                cpf = CASE WHEN ? != '' THEN ? ELSE cpf END,
                birth_date = CASE WHEN ? != '' THEN ? ELSE birth_date END
            WHERE id = ?
        """, (name, form.email or "", form.email or "", form.cpf or "", form.cpf or "", form.birth_date or "", form.birth_date or "", client_id))
    else:
        cursor.execute("""
            INSERT INTO clients (name, phone, email, cpf, birth_date)
            VALUES (?, ?, ?, ?, ?)
        """, (name, clean_phone, form.email or "", form.cpf or "", form.birth_date or ""))
        client_id = cursor.lastrowid

    now_str = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    existing_anamnese = cursor.execute("SELECT id FROM anamnese_forms WHERE client_id = ?", (client_id,)).fetchone()

    if existing_anamnese:
        cursor.execute("""
            UPDATE anamnese_forms
            SET contract_number = ?, sexo = ?, endereco = ?, bairro = ?, cep = ?, cidade = ?, uf = ?,
                tel_fixo = ?, instagram = ?, profissao = ?, como_conheceu = ?, tipo_tratamento = ?,
                areas_tratamento = ?, exposicao_sol_48h = ?, historico_patologias = ?,
                questionario_medico = ?, reacao_solar = ?, signature_name = ?, signature_date = ?,
                signature_image = CASE WHEN ? != '' THEN ? ELSE signature_image END,
                updated_at = ?
            WHERE client_id = ?
        """, (
            form.contract_number, form.sexo, form.endereco, form.bairro, form.cep, form.cidade, form.uf,
            form.tel_fixo, form.instagram, form.profissao, form.como_conheceu, form.tipo_tratamento,
            form.areas_tratamento, form.exposicao_sol_48h, form.historico_patologias,
            form.questionario_medico, form.reacao_solar, form.signature_name or name,
            form.signature_date or now_str, form.signature_image or "", form.signature_image or "", now_str, client_id
        ))
    else:
        cursor.execute("""
            INSERT INTO anamnese_forms (
                client_id, contract_number, sexo, endereco, bairro, cep, cidade, uf,
                tel_fixo, instagram, profissao, como_conheceu, tipo_tratamento,
                areas_tratamento, exposicao_sol_48h, historico_patologias,
                questionario_medico, reacao_solar, signature_name, signature_date,
                signature_image, created_at, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """, (
            client_id, form.contract_number, form.sexo, form.endereco, form.bairro, form.cep, form.cidade, form.uf,
            form.tel_fixo, form.instagram, form.profissao, form.como_conheceu, form.tipo_tratamento,
            form.areas_tratamento, form.exposicao_sol_48h, form.historico_patologias,
            form.questionario_medico, form.reacao_solar, form.signature_name or name,
            form.signature_date or now_str, form.signature_image or "", now_str, now_str
        ))

    conn.commit()
    conn.close()
    return {"success": True, "client_id": client_id, "name": name, "message": "Ficha e assinatura digital registradas com sucesso!"}

@app.get("/api/client/ficha")
def client_get_ficha(phone: Optional[str] = None, client_id: Optional[int] = None):
    if not phone and not client_id:
        return {"client": None, "anamnese": None}
    conn = get_db()
    client = None
    if client_id:
        client = conn.execute("SELECT * FROM clients WHERE id = ?", (client_id,)).fetchone()
    elif phone:
        clean_phone = "".join(filter(str.isdigit, phone))
        if not clean_phone.startswith("55") and len(clean_phone) in (10, 11):
            clean_phone = "55" + clean_phone
        client = conn.execute("SELECT * FROM clients WHERE phone = ? OR phone LIKE ?", (clean_phone, f"%{clean_phone}%")).fetchone()

    if not client:
        conn.close()
        return {"client": None, "anamnese": None}

    c_id = client["id"]
    anamnese = conn.execute("SELECT * FROM anamnese_forms WHERE client_id = ?", (c_id,)).fetchone()
    conn.close()
    return {
        "client": dict(client),
        "anamnese": dict(anamnese) if anamnese else None
    }

# --- Controle de Pacotes de Clientes (Sessões Realizadas e Restantes) ---

@app.get("/api/admin/client-packages")
def list_client_packages(client_id: Optional[int] = None, _=Depends(verify_admin)):
    conn = get_db()
    query = """
        SELECT cp.*, c.name as client_name, c.phone as client_phone, s.name as service_name
        FROM client_packages cp
        JOIN clients c ON cp.client_id = c.id
        JOIN services s ON cp.service_id = s.id
        WHERE 1=1
    """
    params = []
    if client_id:
        query += " AND cp.client_id = ?"
        params.append(client_id)
    query += " ORDER BY cp.id DESC"
    rows = conn.execute(query, params).fetchall()
    conn.close()
    return [dict(r) for r in rows]

@app.post("/api/admin/client-packages")
def sell_client_package(req: SellPackageRequest, _=Depends(verify_admin)):
    conn = get_db()
    cursor = conn.cursor()

    service = cursor.execute("SELECT * FROM services WHERE id = ?", (req.service_id,)).fetchone()
    if not service:
        conn.close()
        raise HTTPException(status_code=404, detail="Pacote/Procedimento não encontrado.")

    total_sessions = service["sessions_count"] or 10

    # Cria o pacote para o cliente
    cursor.execute("""
        INSERT INTO client_packages (client_id, service_id, total_sessions, completed_sessions, price_paid, payment_method, status, notes)
        VALUES (?, ?, ?, 0, ?, ?, 'ativo', ?)
    """, (req.client_id, req.service_id, total_sessions, req.price_paid, req.payment_method, req.notes))
    pkg_id = cursor.lastrowid

    # Lança automaticamente no Fluxo de Caixa como entrada
    client = cursor.execute("SELECT name FROM clients WHERE id = ?", (req.client_id,)).fetchone()
    client_name = client["name"] if client else "Cliente"
    today_str = datetime.date.today().strftime("%Y-%m-%d")

    cursor.execute("""
        INSERT INTO cash_flow (type, category, description, amount, payment_method, transaction_date, client_id)
        VALUES ('entrada', 'Venda de Pacote', ?, ?, ?, ?, ?)
    """, (f"Pacote {service['name']} - {client_name}", req.price_paid, req.payment_method, today_str, req.client_id))

    conn.commit()
    conn.close()
    return {"success": True, "id": pkg_id}

@app.post("/api/admin/client-packages/{pkg_id}/consume-session")
def consume_package_session(pkg_id: int, _=Depends(verify_admin)):
    conn = get_db()
    cursor = conn.cursor()
    pkg = cursor.execute("SELECT * FROM client_packages WHERE id = ?", (pkg_id,)).fetchone()
    if not pkg:
        conn.close()
        raise HTTPException(status_code=404, detail="Pacote não encontrado.")

    completed = pkg["completed_sessions"] + 1
    total = pkg["total_sessions"]
    new_status = 'concluido' if completed >= total else 'ativo'

    cursor.execute("""
        UPDATE client_packages
        SET completed_sessions = ?, status = ?
        WHERE id = ?
    """, (completed, new_status, pkg_id))
    conn.commit()
    conn.close()
    return {"success": True, "completed_sessions": completed, "total_sessions": total, "status": new_status}

# --- Fluxo de Caixa (Financeiro) ---

@app.get("/api/admin/cash-flow")
def list_cash_flow(month: Optional[str] = None, _=Depends(verify_admin)):
    conn = get_db()
    query = """
        SELECT cf.*, c.name as client_name
        FROM cash_flow cf
        LEFT JOIN clients c ON cf.client_id = c.id
        WHERE 1=1
    """
    params = []
    if month:
        query += " AND cf.transaction_date LIKE ?"
        params.append(f"{month}%")
    query += " ORDER BY cf.transaction_date DESC, cf.id DESC"
    rows = conn.execute(query, params).fetchall()

    # Totais
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
def add_cash_flow_entry(entry: CashFlowEntry, _=Depends(verify_admin)):
    conn = get_db()
    cursor = conn.cursor()
    cursor.execute("""
        INSERT INTO cash_flow (type, category, description, amount, payment_method, transaction_date, client_id)
        VALUES (?, ?, ?, ?, ?, ?, ?)
    """, (entry.type, entry.category, entry.description, entry.amount, entry.payment_method, entry.transaction_date, entry.client_id))
    new_id = cursor.lastrowid
    conn.commit()
    conn.close()
    return {"success": True, "id": new_id}

@app.delete("/api/admin/cash-flow/{entry_id}")
def delete_cash_flow_entry(entry_id: int, _=Depends(verify_admin)):
    conn = get_db()
    conn.execute("DELETE FROM cash_flow WHERE id = ?", (entry_id,))
    conn.commit()
    conn.close()
    return {"success": True, "id": entry_id}

# --- Inteligência de Clínica: Análise de Clientes, Ticket Médio & Retenção (Evitar Perdas) ---

@app.get("/api/admin/analytics")
def get_clinic_analytics(_=Depends(verify_admin)):
    conn = get_db()

    # 1. Total faturado e ticket médio
    total_revenue_row = conn.execute("SELECT SUM(amount) as total, COUNT(*) as count FROM cash_flow WHERE type = 'entrada'").fetchone()
    total_revenue = total_revenue_row["total"] or 0
    total_sales = total_revenue_row["count"] or 0

    # Contagem de clientes únicos compradores
    buyers_count = conn.execute("SELECT COUNT(DISTINCT client_id) FROM cash_flow WHERE type = 'entrada' AND client_id IS NOT NULL").fetchone()[0] or 1
    ticket_medio_cliente = round(total_revenue / buyers_count, 2) if buyers_count > 0 else 0
    ticket_medio_venda = round(total_revenue / total_sales, 2) if total_sales > 0 else 0

    # 2. Taxa de No-Show / Cancelamento
    total_apps = conn.execute("SELECT COUNT(*) FROM appointments").fetchone()[0] or 1
    cancelled_apps = conn.execute("SELECT COUNT(*) FROM appointments WHERE status = 'cancelado'").fetchone()[0] or 0
    no_show_rate = round((cancelled_apps / total_apps) * 100, 1)

    # 3. Pacotes com sessões quase acabando (Oportunidade de Renovação / Upsell)
    # Ex: Pacotes com 7 ou mais sessões concluídas de 10
    renewal_opportunities = conn.execute("""
        SELECT cp.id, cp.completed_sessions, cp.total_sessions, c.id as client_id, c.name, c.phone, s.name as service_name
        FROM client_packages cp
        JOIN clients c ON cp.client_id = c.id
        JOIN services s ON cp.service_id = s.id
        WHERE cp.status = 'ativo' AND cp.completed_sessions >= (cp.total_sessions - 3)
    """).fetchall()

    # 4. Clientes em Risco de Perda (Inativos há mais de 30 dias para laser)
    # Depilação a laser requer sessões a cada 30 a 45 dias. Passou disso, há risco de abandono do tratamento!
    cutoff_date = (datetime.date.today() - datetime.timedelta(days=30)).strftime("%Y-%m-%d")
    at_risk_clients = conn.execute("""
        SELECT c.id, c.name, c.phone, MAX(a.appointment_date) as last_date,
               CAST((julianday('now') - julianday(MAX(a.appointment_date))) AS INTEGER) as days_inactive
        FROM clients c
        JOIN appointments a ON c.id = a.client_id
        WHERE a.status IN ('concluido', 'agendado', 'confirmado')
        GROUP BY c.id
        HAVING MAX(a.appointment_date) < ? AND MAX(a.appointment_date) IS NOT NULL
        ORDER BY last_date ASC
    """, (cutoff_date,)).fetchall()

    # 5. Top 5 Procedimentos mais procurados
    top_services = conn.execute("""
        SELECT s.name, COUNT(a.id) as total_bookings, SUM(s.price) as generated_revenue
        FROM appointments a
        JOIN services s ON a.service_id = s.id
        GROUP BY s.id
        ORDER BY total_bookings DESC
        LIMIT 5
    """).fetchall()

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

# --- Atualização de Status & Reagendamentos ---

@app.put("/api/appointments/{app_id}/status")
def update_status(app_id: int, req: StatusUpdateRequest, _=Depends(verify_admin)):
    conn = get_db()
    conn.execute("UPDATE appointments SET status = ? WHERE id = ?", (req.status, app_id))
    conn.commit()
    conn.close()
    return {"success": True, "id": app_id, "status": req.status}

@app.put("/api/appointments/{app_id}/reschedule")
def reschedule_appointment(app_id: int, req: RescheduleRequest, _=Depends(verify_admin)):
    conn = get_db()
    cursor = conn.cursor()
    conflict = cursor.execute(
        "SELECT id FROM appointments WHERE appointment_date = ? AND appointment_time = ? AND status != 'cancelado' AND id != ?",
        (req.appointment_date, req.appointment_time, app_id)
    ).fetchone()
    if conflict:
        conn.close()
        raise HTTPException(status_code=400, detail="Este horário já está ocupado. Escolha outro.")

    cursor.execute(
        "UPDATE appointments SET appointment_date = ?, appointment_time = ?, status = 'agendado' WHERE id = ?",
        (req.appointment_date, req.appointment_time, app_id)
    )
    conn.commit()
    conn.close()
    return {"success": True, "id": app_id, "appointment_date": req.appointment_date, "appointment_time": req.appointment_time}

# Pacotes do Catálogo
@app.post("/api/admin/services")
def create_service(s: ServiceItem, _=Depends(verify_admin)):
    conn = get_db()
    cursor = conn.cursor()
    cursor.execute(
        "INSERT INTO services (name, category, sessions_count, duration_minutes, price) VALUES (?, ?, ?, ?, ?)",
        (s.name, s.category, s.sessions_count, s.duration_minutes, s.price)
    )
    new_id = cursor.lastrowid
    conn.commit()
    conn.close()
    return {"success": True, "id": new_id, "name": s.name}

@app.put("/api/admin/services/{service_id}")
def update_service(service_id: int, s: ServiceItem, _=Depends(verify_admin)):
    conn = get_db()
    cursor = conn.cursor()
    cursor.execute("""
        UPDATE services 
        SET name = ?, category = ?, sessions_count = ?, duration_minutes = ?, price = ?
        WHERE id = ?
    """, (s.name, s.category, s.sessions_count, s.duration_minutes, s.price, service_id))
    conn.commit()
    conn.close()
    return {"success": True, "id": service_id, "name": s.name}

@app.delete("/api/admin/services/{service_id}")
def delete_service(service_id: int, _=Depends(verify_admin)):
    conn = get_db()
    conn.execute("DELETE FROM services WHERE id = ?", (service_id,))
    conn.commit()
    conn.close()
    return {"success": True, "id": service_id}

# Rotas de Arquivos Estáticos com Fallback Seguro
os.makedirs(STATIC_DIR, exist_ok=True)
os.makedirs(UPLOAD_DIR, exist_ok=True)
app.mount("/static/uploads", StaticFiles(directory=UPLOAD_DIR), name="uploads")
app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")

@app.get("/")
def index_redirect():
    agendar_file = os.path.join(STATIC_DIR, "agendar.html")
    if os.path.exists(agendar_file):
        return FileResponse(agendar_file)
    return JSONResponse({"status": "Laser Clinic App está online!", "instrucao": "Arquivos estáticos sendo carregados."})

@app.get("/agendar")
def client_booking_page():
    agendar_file = os.path.join(STATIC_DIR, "agendar.html")
    if os.path.exists(agendar_file):
        return FileResponse(agendar_file)
    raise HTTPException(status_code=404, detail="Página agendar.html não encontrada na pasta static.")

@app.get("/admin")
def admin_panel_page():
    admin_file = os.path.join(STATIC_DIR, "admin.html")
    if os.path.exists(admin_file):
        return FileResponse(admin_file)
    raise HTTPException(status_code=404, detail="Página admin.html não encontrada na pasta static.")

@app.get("/ficha")
@app.get("/anamnese")
@app.get("/cadastro")
def client_ficha_page():
    ficha_file = os.path.join(STATIC_DIR, "ficha.html")
    if os.path.exists(ficha_file):
        return FileResponse(ficha_file)
    raise HTTPException(status_code=404, detail="Página ficha.html não encontrada na pasta static.")

if __name__ == "__main__":
    import uvicorn
    uvicorn.run("main:app", host="0.0.0.0", port=8000, reload=True)
