import csv
import numpy as np
import sys
import os
import json
import datetime

from jinja2 import Template
from send_mail import SendMail

from make_stats import make_stats

snr=10  # limit snr
snrQ=5  #limit for worse snr

lastweek=False

make_stats()

if not os.path.isfile('db/objects.csv'):
    print('NO object DB!')
    sys.exit()

if not os.path.isfile('db/observations.json'):
    print('NO observations DB!')
    sys.exit()

objects=[]

f=open('db/objects.csv','r')
reader = csv.DictReader(f)
for obj in reader:
    if obj['Done']=='0': objects.append(obj)    #load object and select only obj. for observations
f.close()

f=open('db/observations.json','r')
obs0=json.load(f)     # load observations
f.close()
all_obs={x.lower().replace('-','').replace(' ',''): obs0[x] for x in obs0}

f=open('db/all_names.json','r')
names=json.load(f)     # load used names
f.close()

f=open('db/progID.json','r')
progs=json.load(f)
f.close()


goods=[]
faints=[]

goodProg={}
faintProg={}

for obj in objects:
    name=obj['Target'].lower().replace(' ', '').replace('-','').replace('_','')
    if obj['Type'] in ['RV Standard','SpecPhot Standard']: continue
    if len(obj['Nights'])==0: continue  #not given
    nights=int(obj['Nights'])
    exp0=float(obj['ExpTime'])
    if name not in all_obs: continue   #no observations

    if len(all_obs[name])<nights: continue  #observations less than requested

    if lastweek:
        last=datetime.datetime.strptime(all_obs[name][-1],'%Y-%m-%d')
        if datetime.datetime.now()-last>datetime.timedelta(days=8):
            continue    #old object -> ignore

    good=[]
    snrs=[]
    quest=[]
    for obs in all_obs[name]:
        f=open('static/logs/'+obs+'_log.csv','r')
        reader = csv.DictReader(f)
        for row in reader:
            #utilize similar objects names - spaces, lower/upper case etc.
            if name==row['object'].replace('?','').replace('ttarget-','').replace('ttarget_','').replace('-thar','').replace('_thar','').replace('_',' ').strip().lower().replace('-','').replace(' ',''):
                exp=row['exposure']
                #inst=row['instrument']
                if 'snr' in row:
                    if len(row['snr'])>0:
                        snrs.append(float(row['snr']))
                        if float(row['snr'])>snr:
                            if obs not in good: good.append(obs)
                            #break
                        elif float(row['snr'])>snrQ:
                            if obs not in quest: quest.append(obs)
                    else: break
                else: break
        f.close()

    if len(snrs)==0: continue  #no usable spectra

    if len(good)>=nights:
        tmp={'name':obj['Target'],'nights':nights,'observed':len(good),'snr':round(np.mean(snrs),1), 'progID':obj['ProgramID']}
        goods.append(tmp)
        if obj['ProgramID'] in goodProg: goodProg[obj['ProgramID']].append(tmp)
        else: goodProg[obj['ProgramID']]=[tmp]
            
    elif len(quest)+len(good)>=nights:
        tmp={'name':obj['Target'],'nights':nights,'observed':len(quest)+len(good),'good':len(good),'faint':len(quest),'snr':round(np.mean(snrs),1), 'progID':obj['ProgramID']}
        faints.append(tmp)
        if obj['ProgramID'] in faintProg: faintProg[obj['ProgramID']].append(tmp)
        else: faintProg[obj['ProgramID']]=[tmp]

# print('Good spectra:')
# for x in sorted(goods,key=(lambda x: x['name'].lower().replace(' ', '').replace('-','').replace('_','') )):
#     print(x['name']+':',x['observed'],'/',x['nights'],'snr:',x['snr'])
# print()

# print('Faint spectra:')
# for x in sorted(faints,key=(lambda x: x['name'].lower().replace(' ', '').replace('-','').replace('_','') )):
#     print(x['name']+':',x['good'],'+',x['faint'],'/',x['nights'],'snr:',x['snr'])
# print()
# print()

# print('Good spectra (web SNR>10 -> ceres+ SNR >~ 40):')
# for pr in goodProg:
#     if pr in progs: 
#         print(progs[pr]['program_title'],'(',progs[pr]['name'],')')
#     else: print('NOT GIVEN')
#     good=goodProg[pr]
#     for x in sorted(good,key=(lambda x: x['name'].lower().replace(' ', '').replace('-','').replace('_','') )):
#         print(x['name']+':',x['observed'],'/',x['nights'],'snr:',x['snr'])
#     print()
# print()

f=open('templates/message_good')
templateGood = Template(f.read())
f.close()

for pr in goodProg:
    if pr not in progs: continue
    good=goodProg[pr]
       
    #create text from template    
    mess=templateGood.render(program=progs[pr]['program_title'],progID=progs[pr]['code'],objects=sorted(good,key=(lambda x: x['name'].lower().replace(' ', '').replace('-','').replace('_','') )))
    
    send=SendMail(progs[pr]['mail'])
    
    cc=send.mail['cc']
    send.mail['cc']=send.mail['to']
    send.mail['to']=cc
    
    send.mail['subject']='Status of your targets'
    
    send.message=mess
    
    #send.check()
    send.run()



   
# print('Faint spectra (web SNR>5 -> ceres+ SNR ~ 10-30):')
# for pr in faintProg:
#     if pr in progs: 
#         print(progs[pr]['program_title'],'(',progs[pr]['name'],')')
#     else: print('NOT GIVEN')
#     faint=faintProg[pr]
#     for x in sorted(faint,key=(lambda x: x['name'].lower().replace(' ', '').replace('-','').replace('_','') )):
#         print(x['name']+':',x['good'],'+',x['faint'],'/',x['nights'],'snr:',x['snr'])
#     print()
# print()
# print()
        
f=open('templates/message_faint')
templateFaint = Template(f.read())
f.close()

for pr in faintProg:
    if pr not in progs: continue
    faint=faintProg[pr]
       
    #create text from template    
    mess=templateFaint.render(program=progs[pr]['program_title'],progID=progs[pr]['code'],objects=sorted(faint,key=(lambda x: x['name'].lower().replace(' ', '').replace('-','').replace('_','') )))
    
    send=SendMail(progs[pr]['mail'])
    
    cc=send.mail['cc']
    send.mail['cc']=send.mail['to']
    send.mail['to']=cc
    
    send.mail['subject']='Status of your targets'
    
    send.message=mess
    
    #send.check()
    send.run()
    
