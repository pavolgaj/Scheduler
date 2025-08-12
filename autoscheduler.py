import os
import json
import pandas as pd
import numpy as np
from datetime import datetime,timezone
import uuid

import matplotlib
matplotlib.use('Agg')

from scheduler import *
from make_stats import make_stats

date=datetime.now(timezone.utc).strftime('%Y-%m-%d')

use_condi=['good','poor','na']
use_freq=['everynight', 'twiceweek', 'onceweek', 'twicemonth', 'oncemonth', 'unspecified']
prior=True #rescale priorities
series = True

name='autoschedule'

if not os.path.isfile('db/objects.csv'):
    raise "NO object DB"
objects0=load_objects('db/objects.csv',check=False)   #check in Simbad?

use_group=[]
use_program=[]

f=open('db/progID.json','r')
ids=json.load(f)
f.close()

for obj in objects0:
    if obj['full']['Done']==1: continue  #remove already finished targets

    group=obj['full']['Type']
    if pd.isna(group): group='None'
    if group not in use_group: use_group.append(group)

    #add program name
    progID=obj['full']['ProgramID']
    if pd.isna(progID): progID=''
    if progID not in use_program:
        if ids[str(progID)]['mode'].split()[0]=='service:': use_program.append(progID)


#load last observations
make_stats()
stats={}
if os.path.isfile('db/statistics.csv') and prior:
    f=open('db/statistics.csv','r')
    lines=f.readlines()
    f.close()
    for l in lines[1:]:
        tmp=l.strip().split(',')
        target=tmp[0]
        last=datetime.strptime(tmp[4],'%Y-%m-%d')
        #utilize similar objects names - spaces, lower/upper case etc.
        tr=target.lower().replace('-','').replace(' ','').replace('+','').replace('.','').replace('_','')
        if tr in stats:
            if last>stats[tr]: stats[tr]=last
        else: stats[tr]=last


#add selected objects by types and series
objects1={}
for obj in objects0:
    if obj['full']['Done']==1: continue
    if not series and obj['n_exp']=='series': continue
    group=obj['full']['Type']
    if pd.isna(group): group='None'
    condi=obj['full']['Conditions']
    if pd.isna(condi): condi=''
    fr=obj['full']['Frequency']
    if pd.isna(fr): fr='unspecified'

    #add program name
    progID=obj['full']['ProgramID']
    if pd.isna(progID): progID=''

    plandate=datetime.strptime(date,'%Y-%m-%d')

    if group in use_group and progID in use_program:
        if (condi in use_condi and fr in use_freq) or group in ['RV Standard','SpecPhot Standard']:
            #ignore conditons for standards

            #update priority based on last obs.
            tr=obj['full']['Target'].lower().replace('-','').replace(' ','').replace('+','').replace('.','').replace('_','')

            if tr in stats and group not in ['RV Standard','SpecPhot Standard']:
                last=stats[tr]
                if plandate>last:
                    #only for future planning
                    diffdate=(plandate-last).days

                    #specify interval for obs.
                    if fr=='everynight':
                        obsint=1
                        k=0.2
                    elif fr=='twiceweek':
                        obsint=3
                        k=0.1
                    elif fr=='onceweek': obsint=7
                    elif fr=='twicemonth': obsint=14
                    elif fr=='oncemonth': obsint=30
                    elif fr=='unspecified': obsint=10

                    #modification of priority
                    if obsint<5:
                        dprior=10-20/(1+np.exp(-k*(diffdate-obsint)))
                    else:
                        dprior=20*(1-np.exp(-(diffdate-obsint)**2/obsint**2))
                        if obsint<diffdate: dprior*=-0.8
                    if fr=='unspecified' and dprior<0: dprior=0

                    obj['priority']+=dprior

                    #always priority>1
                    if fr=='everynight': obj['priority']=max(1.1,obj['priority'])
                    elif fr=='twiceweek': obj['priority']=max(1.2,obj['priority'])
                    elif fr=='onceweek': obj['priority']=max(1.3,obj['priority'])
                    elif fr=='twicemonth': obj['priority']=max(1.4,obj['priority'])
                    elif fr=='oncemonth': obj['priority']=max(1.5,obj['priority'])

                    obj['priority']=round(obj['priority'],1)


            mag=obj['full']['Mag']

            try: mag=float(mag)
            except ValueError:
                objects1[str(uuid.uuid4())]=obj
                continue

            objects1[str(uuid.uuid4())]=obj

#load config - based on observatory!
config=load_config('lasilla_config.txt')

observatory=config['observatory']

read_out = config['read_out']     #read_out time of camera + comp (with readout) + ...
slew_rate = config['slew_rate']   #slew rate of the telescope

scheduler=config['scheduler']

#set used scheduler: SequentialScheduler / PriorityScheduler -> select on web
if scheduler=='Sequential': Scheduler=SequentialScheduler
elif scheduler=='Priority': Scheduler=PriorityScheduler
elif scheduler=='StdPriority': Scheduler=StdPriorityScheduler

#general constraints
constraints0 = [ModifAltitudeConstraint(config['minAlt'],config['maxAlt'],boolean_constraint=True),
            ModifAirmassConstraint(config['airmass'],boolean_constraint=False),AtNightConstraint.twilight_nautical(), MoonSeparationConstraint(config['moon'])]

