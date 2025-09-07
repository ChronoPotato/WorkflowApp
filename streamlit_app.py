"""
Streamlit Workflow Tracker — Fee Uplift (Starter App)
-----------------------------------------------------
A production-ready starter for tracking multi-team workflows around client letters
and fee uplifts. Uses SQLAlchemy ORM on top of PostgreSQL (or SQLite fallback),
with a simple state machine and an audit trail. Designed to be extended with
DocuSign / wet-signature handling and IO (e.g., Intelliflo Office) integrations.

Run:
  streamlit run app.py

Configure DB (recommended):
  In .streamlit/secrets.toml, set:
    db_url = "postgresql+psycopg2://user:pass@host:5432/yourdb"

Otherwise it falls back to a local SQLite file ./fee_uplift.db
"""

from __future__ import annotations

import os
import json
from datetime import datetime, timedelta
from typing import Dict, List, Optional, Tuple

import pandas as pd
import streamlit as st
from sqlalchemy import (
    create_engine,
    Column,
    Integer,
    String,
    DateTime,
    Enum,
    Boolean,
    ForeignKey,
    Text,
    JSON,
    event,
)
from sqlalchemy.orm import declarative_base, relationship, sessionmaker, Session
import enum

# -------------------------
# Database setup
# -------------------------

DB_URL = (
    st.secrets.get("db_url")
    if hasattr(st, "secrets")
    else os.environ.get("DB_URL")
)
if not DB_URL:
    DB_URL = "sqlite:///fee_uplift.db"

engine = create_engine(DB_URL, echo=False, future=True)
SessionLocal = sessionmaker(bind=engine, autoflush=False, autocommit=False)
Base = declarative_base()

# -------------------------
# Enums & core constants
# -------------------------

class SignatureType(str, enum.Enum):
    DOCUSIGN = "DOCUSIGN"
    WET = "WET"
    POSITIVE_CONSENT = "POSITIVE_CONSENT"  # bulk uplift positive consent plan

class CaseStatus(str, enum.Enum):
    DRAFT = "DRAFT"
    READY_TO_SEND = "READY_TO_SEND"  # data has confirmed population & recipients
    SENT_TO_CLIENT = "SENT_TO_CLIENT"  # via DocuSign/wet post
    CLIENT_SIGNED = "CLIENT_SIGNED"
    CLIENT_OPTED_OUT = "CLIENT_OPTED_OUT"
    RECEIVED_PAPERWORK = "RECEIVED_PAPERWORK"  # inbound docs received
    SUBMITTED_TO_PROVIDER = "SUBMITTED_TO_PROVIDER"
    PROVIDER_UPLIFTED = "PROVIDER_UPLIFTED"
    IO_CLOSED_NEW_FEE = "IO_CLOSED_NEW_FEE"  # closed old fee & created new in IO
    COMPLETED = "COMPLETED"

class TaskStatus(str, enum.Enum):
    OPEN = "OPEN"
    DONE = "DONE"
    CANCELLED = "CANCELLED"

# Generic team names — customise to your org lexicon in Admin > Teams
DEFAULT_TEAMS = [
    "Data",
    "Admin Solution",
    "Ops Support",
    "Tech Excellence Center",
    "Submission & Novation",
    "Support Hub",
    "Post",
    "IS",
]

# -------------------------
# ORM Models
# -------------------------

class Team(Base):
    __tablename__ = "teams"
    id = Column(Integer, primary_key=True)
    name = Column(String, unique=True, nullable=False)

class User(Base):
    __tablename__ = "users"
    id = Column(Integer, primary_key=True)
    name = Column(String, nullable=False)
    email = Column(String, unique=True)
    team_id = Column(Integer, ForeignKey("teams.id"))
    role = Column(String, default="member")  # member, manager, admin

    team = relationship("Team")

class Provider(Base):
    __tablename__ = "providers"
    id = Column(Integer, primary_key=True)
    name = Column(String, unique=True, nullable=False)

class Client(Base):
    __tablename__ = "clients"
    id = Column(Integer, primary_key=True)
    name = Column(String, nullable=False)
    io_reference = Column(String)  # e.g., Intelliflo Office ref
    provider_id = Column(Integer, ForeignKey("providers.id"))
    opted_out = Column(Boolean, default=False)

    provider = relationship("Provider")

class Requirement(Base):
    __tablename__ = "requirements"
    id = Column(Integer, primary_key=True)
    provider_id = Column(Integer, ForeignKey("providers.id"))
    name = Column(String, nullable=False)
    description = Column(Text)
    template_path = Column(String)  # location of letter/template if applicable

    provider = relationship("Provider")

