#import uuid
import secrets
import csv
import string

old='db/objects.csv'
new='db/objects-id.csv'

header='Target,RA,DEC,Mag,Period,Epoch,ExpTime,Number,Nights,Priority,Type,Remarks,MoonPhase,StartPhase,EndPhase,StartDate,EndDate,Conditions,Frequency,OtherRequests,Supervisor,ObjectID,ProgramID,Done'

used=[]

f=open(old,'r')
out=open(new,'w')

def generate_id() -> str:
    return ''.join(secrets.choice(string.ascii_letters + string.digits) for _ in range(12))
                
reader = csv.DictReader(f)
writer=csv.DictWriter(out,fieldnames=header.strip().split(','))   
writer.writeheader()

for i,obj in enumerate(reader):
    objid=generate_id()
    while objid in used: objid=generate_id()
    obj['ObjectID']=objid
    used.append(objid)
    writer.writerow(obj)
    
f.close()
out.close()
    

