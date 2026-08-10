import os
import traceback
import calendar as pycalendar
from datetime import datetime, date
from typing import Optional, List

from fastapi import FastAPI, Depends, HTTPException, Query, Request, status
from fastapi.responses import HTMLResponse, JSONResponse
from fastapi.middleware.cors import CORSMiddleware
from fastapi.security import OAuth2PasswordRequestForm
from sqlalchemy.orm import Session
from sqlalchemy import func, or_
from sqlalchemy.exc import IntegrityError

from database import Base, engine, get_db, SessionLocal
from models import Doctor, Clinic, Patient, Visit, VisitStatus, ClinicCalendar, ProcessedWebhookEvent
from schemas import (
    TokenSchema, DoctorOutSchema,
    DashboardSummaryOutSchema, VisitOutSchema, PatientOutSchema, ManualPatientAddSchema,
    PatientSummarySchema
)
from security import verify_password, create_access_token, get_current_doctor, get_password_hash
from whatsapp import process_whatsapp_message, get_today_ist, generate_daily_token

Base.metadata.create_all(bind=engine)

app = FastAPI(title="WhatsApp Clinic Token System API", version="1.0.0")

# CORS is configured to allow any frontend origin (like GitHub Pages) to access this backend
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

@app.on_event("startup")
def create_default_admin():
    db = SessionLocal()
    try:
        admin_email = os.getenv("ADMIN_USERNAME", "admin")
        admin_password = os.getenv("ADMIN_PASSWORD", "ChangeThisAdminPassword123!")
        
        clinic = db.query(Clinic).first()
        if not clinic:
            clinic = Clinic(
                name="System Default Clinic",
                whatsapp_phone_number_id=os.getenv("WHATSAPP_PHONE_NUMBER_ID", "default_phone_id")
            )
            db.add(clinic)
            db.commit()
            db.refresh(clinic)
            
        admin = db.query(Doctor).filter(Doctor.email == admin_email).first()
        if not admin:
            admin = Doctor(
                clinic_id=clinic.id,
                name="Admin Doctor",
                email=admin_email,
                password_hash=get_password_hash(admin_password),
                is_active=True
            )
            db.add(admin)
            db.commit()
    except Exception as e:
        print(f"Error initializing admin account: {e}")
    finally:
        db.close()


# =========================================================================
# MAGIC SETUP ENDPOINT (डेटाबेस को फिक्स करने के लिए)
# =========================================================================
@app.get("/setup")
def setup_database_admin(db: Session = Depends(get_db)):
    try:
        admin_email = os.getenv("ADMIN_USERNAME", "admin")
        admin_password = os.getenv("ADMIN_PASSWORD", "ChangeThisAdminPassword123!")

        # 1. Ensure Clinic Exists
        clinic = db.query(Clinic).first()
        if not clinic:
            clinic = Clinic(
                name="System Default Clinic",
                whatsapp_phone_number_id=os.getenv("WHATSAPP_PHONE_NUMBER_ID", "default_phone_id")
            )
            db.add(clinic)
            db.commit()
            db.refresh(clinic)

        # 2. Check existing accounts
        all_doctors = db.query(Doctor).all()
        doctor_emails = [d.email for d in all_doctors]

        # 3. Force Update or Create Admin
        admin = db.query(Doctor).filter(Doctor.email == admin_email).first()
        if admin:
            admin.password_hash = get_password_hash(admin_password)
            db.commit()
            return {
                "status": "success", 
                "message": f"Password updated for existing user: {admin_email}", 
                "existing_accounts": doctor_emails
            }
        else:
            new_admin = Doctor(
                clinic_id=clinic.id,
                name="Admin Doctor",
                email=admin_email,
                password_hash=get_password_hash(admin_password),
                is_active=True
            )
            db.add(new_admin)
            db.commit()
            return {
                "status": "success", 
                "message": f"Created NEW admin account: {admin_email}", 
                "existing_accounts": doctor_emails
            }

    except Exception as e:
        db.rollback()
        return {
            "status": "error", 
            "error_message": str(e), 
            "traceback": traceback.format_exc()
        }
# =========================================================================


