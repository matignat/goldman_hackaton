from ..models import Audit,Activity,now

def audit(db,action,team_id=None,ticket_id=None,actor=None,detail=None):
    db.add(Audit(actor_id=getattr(actor,'id',None),actor=getattr(actor,'name','system'),
                 action=action,team_id=team_id,ticket_id=ticket_id,detail=detail or {}))

def activity(db,team_id,ticket_id,kind,label,coalesce=False,count=1):
    if coalesce:
        rec=db.query(Activity).filter_by(team_id=team_id,ticket_id=ticket_id,kind=kind).order_by(Activity.id.desc()).first()
        if rec:
            rec.count=count;rec.updated_at=now();rec.label=label
            return
    db.add(Activity(team_id=team_id,ticket_id=ticket_id,kind=kind,label=label,count=count,updated_at=now()))
