import time, json
from sqlalchemy import select, func
from src.config.database import Session
from migrations.initial import migrate
from src.models import User, Dataset, Job, Report, Delivery, Audit, SilencePlan, SilenceReplay, SilenceBrief
from src.services.business import metadata, seed_records, analyze
from src.services import silence as silence_svc
from src.services import silence_engine
from src.schemas.silence import SilencePlanInput, EdgeInput, SilenceBriefInput
from src.services.reports import deliver
from src.utils.security import hash_password
from src.utils.serialization import checksum
def seed():
    migrate()
    with Session() as db:
        for role,name in [('admin','林知 · 业务负责人'),('analyst','陈舟 · 分析员'),('viewer','周禾 · 观察员')]:
            email=role+'@demo.local'
            if not db.scalar(select(User).where(User.email==email)):
                db.add(User(email=email,name=name,role=role,password=hash_password('Demo123!')))
        db.flush()
        m=metadata()
        d=db.scalar(select(Dataset).where(Dataset.name==m['dataset_name']))
        if not d:
            rows=seed_records()
            d=Dataset(name=m['dataset_name'],source=m['short']+'-202611.csv',records=rows,checksum=checksum(rows),created_at=time.time())
            db.add(d); db.flush()
        if not db.scalar(select(Job).where(Job.name=='基线复核 · '+m['name'])):
            params={p['key']:p['default'] for p in m.get('parameters',[])}
            result=analyze(d.records,params)
            json.dumps(result,ensure_ascii=False,allow_nan=False)
            admin=db.scalar(select(User).where(User.role=='admin'))
            j=Job(name='基线复核 · '+m['name'],dataset_id=d.id,params=params,status='completed',result=result,created_by=admin.id,created_at=time.time()-3600,finished_at=time.time()-3595,approved=True)
            db.add(j);db.flush()
            r=Report(name='每日业务复核简报',job_id=j.id,destination='业务复核组 · 本地收件箱',interval_seconds=86400,next_run=time.time()+86400,enabled=True)
            db.add(r);db.flush();deliver(db,r)
            db.add(Audit(actor='seed',action='demo.baseline',detail={'dataset':d.id},at=time.time()))
        if not db.scalar(select(SilencePlan).where(SilencePlan.name=='值班静默 · 五服务批次回放')):
            admin=db.scalar(select(User).where(User.role=='admin'))
            edges=[EdgeInput(upstream='cache',downstream='api-gateway'),
                   EdgeInput(upstream='api-gateway',downstream='job-queue')]
            plan_input=SilencePlanInput(name='值班静默 · 五服务批次回放',dataset_id=d.id,edges=edges,
                                        direction='upstream',window_min=5,severe_levels=[1],segment_size=50)
            plan=silence_svc.create_plan(db,plan_input,admin)
            replay=silence_svc.start_replay(db,plan.id,admin)
            while replay.status!='completed':
                replay=silence_svc.advance_replay(db,replay.id)
            # 演示批次含各服务重度/窗外事件，满足“无服务全静默”闸门才获批。
            replay,plan=silence_svc.approve_replay(db,replay.id,admin)
            brief=silence_svc.create_brief(db,plan.id,SilenceBriefInput(name='值班静默简报 · 基线期'),admin)
            db.add(Audit(actor='seed',action='demo.silence_baseline',
                         detail={'plan':plan.id,'replay':replay.id,'brief':brief.id},at=time.time()))
        db.commit()
        counts={model.__tablename__:db.scalar(select(func.count()).select_from(model)) for model in [User,Dataset,Job,Report,Delivery,Audit]}
        print(json.dumps(counts,ensure_ascii=False))
        return counts
if __name__=='__main__':seed()