@app.get("/health")
def health_check():
    return {"status": "healthy"}

@app.get("/")
def read_root():
    return {"status": "online"}

# =========================================================================
# PASSWORD BYPASS: यहाँ से पासवर्ड चेकिंग हटा दी गई है 
# =========================================================================
@app.post("/auth/login", response_model=TokenSchema)
def login(form_data: OAuth2PasswordRequestForm = Depends(), db: Session = Depends(get_db)):
    # सिर्फ ईमेल चेक कर रहे हैं, पासवर्ड चेक को पूरी तरह से इग्नोर कर दिया गया है
    doctor = db.query(Doctor).filter(Doctor.email == form_data.username).first()
    
    if not doctor:
        raise HTTPException(status_code=400, detail="Admin email not found in database")

    # अब यूज़र पासवर्ड बॉक्स में कुछ भी डाले, सिस्टम उसे सीधे एक्सेस दे देगा
    access_token = create_access_token(data={"sub": str(doctor.id)})
    return {"access_token": access_token, "token_type": "bearer"}
# =========================================================================

@app.get("/auth/me", response_model=DoctorOutSchema)
def get_me(current_doctor: Doctor = Depends(get_current_doctor)):
    return DoctorOutSchema(
        id=current_doctor.id,
        clinic_id=current_doctor.clinic_id,
        name=current_doctor.name,
        email=current_doctor.email,
        clinic_name=current_doctor.clinic.name
    )

@app.get("/webhook")
def verify_webhook(request: Request):
    verify_token = os.getenv("WHATSAPP_VERIFY_TOKEN", "verify_token")
    mode = request.query_params.get("hub.mode")
    token = request.query_params.get("hub.verify_token")
    challenge = request.query_params.get("hub.challenge")

    if mode and token:
        if mode == "subscribe" and token == verify_token:
            return HTMLResponse(content=challenge, status_code=200)
        raise HTTPException(status_code=403, detail="Verification failed")
    return {"status": "webhook endpoint ready"}

@app.post("/webhook")
async def receive_webhook(request: Request, db: Session = Depends(get_db)):
    data = await request.json()
    try:
        entries = data.get("entry", [])
        for entry in entries:
            changes = entry.get("changes", [])
            for change in changes:
                value = change.get("value", {})
                
                # Only process genuine incoming text messages, drop system status updates entirely
                if "statuses" in value:
                    continue
                    
                phone_number_id = value.get("metadata", {}).get("phone_number_id")
                messages = value.get("messages", [])

                for msg in messages:
                    msg_id = msg.get("id")
                    sender_phone = msg.get("from")
                    msg_type = msg.get("type")
                    timestamp = msg.get("timestamp")
                    
                    print("\n[WEBHOOK EVENT]")
                    print(f"object: {data.get('object')}")
                    print(f"phone_number_id: {phone_number_id}")
                    print(f"event_type: messages")
                    print(f"message_id: {msg_id}")
                    print(f"from: {sender_phone}")
                    print(f"message_type: {msg_type}")
                    print(f"text: {msg.get('text', {}).get('body', '') if msg_type == 'text' else ''}")
                    print(f"timestamp: {timestamp}")

                    if msg_type == "text" and msg_id and sender_phone and phone_number_id:
                        msg_body = msg.get("text", {}).get("body", "")
                        
                        # Idempotency Tracking: Ensuring Meta retries or duplicates never process twice
                        try:
                            processed_record = ProcessedWebhookEvent(
                                message_id=msg_id,
                                phone_number_id=phone_number_id,
                                sender_number=sender_phone,
                                event_type="text"
                            )
                            db.add(processed_record)
                            db.commit()
                        except IntegrityError:
                            db.rollback()
                            print(f"\n[DUPLICATE WEBHOOK] message_id={msg_id} already processed. Skipping.")
                            continue
                        
                        process_whatsapp_message(db, phone_number_id, sender_phone, msg_body, msg_id)
                            
    except Exception as e:
        print("\n========================================")
        print("WEBHOOK ERROR")
        print(f"error: {str(e)}")
        print("traceback:")
        traceback.print_exc()
        print("========================================")

    return JSONResponse(content={"status": "received"}, status_code=200)

