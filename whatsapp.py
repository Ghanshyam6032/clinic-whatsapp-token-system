import os
import requests
import zoneinfo
from datetime import datetime, date
from sqlalchemy.orm import Session
from sqlalchemy import func, text
from sqlalchemy.exc import IntegrityError
from models import Clinic, Patient, Visit, VisitStatus, ClinicCalendar, ConversationState

IST = zoneinfo.ZoneInfo("Asia/Kolkata")

def get_today_ist() -> date:
    return datetime.now(IST).date()

def send_whatsapp_message(phone_number_id: str, to: str, text_msg: str, reason_for_send: str, message_id: str, state: str):
    print("\n[WHATSAPP SEND]")
    print(f"to: {to}")
    print(f"message: {text_msg}")
    print(f"reason_for_send: {reason_for_send}")
    print(f"message_id: {message_id}")
    print(f"state: {state}")
    
    access_token = os.getenv("WHATSAPP_ACCESS_TOKEN")
    if not access_token:
        print("WHATSAPP_ACCESS_TOKEN missing. Message suppressed.")
        return

    url = f"https://graph.facebook.com/v26.0/{phone_number_id}/messages"
    headers = {
        "Authorization": f"Bearer {access_token}",
        "Content-Type": "application/json"
    }
    payload = {
        "messaging_product": "whatsapp",
        "recipient_type": "individual",
        "to": to,
        "type": "text",
        "text": {"body": text_msg}
    }
    try:
        res = requests.post(url, headers=headers, json=payload, timeout=10)
        res.raise_for_status()
        print("→ Message sent successfully to Meta API.")
    except Exception as e:
        print(f"→ Error sending WhatsApp message to {to}: {str(e)}")

def get_or_create_state(db: Session, clinic_id: int, whatsapp_number: str) -> ConversationState:
    state_rec = db.query(ConversationState).filter(
        ConversationState.clinic_id == clinic_id,
        ConversationState.whatsapp_number == whatsapp_number
    ).first()
    if not state_rec:
        state_rec = ConversationState(
            clinic_id=clinic_id,
            whatsapp_number=whatsapp_number,
            state="MAIN_MENU",
            temporary_data={}
        )
        db.add(state_rec)
        db.commit()
        db.refresh(state_rec)
    return state_rec

def reset_state(db: Session, clinic_id: int, whatsapp_number: str):
    state_rec = get_or_create_state(db, clinic_id, whatsapp_number)
    state_rec.state = "MAIN_MENU"
    state_rec.temporary_data = {}
    db.commit()

def generate_daily_token(db: Session, clinic_id: int, patient_id: int, reason: str) -> Visit:
    today = get_today_ist()

    closed_rec = db.query(ClinicCalendar).filter(
        ClinicCalendar.clinic_id == clinic_id,
        ClinicCalendar.date == today,
        ClinicCalendar.status == "CLOSED"
    ).first()
    if closed_rec:
        raise ValueError("CLOSED_CLINIC")

    active_visit = db.query(Visit).filter(
        Visit.clinic_id == clinic_id,
        Visit.patient_id == patient_id,
        Visit.visit_date == today,
        Visit.status.in_([VisitStatus.WAITING, VisitStatus.CURRENT])
    ).first()

    if active_visit:
        raise ValueError(f"ACTIVE_TOKEN:{active_visit.token_number}")

    try:
        if not db.bind.dialect.name == "sqlite":
            db.execute(text("LOCK TABLE visits IN EXCLUSIVE MODE;"))

        max_token = db.query(func.max(Visit.token_number)).filter(
            Visit.clinic_id == clinic_id,
            Visit.visit_date == today
        ).scalar() or 0

        next_token = max_token + 1
        new_visit = Visit(
            clinic_id=clinic_id,
            patient_id=patient_id,
            token_number=next_token,
            visit_date=today,
            visit_reason=reason,
            status=VisitStatus.WAITING
        )
        db.add(new_visit)
        db.commit()
        db.refresh(new_visit)
        return new_visit
    except Exception as e:
        db.rollback()
        raise e