class Case(Base):
    __tablename__ = "cases"
    id = Column(Integer, primary_key=True)
    title = Column(String, nullable=False)

    client_id = Column(Integer, ForeignKey("clients.id"), nullable=False)
    provider_id = Column(Integer, ForeignKey("providers.id"), nullable=False)

    signature_type = Column(Enum(SignatureType), nullable=False, default=SignatureType.DOCUSIGN)
    status = Column(Enum(CaseStatus), nullable=False, default=CaseStatus.DRAFT)

    assigned_team_id = Column(Integer, ForeignKey("teams.id"))
    sla_days = Column(Integer, default=10)

    created_at = Column(DateTime, default=datetime.utcnow)
    updated_at = Column(DateTime, default=datetime.utcnow)
    due_at = Column(DateTime)

    client = relationship("Client")
    provider = relationship("Provider")
    assigned_team = relationship("Team")
    tasks = relationship("Task", back_populates="case", cascade="all, delete-orphan")
    documents = relationship("Document", back_populates="case", cascade="all, delete-orphan")
    logs = relationship("AuditLog", back_populates="case", cascade="all, delete-orphan")

class Task(Base):
    __tablename__ = "tasks"
    id = Column(Integer, primary_key=True)
    case_id = Column(Integer, ForeignKey("cases.id"))
    team_id = Column(Integer, ForeignKey("teams.id"))
    title = Column(String, nullable=False)
    status = Column(Enum(TaskStatus), default=TaskStatus.OPEN)
    due_at = Column(DateTime)

    case = relationship("Case", back_populates="tasks")
    team = relationship("Team")

class Document(Base):
    __tablename__ = "documents"
    id = Column(Integer, primary_key=True)
    case_id = Column(Integer, ForeignKey("cases.id"))
    doc_type = Column(String)  # e.g., "unsigned_letter", "signed_letter", "client_reply"
    path = Column(String)
    signed = Column(Boolean, default=False)
    uploaded_to_io = Column(Boolean, default=False)
    external_id = Column(String)  # e.g., DocuSign envelope id

    case = relationship("Case", back_populates="documents")

class AuditLog(Base):
    __tablename__ = "audit_logs"
    id = Column(Integer, primary_key=True)
    case_id = Column(Integer, ForeignKey("cases.id"))
    user_id = Column(Integer, ForeignKey("users.id"), nullable=True)
    action = Column(String, nullable=False)
    notes = Column(Text)
    meta = Column(JSON, default={})
    created_at = Column(DateTime, default=datetime.utcnow)

    case = relationship("Case", back_populates="logs")

@event.listens_for(Case, "before_update")
def _timestamp_before_update(mapper, connection, target):
    target.updated_at = datetime.utcnow()

# -------------------------
# Initial DB bootstrap
# -------------------------

def bootstrap_db(session: Session):
    # Create tables
    Base.metadata.create_all(engine)

    # Seed default teams if empty
    if session.query(Team).count() == 0:
        for t in DEFAULT_TEAMS:
            session.add(Team(name=t))
        session.commit()

# -------------------------
# Simple workflow engine
# -------------------------

# Map each status to the team that typically acts next
NEXT_TEAM_HINT = {
    CaseStatus.DRAFT: "Data",
    CaseStatus.READY_TO_SEND: "Admin Solution",
    CaseStatus.SENT_TO_CLIENT: "Ops Support",
    CaseStatus.CLIENT_SIGNED: "Submission & Novation",
    CaseStatus.CLIENT_OPTED_OUT: "Support Hub",
    CaseStatus.RECEIVED_PAPERWORK: "Submission & Novation",
    CaseStatus.SUBMITTED_TO_PROVIDER: "Tech Excellence Center",
    CaseStatus.PROVIDER_UPLIFTED: "IS",
    CaseStatus.IO_CLOSED_NEW_FEE: "Admin Solution",
    CaseStatus.COMPLETED: None,
}