#load telescope restrictions and set constraint
limE,limW=load_limits()
constraints0.append(LimitConstraint(limE,limW))

plantime=Time(date+' '+str(12-int(round(observatory.longitude.value/15))).rjust(2,'0')+':00:00')    #approx. local noon (in UTC)

#calculate sunset, sunrise and midnight times + set some time ranges

#sunrise/sunset calculation with 1 hour extend -> for scheduling intervals and plots
#could be modified for speed up -> add horizon=-12*u.deg (nautical twilight) or -18 (astronomical) and remove additional hour.
midnight=observatory.midnight(plantime,n_grid_points=10, which='next')
suns=observatory.sun_set_time(midnight,n_grid_points=10, which='nearest')-1*u.hour
sunr=observatory.sun_rise_time(midnight,n_grid_points=10, which='nearest')+1*u.hour

night=sunr-suns
obstime=suns+night*np.linspace(0, 1, 100)    #range of observing scheduling

constraints=list(constraints0)

# Prefiltering
objects=prefilter(objects1,constraints,observatory,obstime)

# presort needed mainly for Seq. scheduler
if scheduler=='Sequential':
    objects=presort(objects, observatory, midnight,key='meridian')     #meridian/set/rise


#fill blocks
blocks=[]
names={}
for obj in objects:
    cons=[]
    if not pd.isna(obj['full']['StartDate']) or not pd.isna(obj['full']['EndDate']):
        #constraint on obs date
        try: start=Time(obj['full']['StartDate'])
        except: start=None
        try: end=Time(obj['full']['EndDate'])
        except: end=None
        cons.append(TimeConstraint(start,end))
    if not pd.isna(obj['full']['MoonPhase']):
        #constraint on Moon phase
        cons.append(MoonIlluminationConstraint(0,obj['full']['MoonPhase']))
        #TODO remove later?
    if not pd.isna(obj['full']['StartPhase']) or not pd.isna(obj['full']['EndPhase']):
        #phase constraint for EB or exoplanets
        objPer=PeriodicEvent(epoch=Time(obj['full']['Epoch'],format='jd'),period=float(obj['full']['Period'])*u.day)
        if pd.isna(obj['full']['StartPhase']): start=None
        else: start=obj['full']['StartPhase']
        if pd.isna(obj['full']['EndPhase']): end=None
        else: end=obj['full']['EndPhase']
        cons.append(PhaseConstraint(objPer,start,end))

    if obj['n_exp']=='series':
        #series -> 20 blocks with 5 exp.
        for i in range(20):
            if obj['target'].name in names:  #repeating objects -> NOT replace debug plots
                names[obj['target'].name]+=1
                name1=obj['target'].name+'_s'+str(names[obj['target'].name])
            else:
                names[obj['target'].name]=0
                name1=obj['target'].name
            blocks.append(ObservingBlock.from_exposures(FixedTarget(name=name1, coord=obj['target'].coord),obj['priority'],obj['exp']*u.second,5,read_out,constraints=cons))
    else:
        if obj['target'].name in names:  #repeating objects -> NOT replace debug plots
            names[obj['target'].name]+=1
            name1=obj['target'].name+'_s'+str(names[obj['target'].name])
        else:
            names[obj['target'].name]=0
            name1=obj['target'].name
        blocks.append(ObservingBlock.from_exposures(FixedTarget(name=name1, coord=obj['target'].coord),obj['priority'],obj['exp']*u.second,obj['n_exp'],read_out,constraints=cons))

if len(blocks)==0:
    raise 'Schedule is EMPTY!'

#run scheduler
constraintsM=[]
for c in constraints:
    if not 'MoonSep' in c.__class__.__name__ or 'Modif' in c.__class__.__name__:
        constraintsM.append(c)    #NOT MoonSeparation constraint (already prefiltered) -> speed up

transitioner = Transitioner(slew_rate)
scheduler = Scheduler(constraints = constraintsM,observer = observatory,transitioner = transitioner)
schedule = Schedule(suns,sunr)     #start and end of scheduling interval
scheduler(blocks, schedule)

#calculate positions
check_limits(schedule)

#save schedules...
out=name+'_'+plantime.strftime('%Y-%m-%d')

tab=schedule_table(schedule,objects1)
df=tab[~(tab['target']=='TransitionBlock')].to_pandas()

cols={'target':'Target', 'ra':'RA', 'dec':'DEC', 'mag':'Mag','exposure (seconds)':'ExpTime', 'number exposures':'Number','_Remarks':'Remarks', 'start time (UTC)':'Start', 'end time (UTC)':'End','altitude':'Altitude', 'airmass':'Airmass', 'azimut':'Azimut','altitude-start':'AltitudeStart', 'airmass-start':'AirmassStart', 'azimut-start':'AzimutStart','altitude-end':'AltitudeEnd', 'airmass-end':'AirmassEnd', 'azimut-end':'AzimutEnd','position':'Position','priority':'Priority','moon-separation':'MoonSeparation'}

df=df.rename(columns=cols)
df.to_csv('schedules/'+out+'.csv',index=False)

#remove old figs
if os.path.isfile('schedules/'+out+'_alt.png'):
    os.remove('schedules/'+out+'_alt.png')
    os.remove('schedules/'+out+'_sky.png')