@app.get("/doctor/today", response_model=DashboardSummaryOutSchema)
def get_today_summary(db: Session = Depends(get_db), current_doctor: Doctor = Depends(get_current_doctor)):
    today = get_today_ist()
    clinic_id = current_doctor.clinic_id
    visits = db.query(Visit).filter(Visit.clinic_id == clinic_id, Visit.visit_date == today).order_by(Visit.token_number.asc()).all()

    current_v = next((v for v in visits if v.status == VisitStatus.CURRENT), None)
    next_waiting_v = next((v for v in visits if v.status == VisitStatus.WAITING), None)
    
    waiting_cnt = sum(1 for v in visits if v.status == VisitStatus.WAITING)
    completed_cnt = sum(1 for v in visits if v.status == VisitStatus.COMPLETED)
    cancelled_cnt = sum(1 for v in visits if v.status == VisitStatus.CANCELLED)

    def build_visit_out(v: Optional[Visit]) -> Optional[VisitOutSchema]:
        if not v: return None
        return VisitOutSchema(
            id=v.id, token_number=v.token_number, visit_date=v.visit_date, visit_reason=v.visit_reason,
            status=v.status, created_at=v.created_at, completed_at=v.completed_at, cancelled_at=v.cancelled_at,
            patient_id=v.patient_id, patient_name=v.patient.name, patient_age=v.patient.age,
            patient_gender=v.patient.gender, patient_phone=v.patient.whatsapp_number
        )

    return DashboardSummaryOutSchema(
        clinic_name=current_doctor.clinic.name, today_date=today.strftime("%d %B %Y"), current_visit=build_visit_out(current_v),
        next_waiting_visit=build_visit_out(next_waiting_v), waiting_count=waiting_cnt, completed_count=completed_cnt,
        cancelled_count=cancelled_cnt, total_count=len(visits)
    )

@app.post("/doctor/next-patient")
def next_patient(db: Session = Depends(get_db), current_doctor: Doctor = Depends(get_current_doctor)):
    today = get_today_ist()
    clinic_id = current_doctor.clinic_id

    curr_visit = db.query(Visit).filter(Visit.clinic_id == clinic_id, Visit.visit_date == today, Visit.status == VisitStatus.CURRENT).first()
    if curr_visit:
        curr_visit.status = VisitStatus.COMPLETED
        curr_visit.completed_at = datetime.utcnow()

    next_visit = db.query(Visit).filter(Visit.clinic_id == clinic_id, Visit.visit_date == today, Visit.status == VisitStatus.WAITING).order_by(Visit.token_number.asc()).first()
    if next_visit:
        next_visit.status = VisitStatus.CURRENT
        db.commit()
        return {"message": f"Token #{next_visit.token_number} is now CURRENT"}
    
    db.commit()
    return {"message": "No waiting patients remaining"}

@app.post("/doctor/add-walkin")
def add_walkin_patient(payload: ManualPatientAddSchema, db: Session = Depends(get_db), current_doctor: Doctor = Depends(get_current_doctor)):
    clinic_id = current_doctor.clinic_id
    patient = db.query(Patient).filter(Patient.clinic_id == clinic_id, Patient.whatsapp_number == payload.whatsapp_number).first()

    if not patient:
        patient = Patient(clinic_id=clinic_id, name=payload.name, whatsapp_number=payload.whatsapp_number, age=payload.age, gender=payload.gender)
        db.add(patient)
        db.commit()
        db.refresh(patient)
    
    try:
        visit = generate_daily_token(db, clinic_id, patient.id, payload.visit_reason)
        return {"message": "Patient added successfully", "token_number": visit.token_number}
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))

@app.get("/doctor/calendar/{year}/{month}")
def get_month_calendar(year: int, month: int, db: Session = Depends(get_db), current_doctor: Doctor = Depends(get_current_doctor)):
    clinic_id = current_doctor.clinic_id
    num_days = pycalendar.monthrange(year, month)[1]
    closed_records = db.query(ClinicCalendar).filter(ClinicCalendar.clinic_id == clinic_id, func.extract('year', ClinicCalendar.date) == year, func.extract('month', ClinicCalendar.date) == month, ClinicCalendar.status == "CLOSED").all()
    closed_days = {r.date.day for r in closed_records}
    
    calendar_days = []
    for day in range(1, num_days + 1):
        status = "CLOSED" if day in closed_days else "OPEN"
        calendar_days.append({"day": day, "date": f"{year}-{month:02d}-{day:02d}", "status": status})
    return {"year": year, "month": month, "days": calendar_days}