# Allowed transitions and the UI label that triggers them
TRANSITIONS: Dict[Tuple[CaseStatus, str], Tuple[CaseStatus, str]] = {
    # (from_status, action_label): (to_status, audit_action)
    (CaseStatus.DRAFT, "Mark Ready to Send"): (CaseStatus.READY_TO_SEND, "ready_to_send"),
    (CaseStatus.READY_TO_SEND, "Send to Client"): (CaseStatus.SENT_TO_CLIENT, "sent_to_client"),
    (CaseStatus.SENT_TO_CLIENT, "Mark Client Signed"): (CaseStatus.CLIENT_SIGNED, "client_signed"),
    (CaseStatus.SENT_TO_CLIENT, "Mark Client Opted Out"): (CaseStatus.CLIENT_OPTED_OUT, "client_opted_out"),
    (CaseStatus.CLIENT_SIGNED, "Mark Docs Received"): (CaseStatus.RECEIVED_PAPERWORK, "received_paperwork"),
    (CaseStatus.RECEIVED_PAPERWORK, "Submit to Provider"): (CaseStatus.SUBMITTED_TO_PROVIDER, "submitted_to_provider"),
    (CaseStatus.SUBMITTED_TO_PROVIDER, "Provider Uplifted"): (CaseStatus.PROVIDER_UPLIFTED, "provider_uplifted"),
    (CaseStatus.PROVIDER_UPLIFTED, "Close Fee in IO & Create New"): (CaseStatus.IO_CLOSED_NEW_FEE, "io_closed_new_fee"),
    (CaseStatus.IO_CLOSED_NEW_FEE, "Complete Case"): (CaseStatus.COMPLETED, "completed"),
}

# -------------------------
# Repository helpers
# -------------------------

def get_session() -> Session:
    return SessionLocal()

# Utility to assign the next team automatically based on status

def assign_next_team(session: Session, case: Case):
    hint = NEXT_TEAM_HINT.get(case.status)
    if not hint:
        case.assigned_team = None
        return
    team = session.query(Team).filter(Team.name == hint).first()
    case.assigned_team = team

# Create a task for the owning team

def create_task_for_case(session: Session, case: Case, title: str, due_days: int = 5):
    if case.assigned_team is None:
        return
    task = Task(
        case=case,
        team=case.assigned_team,
        title=title,
        status=TaskStatus.OPEN,
        due_at=datetime.utcnow() + timedelta(days=due_days),
    )
    session.add(task)

# Record an audit log

def log_action(session: Session, case: Case, user: Optional[User], action: str, notes: str = "", meta: dict | None = None):
    session.add(
        AuditLog(case=case, user_id=user.id if user else None, action=action, notes=notes, meta=meta or {})
    )

# Apply a transition

def apply_transition(session: Session, case: Case, user: Optional[User], action_label: str, notes: str = "") -> bool:
    key = (case.status, action_label)
    if key not in TRANSITIONS:
        return False
    new_status, audit_action = TRANSITIONS[key]
    case.status = new_status

    # SLA: set/update due date
    case.due_at = datetime.utcnow() + timedelta(days=case.sla_days)

    assign_next_team(session, case)
    create_task_for_case(session, case, title=f"{new_status} — next step")
    log_action(session, case, user, audit_action, notes)
    return True

# -------------------------
# Streamlit UI
# -------------------------

st.set_page_config(page_title="Workflow Tracker — Fee Uplift", layout="wide")

# Sidebar: pseudo-auth (replace with your SSO)
st.sidebar.header("Who are you?")
if "user" not in st.session_state:
    st.session_state.user = None

with get_session() as session:
    bootstrap_db(session)

    # Build user/team pickers for demo purposes
    teams = session.query(Team).order_by(Team.name).all()
    team_names = [t.name for t in teams]

    name = st.sidebar.text_input("Your name", value=st.session_state.get("name", ""))
    email = st.sidebar.text_input("Your email (optional)", value=st.session_state.get("email", ""))
    team_choice = st.sidebar.selectbox("Your team", team_names)
    role_choice = st.sidebar.selectbox("Your role", ["member", "manager", "admin"])

    if st.sidebar.button("Sign in / Switch"):
        # Create/find user
        team_obj = session.query(Team).filter(Team.name == team_choice).first()
        user = session.query(User).filter(User.email == email).first() if email else None
        if not user:
            user = User(name=name or "Anonymous", email=email or None, team=team_obj, role=role_choice)
            session.add(user)
            session.commit()
        else:
            user.name = name or user.name
            user.team = team_obj
            user.role = role_choice
            session.commit()
        st.session_state.user = {"id": user.id, "name": user.name, "team": team_obj.name, "role": user.role}
        st.session_state.name = user.name
        st.session_state.email = user.email or ""
        st.success(f"Signed in as {user.name} ({team_obj.name})")

# Helper to get current user object

def current_user(session: Session) -> Optional[User]:
    u = st.session_state.get("user")
    if not u:
        return None
    return session.query(User).get(u["id"])  # type: ignore

# Top-level nav
PAGES = {
    "Dashboard": "dashboard",
    "My Queue": "queue",
    "Cases": "cases",
    "Case Detail": "case_detail",
    "Admin": "admin",
}

