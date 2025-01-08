from scheduler import *

import json
import time
import sys
import uuid

if len(sys.argv)<3:
    print('USAGE: python3 run_scheduler.py "config_file" "object_list" (date: YYYY-MM-DD) (number_nights)')
    input()
    sys.exit()

configFile=sys.argv[1]
objlist=sys.argv[2]

if len(sys.argv)>3 and '-' in sys.argv[3]:
    date=sys.argv[3]
    del(sys.argv[3])
else: date=datetime.utcnow().strftime('%Y-%m-%d')

if len(sys.argv)>3: nights=int(sys.argv[3])
else: nights=1

config=load_config(configFile)
if config is None:
    input()
    sys.exit()

observatory=config['observatory']

constraints = [ModifAltitudeConstraint(config['minAlt'],config['maxAlt'],boolean_constraint=False), AirmassConstraint(config['airmass'],boolean_constraint=True),
               AtNightConstraint.twilight_nautical(), MoonSeparationConstraint(config['moon'])]
#,TimeConstraint(Time('2024-08-07 06:00'), Time('2024-08-07 10:00'))
#,AzimutConstraint(250*u.deg,180*u.deg),LimitConstraint(limE,limW)
if observatory.name=='lasilla':
    limE,limW=load_limits()
    constraints.append(LimitConstraint(limE,limW))

read_out = config['read_out']     #read_out time of camera + comp (with readout) + ...
slew_rate = config['slew_rate']   #slew rate of the telescope

#set used scheduler: SequentialScheduler / PriorityScheduler
if config['scheduler']=='Sequential': Scheduler=SequentialScheduler
elif config['scheduler']=='Priority': Scheduler=PriorityScheduler
elif config['scheduler']=='StdPriority': Scheduler=StdPriorityScheduler
else:
    print('Unknown scheduler "'+config['scheduler']+'"! Possible options: Sequential, Priority.')
    input()
    sys.exit()

if not os.path.isdir('schedule'): os.mkdir('schedule')

print('#######################\nConfig loaded!')
print('Observatory:',observatory.name)
print('Date:',date)
print('Number of nights:',str(nights))
print('Scheduler:',Scheduler.__name__)
print('#######################\n')

print('#######################')
print('Used constraints:\n-------------------')
for c in constraints:
    tmp=c.__class__.__name__+': '
    if hasattr(c,'min'):
        if c.min is not None: tmp+='min '+str(c.min)+'; '
    if hasattr(c,'max'):
        if c.max is not None: tmp+='max '+str(c.max)+'; '
    if hasattr(c, 'max_solar_altitude'): tmp+='max_solar_alt '+str(c.max_solar_altitude)+'; '
    print(tmp)
print('#######################\n')


objects0=load_objects(objlist,verbose=True)

objects0={str(uuid.uuid4()): obj for obj in objects0}

print('#######################\nObjects loaded!')
print('Objects:',len(objects0))
print('#######################\n')