@app.post("/doctor/calendar/{date_str}/close")
def close_clinic_date(date_str: str, db: Session = Depends(get_db), current_doctor: Doctor = Depends(get_current_doctor)):
    d = datetime.strptime(date_str, "%Y-%m-%d").date()
    clinic_id = current_doctor.clinic_id
    rec = db.query(ClinicCalendar).filter(ClinicCalendar.clinic_id == clinic_id, ClinicCalendar.date == d).first()
    if not rec:
        rec = ClinicCalendar(clinic_id=clinic_id, date=d, status="CLOSED")
        db.add(rec)
    else:
        rec.status = "CLOSED"
    db.commit()
    return {"message": f"Date {date_str} is now CLOSED"}

@app.post("/doctor/calendar/{date_str}/open")
def open_clinic_date(date_str: str, db: Session = Depends(get_db), current_doctor: Doctor = Depends(get_current_doctor)):
    d = datetime.strptime(date_str, "%Y-%m-%d").date()
    rec = db.query(ClinicCalendar).filter(ClinicCalendar.clinic_id == current_doctor.clinic_id, ClinicCalendar.date == d).first()
    if rec:
        db.delete(rec)
        db.commit()
    return {"message": f"Date {date_str} is now OPEN"}

@app.get("/doctor/patients-summary", response_model=PatientSummarySchema)
def get_patients_summary(query: Optional[str] = None, start_date: Optional[str] = None, end_date: Optional[str] = None, db: Session = Depends(get_db), current_doctor: Doctor = Depends(get_current_doctor)):
    s_date = datetime.strptime(start_date, "%Y-%m-%d").date() if start_date else None
    e_date = datetime.strptime(end_date, "%Y-%m-%d").date() if end_date else None

    pq = db.query(Patient).filter(Patient.clinic_id == current_doctor.clinic_id)
    if query:
        pq = pq.filter(or_(Patient.name.ilike(f"%{query}%"), Patient.whatsapp_number.ilike(f"%{query}%")))
    
    patient_ids = {p.id for p in pq.all()}

    if not patient_ids:
        return PatientSummarySchema(total_patients=0, new_patients=0, returning_patients=0, total_visits=0, completed_visits=0, cancelled_visits=0, waiting_visits=0)

    vq = db.query(Visit).filter(Visit.clinic_id == current_doctor.clinic_id, Visit.patient_id.in_(patient_ids))
    if s_date: vq = vq.filter(Visit.visit_date >= s_date)
    if e_date: vq = vq.filter(Visit.visit_date <= e_date)
    visits = vq.all()

    total_visits = len(visits)
    completed_visits = sum(1 for v in visits if v.status == VisitStatus.COMPLETED)
    cancelled_visits = sum(1 for v in visits if v.status == VisitStatus.CANCELLED)
    waiting_visits = sum(1 for v in visits if v.status in [VisitStatus.WAITING, VisitStatus.CURRENT])

    patient_ids_in_range = {v.patient_id for v in visits}
    
    if not s_date and not e_date:
        total_patients = len(patient_ids)
        new_patients = total_patients 
        returning_patients = 0
    else:
        total_patients = len(patient_ids_in_range)
        new_patients = 0
        for pid in patient_ids_in_range:
            p = db.query(Patient).get(pid)
            if p and s_date <= p.created_at.date() <= e_date:
                new_patients += 1
        returning_patients = total_patients - new_patients

    return PatientSummarySchema(total_patients=total_patients, new_patients=new_patients, returning_patients=returning_patients, total_visits=total_visits, completed_visits=completed_visits, cancelled_visits=cancelled_visits, waiting_visits=waiting_visits)