page = st.sidebar.radio("Navigation", list(PAGES.keys()))

# --------------- Dashboard ---------------

def page_dashboard(session: Session):
    st.title("📊 Workflow Dashboard")

    # KPIs
    total_cases = session.query(Case).count()
    open_cases = session.query(Case).filter(Case.status != CaseStatus.COMPLETED).count()
    overdue = session.query(Case).filter(Case.due_at != None, Case.due_at < datetime.utcnow(), Case.status != CaseStatus.COMPLETED).count()

    c1, c2, c3 = st.columns(3)
    c1.metric("Total cases", total_cases)
    c2.metric("Open cases", open_cases)
    c3.metric("Overdue", overdue)

    st.subheader("Status breakdown")
    rows = (
        session.query(Case.status, Team.name, Provider.name)
        .join(Team, Case.assigned_team_id == Team.id, isouter=True)
        .join(Provider, Case.provider_id == Provider.id, isouter=True)
        .all()
    )
    df = pd.DataFrame(rows, columns=["status", "assigned_team", "provider"])
    if not df.empty:
        st.dataframe(df.value_counts(["status", "assigned_team"]).reset_index(name="count"))
    else:
        st.info("No cases yet. Create some in the Cases page.")

# --------------- Queue ---------------

def page_queue(session: Session):
    st.title("🧾 My Queue")
    user = current_user(session)
    if not user:
        st.warning("Sign in (left sidebar) to see your team queue.")
        return

    st.write(f"Team: **{user.team.name}**")

    q = (
        session.query(Case)
        .join(Team, Case.assigned_team_id == Team.id)
        .filter(Team.id == user.team_id)
        .filter(Case.status != CaseStatus.COMPLETED)
        .order_by(Case.due_at.is_(None), Case.due_at)
    )

    data = [
        {
            "Case ID": c.id,
            "Title": c.title,
            "Client": c.client.name,
            "Provider": c.provider.name,
            "Signature": c.signature_type.value,
            "Status": c.status.value,
            "Due": c.due_at.strftime("%Y-%m-%d") if c.due_at else "",
        }
        for c in q
    ]
    df = pd.DataFrame(data)
    st.dataframe(df, use_container_width=True)

# --------------- Cases ---------------

def page_cases(session: Session):
    st.title("📁 Cases")

    with st.expander("➕ New Case"):
        title = st.text_input("Title", value="Fee uplift — client letter")
        client_name = st.text_input("Client name")
        provider_name = st.text_input("Provider")
        signature_type = st.selectbox("Signature type", [e.value for e in SignatureType])
        sla_days = st.number_input("SLA days", value=10, min_value=1)
        if st.button("Create Case"):
            with SessionLocal() as s:
                # ensure provider
                provider = s.query(Provider).filter(Provider.name == provider_name).first()
                if not provider:
                    provider = Provider(name=provider_name)
                    s.add(provider)
                    s.commit()
                # ensure client
                client = s.query(Client).filter(Client.name == client_name).first()
                if not client:
                    client = Client(name=client_name, provider=provider)
                    s.add(client)
                    s.commit()
                case = Case(
                    title=title,
                    client=client,
                    provider=provider,
                    signature_type=SignatureType(signature_type),
                    status=CaseStatus.DRAFT,
                    sla_days=int(sla_days),
                )
                assign_next_team(s, case)
                s.add(case)
                s.commit()
                log_action(s, case, current_user(s), action="created", notes="Case created")
                s.commit()
                st.success(f"Created case #{case.id}")

    # List cases
    st.subheader("All Cases")
    cases = session.query(Case).order_by(Case.created_at.desc()).all()
    data = [
        {
            "Case ID": c.id,
            "Title": c.title,
            "Client": c.client.name,
            "Provider": c.provider.name,
            "Signature": c.signature_type.value,
            "Status": c.status.value,
            "Assigned Team": c.assigned_team.name if c.assigned_team else "",
            "Created": c.created_at.strftime("%Y-%m-%d"),
        }
        for c in cases
    ]
    st.dataframe(pd.DataFrame(data), use_container_width=True)

    st.info("Open a specific case in the 'Case Detail' page using its ID.")

# --------------- Case Detail ---------------