def process_whatsapp_message(db: Session, phone_number_id: str, sender_phone: str, msg_body: str, message_id: str):
    clinic = db.query(Clinic).filter(Clinic.whatsapp_phone_number_id == phone_number_id).first()
    if not clinic:
        print(f"No clinic configured for phone_number_id: {phone_number_id}")
        return

    msg_text = msg_body.strip()
    lower_text = msg_text.lower()

    state_rec = get_or_create_state(db, clinic.id, sender_phone)
    state = state_rec.state

    print("\n========================================")
    print("WHATSAPP INCOMING MESSAGE")
    print(f"message_id: {message_id}")
    print(f"phone_number_id: {phone_number_id}")
    print(f"from: {sender_phone}")
    print(f"text: {msg_text}")
    print(f"current_state: {state}")
    print(f"temporary_data: {state_rec.temporary_data}")
    print("========================================")

    def reply(text_msg: str, reason: str, updated_state: str):
        send_whatsapp_message(phone_number_id, sender_phone, text_msg, reason, message_id, updated_state)
        print("========================================\n")

    if lower_text in ["menu", "hi", "hello", "cancel", "reset"]:
        reset_state(db, clinic.id, sender_phone)
        msg = f"👋 Welcome to {clinic.name}\n\nPlease select an option:\n\n1️⃣ Get Token\n2️⃣ Check Token Status\n3️⃣ Cancel Token\n4️⃣ Help"
        reply(msg, "User invoked global menu/reset command", "MAIN_MENU")
        return

    if state == "MAIN_MENU":
        if msg_text == "1":
            patient = db.query(Patient).filter(
                Patient.clinic_id == clinic.id, 
                Patient.whatsapp_number == sender_phone
            ).first()

            if patient:
                print(f"[PATIENT FOUND] patient_id={patient.id} (Returning patient detected)")
                state_rec.state = "CONFIRM_EXISTING"
                state_rec.temporary_data = {"patient_id": patient.id}
                db.commit()
                reply(
                    f"👋 Welcome back, {patient.name}!\n\nYour saved details were found.\n\nPlease confirm your token request:\n1️⃣ Continue\n2️⃣ Cancel",
                    "Prompting returning patient for confirmation", 
                    "CONFIRM_EXISTING"
                )
            else:
                state_rec.state = "WAITING_FOR_NAME"
                state_rec.temporary_data = {}
                db.commit()
                reply("Please enter your full name.", "Prompting new patient for name", "WAITING_FOR_NAME")

        elif msg_text == "2":
            today = get_today_ist()
            patient = db.query(Patient).filter(Patient.clinic_id == clinic.id, Patient.whatsapp_number == sender_phone).first()
            if not patient:
                reply("You have no registered tokens today. Option 1 to Get Token.", "Check status - No patient", "MAIN_MENU")
                return

            visit = db.query(Visit).filter(Visit.clinic_id == clinic.id, Visit.patient_id == patient.id, Visit.visit_date == today).order_by(Visit.id.desc()).first()
            if not visit:
                reply("You do not have a token registered for today.", "Check status - No visit", "MAIN_MENU")
                return

            if visit.status == VisitStatus.CANCELLED:
                reply(f"🎫 Your Token #{visit.token_number} was CANCELLED.", "Check status - Cancelled", "MAIN_MENU")
            elif visit.status == VisitStatus.COMPLETED:
                reply(f" Your visit for today (Token #{visit.token_number}) has been COMPLETED.", "Check status - Completed", "MAIN_MENU")
            elif visit.status == VisitStatus.CURRENT:
                reply(f"🎫 Your Token: #{visit.token_number}\n\n🟢 It is currently your turn!\nPlease proceed to the doctor.", "Check status - Current", "MAIN_MENU")
            elif visit.status == VisitStatus.WAITING:
                curr_visit = db.query(Visit).filter(Visit.clinic_id == clinic.id, Visit.visit_date == today, Visit.status == VisitStatus.CURRENT).first()
                c_num = f"#{curr_visit.token_number}" if curr_visit else "Not Started"
                ahead = db.query(Visit).filter(Visit.clinic_id == clinic.id, Visit.visit_date == today, Visit.status == VisitStatus.WAITING, Visit.token_number < visit.token_number).count()
                reply(f"🎫 Your Token: #{visit.token_number}\n👨‍⚕️ Current Token: {c_num}\n👥 Patients before you: {ahead}\n🟢 Status: WAITING", "Check status - Waiting", "MAIN_MENU")

        elif msg_text == "3":
            today = get_today_ist()
            patient = db.query(Patient).filter(Patient.clinic_id == clinic.id, Patient.whatsapp_number == sender_phone).first()
            if not patient:
                reply("You do not have an active token today.", "Cancel requested - No patient", "MAIN_MENU")
                return
            visit = db.query(Visit).filter(Visit.clinic_id == clinic.id, Visit.patient_id == patient.id, Visit.visit_date == today, Visit.status.in_([VisitStatus.WAITING, VisitStatus.CURRENT])).first()
            if not visit:
                reply("You do not have an active token today.", "Cancel requested - No active visit", "MAIN_MENU")
                return

            state_rec.state = "CONFIRM_CANCEL"
            state_rec.temporary_data = dict({"visit_id": visit.id})
            db.commit()
            reply(f"You have Token #{visit.token_number}.\n\nAre you sure you want to cancel it?\n\n1️⃣ Yes\n2️⃣ No", "Prompting cancel confirmation", "CONFIRM_CANCEL")

        elif msg_text == "4":
            reply(f"ℹ️ {clinic.name} Help Center\n\nReply with 'menu' anytime to view main options.\nTo book a token select 1.\nTo check token status select 2.", "Sending Help text", "MAIN_MENU")
        else:
            reply(f"⚠️ Please select a valid option.\n\n👋 Welcome to {clinic.name}\n\n1️⃣ Get Token\n2️⃣ Check Token Status\n3️⃣ Cancel Token\n4️⃣ Help", "Invalid menu option selected", "MAIN_MENU")

    elif state == "CONFIRM_EXISTING":
        if msg_text == "1":
            state_rec.state = "WAITING_FOR_REASON"
            db.commit()
            reply("Please briefly tell us the reason for today's visit (e.g. Fever, Checkup).", "Prompting returning patient for reason", "WAITING_FOR_REASON")
        else:
            reset_state(db, clinic.id, sender_phone)
            reply("Token request cancelled. Type 'hi' to start again.", "Patient cancelled returning flow", "MAIN_MENU")

    elif state == "WAITING_FOR_NAME":
        if len(msg_text) < 2 or len(msg_text) > 100 or msg_text.isdigit():
            reply("⚠️ Please enter a valid full name (2 to 100 characters).", "Invalid name format", state)
            return

        current_data = state_rec.temporary_data if isinstance(state_rec.temporary_data, dict) else {}
        new_data = dict(current_data)
        new_data["name"] = msg_text
        state_rec.temporary_data = new_data
        state_rec.state = "WAITING_FOR_AGE"
        db.commit()
        reply("Please enter your age (e.g., 25).", "Prompting new patient for age", "WAITING_FOR_AGE")

    elif state == "WAITING_FOR_AGE":
        if not msg_text.isdigit() or not (0 <= int(msg_text) <= 120):
            reply("⚠️ Please enter a valid age between 0 and 120.", "Invalid age format", state)
            return

        current_data = state_rec.temporary_data if isinstance(state_rec.temporary_data, dict) else {}
        new_data = dict(current_data)
        new_data["age"] = int(msg_text)
        state_rec.temporary_data = new_data
        state_rec.state = "WAITING_FOR_GENDER"
        db.commit()
        reply("Please select your gender:\n1️⃣ Male\n2️⃣ Female\n3️⃣ Other", "Prompting new patient for gender", "WAITING_FOR_GENDER")

    elif state == "WAITING_FOR_GENDER":
        gender_map = {"1": "Male", "2": "Female", "3": "Other"}
        if msg_text not in gender_map:
            reply("⚠️ Please select a valid gender option:\n1️⃣ Male\n2️⃣ Female\n3️⃣ Other", "Invalid gender option", state)
            return

        current_data = state_rec.temporary_data if isinstance(state_rec.temporary_data, dict) else {}
        new_data = dict(current_data)
        new_data["gender"] = gender_map[msg_text]
        state_rec.temporary_data = new_data
        state_rec.state = "WAITING_FOR_REASON"
        db.commit()
        reply("Please briefly tell us the reason for today's visit (e.g., Fever, Stomach Pain).", "Prompting new patient for reason", "WAITING_FOR_REASON")

    elif state == "WAITING_FOR_REASON":
        if len(msg_text) < 1 or len(msg_text) > 250:
            reply("⚠️ Please enter a valid reason between 1 and 250 characters.", "Invalid reason length", state)
            return

        db.refresh(state_rec)
        temp_data = state_rec.temporary_data if isinstance(state_rec.temporary_data, dict) else {}
        patient_id = temp_data.get("patient_id")

        if not patient_id:
            # Explicit uniqueness check before attempting to create the patient
            existing_patient = db.query(Patient).filter(
                Patient.clinic_id == clinic.id,
                Patient.whatsapp_number == sender_phone
            ).first()

            if existing_patient:
                print(f"[DUPLICATE PATIENT PREVENTED] clinic_id={clinic.id}, whatsapp_number={sender_phone}")
                print(f"[PATIENT FOUND] patient_id={existing_patient.id}")
                patient_id = existing_patient.id
                
                # Update demographic info only if appropriate (from temp memory)
                p_name = temp_data.get("name")
                p_age = temp_data.get("age")
                p_gender = temp_data.get("gender")
                
                if p_name: existing_patient.name = p_name
                if p_age: existing_patient.age = p_age
                if p_gender: existing_patient.gender = p_gender
                
                existing_patient.updated_at = datetime.utcnow()
                db.commit()
            else:
                p_name = temp_data.get("name")
                p_age = temp_data.get("age")
                p_gender = temp_data.get("gender")

                missing_fields = []
                if not p_name: missing_fields.append("name")
                if p_age is None: missing_fields.append("age")
                if not p_gender: missing_fields.append("gender")

                if missing_fields:
                    reply("⚠️ Sorry, some of your registration details were missing. Please type 'menu' and start again.", "Missing temporary data", state)
                    reset_state(db, clinic.id, sender_phone)
                    return

                try:
                    patient = Patient(
                        clinic_id=clinic.id,
                        name=p_name,
                        whatsapp_number=sender_phone,
                        age=p_age,
                        gender=p_gender
                    )
                    db.add(patient)
                    db.commit()
                    db.refresh(patient)
                    patient_id = patient.id
                    print(f"[NEW PATIENT] patient_id={patient_id}")
                except IntegrityError:
                    db.rollback()
                    # Fallback lookup in case a race condition still managed to trigger UniqueViolation
                    existing_patient = db.query(Patient).filter(
                        Patient.clinic_id == clinic.id,
                        Patient.whatsapp_number == sender_phone
                    ).first()
                    if existing_patient:
                        print(f"[DUPLICATE PATIENT PREVENTED] clinic_id={clinic.id}, whatsapp_number={sender_phone} (via IntegrityError)")
                        patient_id = existing_patient.id
                        print(f"[PATIENT FOUND] patient_id={patient_id}")
                    else:
                        raise
        else:
            print(f"[PATIENT FOUND] patient_id={patient_id} (Resolved via state)")

        patient = db.query(Patient).get(patient_id)

        try:
            visit = generate_daily_token(db, clinic.id, patient_id, msg_text)
            print(f"[NEW VISIT] visit_id={visit.id} (Token #{visit.token_number})")
            
            today = get_today_ist()
            current_visit = db.query(Visit).filter(Visit.clinic_id == clinic.id, Visit.visit_date == today, Visit.status == VisitStatus.CURRENT).first()
            current_num = current_visit.token_number if current_visit else 0
            patients_ahead = db.query(Visit).filter(Visit.clinic_id == clinic.id, Visit.visit_date == today, Visit.status == VisitStatus.WAITING, Visit.token_number < visit.token_number).count()
            curr_str = f"#{current_num}" if current_num > 0 else "Not Started"

            reply(
                f"✅ Token Generated Successfully!\n\n🎫 Token: #{visit.token_number}\n\n👤 {patient.name}\n🎂 Age: {patient.age}\n🩺 Reason: {visit.visit_reason}\n\n👨‍⚕️ Current Token: {curr_str}\n👥 Patients before you: {patients_ahead}\n\nPlease visit the clinic according to your token number.\n\nThank you.",
                "Successful daily token generated",
                "MAIN_MENU"
            )
            reset_state(db, clinic.id, sender_phone)

        except ValueError as e:
            err = str(e)
            if err == "CLOSED_CLINIC":
                reply(f"🔴 Clinic Closed\n\n{clinic.name} is closed today.\n\nNo tokens are available today.", "Clinic calendar marked closed", "MAIN_MENU")
            elif err.startswith("ACTIVE_TOKEN"):
                tok = err.split(":")[1]
                today = get_today_ist()
                curr_visit = db.query(Visit).filter(Visit.clinic_id == clinic.id, Visit.visit_date == today, Visit.status == VisitStatus.CURRENT).first()
                c_num = f"#{curr_visit.token_number}" if curr_visit else "Not Started"
                reply(
                    f"⚠️ You already have an active token today.\n\n🎫 Your Token: #{tok}\n👨‍⚕️ Current Token: {c_num}\n\nPlease use 'Check Token Status' to view your position.",
                    "Active token conflict protected", 
                    "MAIN_MENU"
                )
            reset_state(db, clinic.id, sender_phone)

    elif state == "CONFIRM_CANCEL":
        if msg_text == "1":
            visit_id = temp_data.get("visit_id")
            visit = db.query(Visit).get(visit_id)
            if visit and visit.status in [VisitStatus.WAITING, VisitStatus.CURRENT]:
                visit.status = VisitStatus.CANCELLED
                visit.cancelled_at = datetime.utcnow()
                db.commit()
                reply(f"✅ Token #{visit.token_number} has been cancelled successfully.", "User confirmed token cancellation", "MAIN_MENU")
            else:
                reply("Token could not be cancelled.", "Cancel confirmation failed logic check", "MAIN_MENU")
        else:
            reply("Token cancellation aborted.", "User backed out of cancellation", "MAIN_MENU")
        reset_state(db, clinic.id, sender_phone)