@app.get("/doctor/patients", response_model=List[PatientOutSchema])
def search_patients(query: Optional[str] = None, start_date: Optional[str] = None, end_date: Optional[str] = None, db: Session = Depends(get_db), current_doctor: Doctor = Depends(get_current_doctor)):
    q = db.query(Patient).filter(Patient.clinic_id == current_doctor.clinic_id)
    if query:
        q = q.filter(or_(Patient.name.ilike(f"%{query}%"), Patient.whatsapp_number.ilike(f"%{query}%")))

    patients = q.order_by(Patient.id.desc()).all()
    s_date = datetime.strptime(start_date, "%Y-%m-%d").date() if start_date else None
    e_date = datetime.strptime(end_date, "%Y-%m-%d").date() if end_date else None

    res = []
    for p in patients:
        vq = db.query(Visit).filter(Visit.patient_id == p.id)
        if s_date: vq = vq.filter(Visit.visit_date >= s_date)
        if e_date: vq = vq.filter(Visit.visit_date <= e_date)
        
        visits = vq.order_by(Visit.id.asc()).all()
        if not visits and (s_date or e_date):
            continue
            
        completed = sum(1 for v in visits if v.status == VisitStatus.COMPLETED)
        cancelled = sum(1 for v in visits if v.status == VisitStatus.CANCELLED)
        waiting = sum(1 for v in visits if v.status in [VisitStatus.WAITING, VisitStatus.CURRENT])
        first_visit = visits[0] if visits else None
        last_visit = visits[-1] if visits else None
        
        res.append(PatientOutSchema(
            id=p.id, name=p.name, whatsapp_number=p.whatsapp_number, age=p.age, gender=p.gender, created_at=p.created_at, visit_count=len(visits),
            first_visit_date=first_visit.visit_date if first_visit else None, last_visit_date=last_visit.visit_date if last_visit else None,
            last_token_number=last_visit.token_number if last_visit else None, last_visit_reason=last_visit.visit_reason if last_visit else None,
            total_completed=completed, total_cancelled=cancelled, total_waiting=waiting
        ))
    return res

@app.get("/doctor/patients/{patient_id}/history", response_model=List[VisitOutSchema])
def get_patient_history(patient_id: int, db: Session = Depends(get_db), current_doctor: Doctor = Depends(get_current_doctor)):
    patient = db.query(Patient).filter(Patient.id == patient_id, Patient.clinic_id == current_doctor.clinic_id).first()
    if not patient: raise HTTPException(status_code=404, detail="Patient not found")
    visits = db.query(Visit).filter(Visit.patient_id == patient_id).order_by(Visit.id.desc()).all()
    return [
        VisitOutSchema(
            id=v.id, token_number=v.token_number, visit_date=v.visit_date, visit_reason=v.visit_reason, status=v.status,
            created_at=v.created_at, completed_at=v.completed_at, cancelled_at=v.cancelled_at, patient_id=patient.id,
            patient_name=patient.name, patient_age=patient.age, patient_gender=patient.gender, patient_phone=patient.whatsapp_number
        ) for v in visits
    ]

@app.get("/doctor/tokens", response_model=List[VisitOutSchema])
def get_tokens_by_date(date_str: Optional[str] = None, status_filter: Optional[str] = "ALL", db: Session = Depends(get_db), current_doctor: Doctor = Depends(get_current_doctor)):
    req_date = datetime.strptime(date_str, "%Y-%m-%d").date() if date_str else get_today_ist()
    q = db.query(Visit).filter(Visit.clinic_id == current_doctor.clinic_id, Visit.visit_date == req_date)
    if status_filter and status_filter != "ALL":
        q = q.filter(Visit.status == status_filter)
    visits = q.order_by(Visit.token_number.asc()).all()
    return [
        VisitOutSchema(
            id=v.id, token_number=v.token_number, visit_date=v.visit_date, visit_reason=v.visit_reason, status=v.status,
            created_at=v.created_at, completed_at=v.completed_at, cancelled_at=v.cancelled_at, patient_id=v.patient_id,
            patient_name=v.patient.name, patient_age=v.patient.age, patient_gender=v.patient.gender, patient_phone=v.patient.whatsapp_number
        ) for v in visits
    ]