def page_case_detail(session: Session):
    st.title("🔍 Case Detail")
    case_id = st.number_input("Enter Case ID", min_value=1, step=1)
    if st.button("Load Case"):
        st.session_state["active_case_id"] = int(case_id)

    active_id = st.session_state.get("active_case_id")
    if not active_id:
        st.stop()

    case = session.query(Case).get(active_id)
    if not case:
        st.error("Case not found")
        st.stop()

    c1, c2, c3, c4 = st.columns([2,2,2,2])
    c1.write(f"**Title:** {case.title}")
    c1.write(f"**Client:** {case.client.name}")
    c2.write(f"**Provider:** {case.provider.name}")
    c2.write(f"**Signature:** {case.signature_type.value}")
    c3.write(f"**Status:** {case.status.value}")
    c3.write(f"**Assigned Team:** {case.assigned_team.name if case.assigned_team else '-'}")
    c4.write(f"**SLA (days):** {case.sla_days}")
    c4.write(f"**Due:** {case.due_at.strftime('%Y-%m-%d') if case.due_at else '-'}")

    st.markdown("---")
    st.subheader("Next Actions")
    user = current_user(session)

    # Show valid transitions for current status
    available = [label for (st_from, label), _ in TRANSITIONS.items() if st_from == case.status]
    if not available:
        st.info("No further actions from this status.")
    else:
        cols = st.columns(len(available))
        for i, label in enumerate(available):
            if cols[i].button(label):
                notes = st.text_input("Optional notes for audit", key=f"notes_{label}")
                if apply_transition(session, case, user, label, notes=notes):
                    session.commit()
                    st.success(f"Moved to {case.status.value}")
                    st.experimental_rerun()
                else:
                    st.error("Transition not allowed")

    st.markdown("---")

    st.subheader("Documents")
    uploaded = st.file_uploader("Attach a document", type=["pdf", "docx", "png", "jpg"], accept_multiple_files=True)
    if uploaded:
        for f in uploaded:
            save_path = os.path.join("uploads", f"case_{case.id}")
            os.makedirs(save_path, exist_ok=True)
            file_path = os.path.join(save_path, f.name)
            with open(file_path, "wb") as out:
                out.write(f.getbuffer())
            doc = Document(case=case, doc_type="uploaded", path=file_path)
            session.add(doc)
        session.commit()
        st.success("Uploaded.")

    docs = (
        session.query(Document)
        .filter(Document.case_id == case.id)
        .all()
    )
    if docs:
        df = pd.DataFrame([
            {"id": d.id, "type": d.doc_type, "path": d.path, "signed": d.signed, "uploaded_to_io": d.uploaded_to_io}
            for d in docs
        ])
        st.dataframe(df, use_container_width=True)
    else:
        st.caption("No documents yet.")

    st.subheader("Audit Log")
    logs = session.query(AuditLog).filter(AuditLog.case_id == case.id).order_by(AuditLog.created_at.desc()).all()
    if logs:
        df = pd.DataFrame([
            {
                "when": l.created_at.strftime("%Y-%m-%d %H:%M"),
                "action": l.action,
                "user": l.user_id,
                "notes": l.notes,
                "meta": json.dumps(l.meta) if l.meta else "",
            }
            for l in logs
        ])
        st.dataframe(df, use_container_width=True)
    else:
        st.caption("No activity yet.")

# --------------- Admin ---------------

def page_admin(session: Session):
    st.title("⚙️ Admin")

    st.subheader("Teams")
    with st.form("add_team"):
        tname = st.text_input("Team name")
        if st.form_submit_button("Add team"):
            if tname:
                session.add(Team(name=tname))
                session.commit()
                st.success("Team added")

    st.subheader("Providers")
    with st.form("add_provider"):
        pname = st.text_input("Provider name")
        if st.form_submit_button("Add provider"):
            if pname:
                session.add(Provider(name=pname))
                session.commit()
                st.success("Provider added")

    st.subheader("Requirements (by provider)")
    with st.form("add_req"):
        provider_options = {p.name: p.id for p in session.query(Provider).all()}
        prov = st.selectbox("Provider", list(provider_options.keys()))
        rname = st.text_input("Requirement name")
        rdesc = st.text_area("Description")
        rpath = st.text_input("Template path (optional)")
        if st.form_submit_button("Add requirement"):
            req = Requirement(provider_id=provider_options[prov], name=rname, description=rdesc, template_path=rpath)
            session.add(req)
            session.commit()
            st.success("Requirement added")

# --------------- Router ---------------

with get_session() as session:
    if page == "Dashboard":
        page_dashboard(session)
    elif page == "My Queue":
        page_queue(session)
    elif page == "Cases":
        page_cases(session)
    elif page == "Case Detail":
        page_case_detail(session)
    elif page == "Admin":
        page_admin(session)
