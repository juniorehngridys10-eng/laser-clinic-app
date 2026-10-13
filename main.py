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

BASE_DIR = os.path.abspath(os.path.dirname(__file__))
DB_PATH = os.path.join(BASE_DIR, "clinic.db")
STATIC_DIR = os.path.join(BASE_DIR, "static")
UPLOAD_DIR = os.path.join(STATIC_DIR, "uploads")
os.makedirs(UPLOAD_DIR, exist_ok=True)

ADMIN_PIN = "1234"  # Senha padrão de acesso ao Painel da Clínica

app = FastAPI(title="Laser Clinic Agendamento & Gestão")

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

    # Tabela de Configurações da Clínica (Nome e Logotipo)
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

    # Tabela de Serviços e Pacotes
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

    cursor.execute("""
    CREATE TABLE IF NOT EXISTS clients (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        name TEXT NOT NULL,
        phone TEXT NOT NULL UNIQUE,
        email TEXT,
        created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
    );
    """)

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

    # Popula procedimentos padrão caso vazio
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

def verify_admin(x_admin_pin: Optional[str] = Header(None)):
    if x_admin_pin != ADMIN_PIN:
        raise HTTPException(status_code=401, detail="Acesso não autorizado. Digite o PIN da clínica.")
    return True

# --- Endpoints Públicos (Clientes e Identidade Visual) ---

@app.get("/api/settings")
def get_clinic_settings():
    conn = get_db()
    row = conn.execute("SELECT * FROM clinic_settings WHERE id = 1").fetchone()
    conn.close()
    if row:
        return dict(row)
    return {
        "name": "LaserGlow Clinic",
        "subtitle": "Depilação a Laser de Alta Tecnologia",
        "logo_url": "",
        "phone": "",
        "instagram": ""
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
    booked = conn.execute(
        "SELECT appointment_time FROM appointments WHERE appointment_date = ? AND status != 'cancelado'",
        (date,)
    ).fetchall()
    conn.close()

    booked_times = set(r["appointment_time"] for r in booked)

    all_slots = []
    start_hour = 8
    end_hour = 18
    for h in range(start_hour, end_hour):
        for m in (0, 30):
            slot = f"{h:02d}:{m:02d}"
            all_slots.append(slot)

    slots_status = [
        {"time": slot, "available": slot not in booked_times}
        for slot in all_slots
    ]
    return {"date": date, "slots": slots_status}

@app.post("/api/book")
def book_appointment(data: BookingRequest):
    conn = get_db()
    cursor = conn.cursor()

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
        SET name = ?, subtitle = ?, logo_url = ?, phone = ?, instagram = ?
        WHERE id = 1
    """, (settings.name, settings.subtitle, settings.logo_url, settings.phone, settings.instagram))
    conn.commit()
    conn.close()
    return {"success": True, "message": "Configurações salvas com sucesso!"}

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

    # Salva no banco de dados automaticamente
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

@app.get("/api/clients")
def list_clients(_=Depends(verify_admin)):
    conn = get_db()
    query = """
        SELECT c.id, c.name, c.phone, c.email, c.created_at,
               COUNT(a.id) as total_appointments,
               MAX(a.appointment_date) as last_appointment_date
        FROM clients c
        LEFT JOIN appointments a ON c.id = a.client_id
        GROUP BY c.id
        ORDER BY c.name ASC
    """
    rows = conn.execute(query).fetchall()
    conn.close()
    return [dict(r) for r in rows]

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

# Pacotes e Procedimentos
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

if __name__ == "__main__":
    import uvicorn
    uvicorn.run("main:app", host="0.0.0.0", port=8000, reload=True)
