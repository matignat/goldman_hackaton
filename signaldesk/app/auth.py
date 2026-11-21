import os
from datetime import datetime, timezone
from fastapi import Depends, HTTPException, Request
from itsdangerous import URLSafeTimedSerializer, BadSignature, SignatureExpired
from sqlalchemy.orm import Session
from .database import Session as DBSession
from .models import User, Team, Membership, BoardAccess, Ticket

SIGNER = URLSafeTimedSerializer(os.getenv('SECRET_KEY','dev-secret-for-local-demo-only'),salt='signaldesk-demo-session')

def session_user(request: Request):
    token=request.cookies.get('signaldesk_session')
    if not token: raise HTTPException(401,'Select a demo persona to sign in')
    try: return int(SIGNER.loads(token,max_age=60*60*24))
    except (BadSignature,SignatureExpired,ValueError): raise HTTPException(401,'Session expired')

def db_session():
    db=DBSession()
    try: yield db
    finally: db.close()

def current_user(uid:int=Depends(session_user),db:Session=Depends(db_session)):
    user=db.get(User,uid)
    if not user: raise HTTPException(401,'Unknown session user')
    return user

def require_role(user,role):
    if user.role!=role: raise HTTPException(403,f'Requires {role} role')

def require_debug(user):
    if not user.debug_enabled or os.getenv('DEMO_MODE','1')!='1': raise HTTPException(403,'Demo/Debug capability required')

def is_manager(db, user, team_id):
    team=db.get(Team,team_id)
    return bool(team and team.leader_id==user.id and user.role=='M')

def is_member(db,user,team_id):
    return db.get(Membership,(user.id,team_id)) is not None

def is_maintainer(db,user,team_id):
    m=db.get(Membership,(user.id,team_id))
    return bool(m and m.maintainer)

def is_guest(db,user,team_id):
    return bool(db.query(BoardAccess).filter_by(team_id=team_id,user_id=user.id,status='approved').first())

def can_view_board(db,user,team_id):
    return is_member(db,user,team_id) or is_manager(db,user,team_id) or is_guest(db,user,team_id)

def can_view_ticket(db,user,ticket):
    if can_view_board(db,user,ticket.team_id):
        if ticket.state!='archived': return True
        # Archived item access is intentionally narrower for employees.
        if is_manager(db,user,ticket.team_id):return True
        return user.id in (ticket.members or []) or user.id in (ticket.done_by,ticket.approved_by)
    return user.id in (ticket.members or []) or user.id in (ticket.done_by,ticket.approved_by)

def can_work(db,user,ticket):
    return is_manager(db,user,ticket.team_id) or is_maintainer(db,user,ticket.team_id) or user.id in (ticket.members or [])

def can_approve_final(db,user,ticket):
    return is_manager(db,user,ticket.team_id) or is_maintainer(db,user,ticket.team_id)

def require(condition,detail='Permission denied'):
    if not condition:raise HTTPException(403,detail)