plantime=Time(date+' '+str(12-int(round(observatory.longitude.value/15))).rjust(2,'0')+':00:00')    #approx. local noon (in UTC)
while nights>0:
    if not os.path.isdir('schedule/'+plantime.strftime('%Y-%m-%d')): os.mkdir('schedule/'+plantime.strftime('%Y-%m-%d'))

    print('#######################\n'+plantime.strftime('%Y-%m-%d')+'\n#######################\n')
    #calculate sunset, sunrise and midnight times + set some time ranges

    #sunrise/sunset calculation with 1 hour extend -> for scheduling intervals and plots
    #could be modified for speed up -> add horizon=-12*u.deg (nautical twilight) or -18 (astronomical) and remove additional hour.
    midnight=observatory.midnight(plantime,n_grid_points=10, which='next')
    suns=observatory.sun_set_time(midnight,n_grid_points=10, which='nearest')-1*u.hour
    sunr=observatory.sun_rise_time(midnight,n_grid_points=10, which='nearest')+1*u.hour

    night=sunr-suns
    obstime=suns+night*np.linspace(0, 1, 100)    #range of observing scheduling

    print('#######################\nPrefiltering & Presorting...\n#######################\n')
    t0=time.time()
    if config['prefilter']: objects=prefilter(objects0,constraints,observatory,obstime,verbose=True)
    else:
        objects=[]
        for obj in objects0:
            ob=objects0[obj]
            name=ob['target'].name
            ob['target']=FixedTarget(name=obj, coord=ob['target'].coord)
            objects.append(ob)
    if config['presort']: objects=presort(objects, observatory, midnight,key='meridian')     #meridian/set/rise
    print('-------------------')
    print('Calculation time:',time.time()-t0,'seconds')
    print('Observable objects:',len(objects),'\n')
    if len(objects)==0:
        print('NO observable objects!')
        input()
        break

    blocks=[]
    names={}
    for obj in objects:
        if 'Done' in obj['full']:
            if obj['full']['Done']==1: continue
        cons=[]
        if 'StartDate' in obj['full'] and 'EndDate' in obj['full']:
            if not pd.isna(obj['full']['StartDate']) or not pd.isna(obj['full']['EndDate']):
                #constraint on obs date
                try: start=Time(obj['full']['StartDate'])
                except: start=None
                try: end=Time(obj['full']['EndDate'])
                except: end=None
                cons.append(TimeConstraint(start,end))
        if 'MoonPhase' in obj['full']:
            if not pd.isna(obj['full']['MoonPhase']):
                #constraint on Moon phase
                cons.append(MoonIlluminationConstraint(0,obj['full']['MoonPhase']))
                #TODO remove later?
        if 'StartPhase' in obj['full'] and 'EndPhase' in obj['full'] and 'Epoch' in obj['full'] and 'Period' in obj['full']:
            if (not pd.isna(obj['full']['StartPhase']) or not pd.isna(obj['full']['EndPhase'])) and not (pd.isna(obj['full']['Epoch']) or pd.isna(obj['full']['Period'])):
                #phase constraint for EB or exoplanets
                objPer=PeriodicEvent(epoch=Time(obj['full']['Epoch'],format='jd'),period=obj['full']['Period']*u.day)
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
                blocks.append(ObservingBlock.from_exposures(FixedTarget(name=name1, coord=obj['target'].coord),obj['priority'],obj['exp']*u.second,5,read_out,constraints=[]))
        else:
            if obj['target'].name in names:  #repeating objects -> NOT replace debug plots
                names[obj['target'].name]+=1
                name1=obj['target'].name+'_s'+str(names[obj['target'].name])
            else:
                names[obj['target'].name]=0
                name1=obj['target'].name
            blocks.append(ObservingBlock.from_exposures(FixedTarget(name=name1, coord=obj['target'].coord),obj['priority'],obj['exp']*u.second,obj['n_exp'],read_out,constraints=cons))

    print('#######################\nRunning scheduler...')
    print(Scheduler.__name__)
    print('#######################\n')
    constraintsM=[]
    for c in constraints:
        if not 'MoonSep' in c.__class__.__name__ or 'Modif' in c.__class__.__name__: constraintsM.append(c)    #NOT MoonSeparation constraint (already prefiltered) -> speed up

    #running scheduler...
    transitioner = Transitioner(slew_rate)

    t0=time.time()
    scheduler = Scheduler(constraints = constraintsM,observer = observatory,transitioner = transitioner)
    schedule = Schedule(suns,sunr)     #start and end of scheduling interval
    scheduler(blocks, schedule)
    print('#######################\nScheduling finished!')
    print('Calculation time:',time.time()-t0,'seconds')
    print('Observing targets:',len(schedule.observing_blocks))
    print('#######################\n')

    #print schedule in table and save to files
    outname='schedule/'+plantime.strftime('%Y-%m-%d')+'/'+objlist[:objlist.rfind(('.'))]+'_'+observatory.name+'_'+plantime.strftime('%Y-%m-%d')+'_'+Scheduler.__name__

    #check east/west position
    if observatory.name=='lasilla':
        if config['debug']:
            if not os.path.isdir('debug'): os.mkdir('debug')
            if not os.path.isdir('debug/positions'): os.mkdir('debug/positions')
            if not os.path.isdir('debug/positions/'+plantime.strftime('%Y-%m-%d')): os.mkdir('debug/positions/'+plantime.strftime('%Y-%m-%d'))
            check_limits(schedule,plots=True,path=outname.replace('schedule/','debug/positions/')+'_',objects0=objects0)
        else: check_limits(schedule)

    print('#######################\nGenerate outputs...\n#######################\n')
    tab=schedule_table(schedule,objects0)

    cols={'target':'Target', 'start time (UTC)':'Start', 'end time (UTC)':'End','duration (minutes)':'Duration','ra':'RA', 'dec':'DEC', 'mag':'Mag','altitude':'Altitude', 'airmass':'Airmass', 'azimut':'Azimut','priority':'Priority','exposure (seconds)':'ExpTime', 'number exposures':'Number','_Remarks':'Remarks', 'position':'Position'}

    for c in tab.colnames:
        if c in cols: tab[c].name=cols[c]
        elif c[0]=='_':
            if c=='_index' or c=='_configuration': tab.remove_column(c)
            elif not c[1:] in cols.values(): tab[c].name=c[1:]
            elif not (c+'_orig' in tab.colnames or c[1:]+'_orig' in tab.colnames): tab[c].name=c[1:]+'_orig'
            else: tab.remove_column(c)

    if not 'Remarks' in tab.colnames: tab['Remarks']=''

    if 'Position' in tab.colnames: tab1=tab[['index']+list(cols.values())+['configuration']]
    else: tab1=tab[['index']+list(cols.values())[-1]+['configuration']]

    #tab.pprint_all()
    f=open(outname+'_schedule_full.txt','w')
    f.writelines('\n'.join(tab.pformat_all()))
    f.close()

    f=open(outname+'_schedule.txt','w')
    f.writelines('\n'.join(tab1.pformat_all()))
    f.close()

    #tab[~(tab['Target']=='TransitionBlock')].pprint_all()
    f=open(outname+'_schedule_full-objects.txt','w')
    f.writelines('\n'.join(tab[~(tab['Target']=='TransitionBlock')].pformat_all()))
    f.close()
    tab[~(tab['Target']=='TransitionBlock')].to_pandas().to_csv(outname+'_schedule_full-objects.csv',index=False)

    tab1[~(tab1['Target']=='TransitionBlock')].pprint_all()
    f=open(outname+'_schedule_objects.txt','w')
    f.writelines('\n'.join(tab1[~(tab1['Target']=='TransitionBlock')].pformat_all()))
    f.close()
    tab1[~(tab1['Target']=='TransitionBlock')].to_pandas().to_csv(outname+'_schedule-objects.csv',index=False)

    if 'Position' in tab.colnames: tabq=tab[~(tab['Target']=='TransitionBlock')]['Target','RA','DEC','Mag','ExpTime','Number','Remarks','Position']
    else: tabq=tab[~(tab['Target']=='TransitionBlock')]['Target','RA','DEC','Mag','ExpTime','Number','Remarks']
    tabq.pprint_all()
    f=open(outname+'_schedule-queue.txt','w')
    f.writelines('\n'.join(tabq.pformat_all()))
    f.close()

    tabq.to_pandas().to_csv(outname+'_schedule-queue.csv',index=False)

    schedule_batch=batch(schedule,objects0)
    f=open(outname+'_batch.json','w')
    json.dump(schedule_batch,f)
    f.close()

    #modif. tab for web
    df=tab[~(tab['Target']=='TransitionBlock')].to_pandas()
    df['RA']=[x[:x.find('.')] if '.' in x else x for x in df.RA]
    df['DEC']=[x[:x.find('.')] if '.' in x else x for x in df.DEC]
    df['Start']=[x.split()[1][:x.split()[1].rfind(':')] for x in df['Start']]
    df['End']=[x.split()[1][:x.split()[1].rfind(':')] for x in df['End']]
    df.Altitude=[round(float(x)) for x in df.Altitude]
    df.Azimut=[round(float(x)) for x in df.Azimut]
    df.Airmass=['%.1f' %float(x) for x in df.Airmass]
    if 'Position' in tab.colnames: cols=['index', 'Target', 'RA', 'DEC', 'Mag','ExpTime','Number', 'Priority','Start', 'End','Altitude', 'Airmass', 'Azimut', 'Position']
    else: cols=['index', 'Target', 'RA', 'DEC', 'Mag','ExpTime','Number', 'Priority','Start', 'End','Altitude', 'Airmass', 'Azimut']
    df[cols].to_csv(outname+'_schedule-web.csv',index=False)

    print('#######################\nTables generated!\n#######################\n')

    #output images...
    # plt.figure()
    # plot_schedule_airmass(schedule)  #original plot
    # plt.legend(loc='center right',bbox_to_anchor=(1.3, 0.5),fontsize=7)
    # plt.tight_layout()
    # plt.savefig(outname+'_schedule.png',dpi=150)

    plt.figure()
    ax=plot_schedule(schedule,plottype='alt',slots=True,moon=True,objects0=objects)
    plt.savefig(outname+'_alt.png',dpi=150)

    plt.figure()
    ax=plot_schedule(schedule,plottype='airmass',objects0=objects)
    plt.savefig(outname+'_airmass.png',dpi=150)

    plt.figure()
    ax=plot_schedule(schedule,plottype='sky',moon=True,objects0=objects)
    plt.savefig(outname+'_sky.png',dpi=150)

    plt.figure()
    ax=plot_timeline(schedule,night,objects0=objects)
    plt.savefig(outname+'_time.png',dpi=150)

    if config['debug']:
        if not os.path.isdir('debug'): os.mkdir('debug')
        if not os.path.isdir('debug/constraints'): os.mkdir('debug/constraints')
        if not os.path.isdir('debug/'+plantime.strftime('%Y-%m-%d')): os.mkdir('debug/'+plantime.strftime('%Y-%m-%d'))
        if not os.path.isdir('debug/constraints/'+plantime.strftime('%Y-%m-%d')): os.mkdir('debug/constraints/'+plantime.strftime('%Y-%m-%d'))

        plt.figure()
        ax,scores=plot_score(blocks, schedule, constraints,objects0=objects0)
        plt.savefig(outname.replace('schedule/','debug/')+'_score.png',dpi=150)
        plt.close()

        #scores.pprint_all()
        f=open(outname.replace('schedule/','debug/')+'_score.txt','w')
        f.writelines('\n'.join(scores.pformat_all()))
        f.close()

        for star in list(objects0.values()):
            plt.figure()
            plot_constraints(constraints,observatory,star['target'],obstime)
            plt.savefig(outname.replace('schedule/','debug/constraints/')+'_'+star['full']['Target'].replace(' ','_').replace('/','_')+'.png',dpi=150)
            plt.close()

    #plt.show()
    print('#######################\nPlots generated!\n#######################\n')

    #remove already scheduled targets for next night scheduling
    objects1=[x.target.name for x in schedule.observing_blocks]
    tmp={}
    for i,obj in enumerate(objects0.values()):
        if obj['target'].name in objects1:
            del(objects1[objects1.index(obj['target'].name)])
            if obj['priority']<1:       #every-night objects (RV std...)
                name=obj['target'].name
                obj['target']=FixedTarget(name=obj['full']['Target'], coord=obj['target'].coord)
                tmp[name]=obj
        else:
            name=obj['target'].name
            obj['target']=FixedTarget(name=obj['full']['Target'], coord=obj['target'].coord)
            tmp[name]=obj

    objects0=dict(tmp)

    if len(objects0)==0: break

    nights-=1
    plantime+=1*u.day
