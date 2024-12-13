from scheduler import *

import time
import sys
import json

if len(sys.argv)<3:
    print('USAGE: python3 plot_schedule.py "config_file" "schedule"')
    input()
    sys.exit()

configFile=sys.argv[1]
objlist=sys.argv[2]

date=''
if len(sys.argv)>3: date=sys.argv[3]   #Y-m-d

config=load_config(configFile)
if config is None:
    input()
    sys.exit()

observatory=config['observatory']

read_out = config['read_out']     #read_out time of camera + comp (with readout) + ...
slew_rate = config['slew_rate']   #slew rate of the telescope

check=True
transitions=True


df=pd.read_csv(objlist)
#df = df.rename(columns={'target': 'Target', 'ra': 'RA', 'dec': 'DEC', 'number exposures': 'Number', 'exposure (seconds)': 'ExpTime', 'number': 'Number', 'exposure': 'ExpTime', 'end': 'stop','start time (UTC)':'start','end time (UTC)':'stop','Start':'start','End':'stop','V':'mag','Vmag':'mag','Mag':'mag'})

if not 'Number' in df.columns: df['Number']=np.full(len(df),np.nan)
if not 'RA' in df.columns: df['RA']=np.full(len(df),'')
if not 'DEC' in df.columns: df['DEC']=np.full(len(df),'')
if not 'Mag' in df.columns: df['Mag']=np.full(len(df),'')

df['Number'][pd.isna(df['Number'])]=1    #if missing number of exp. -> 1 exp.
df['Mag'][pd.isna(df['Mag'])]=''

objects={}
names={}
for i,x in df.iterrows():
    name=x['Target'].strip()
    if name in names:  #repeating objects -> NOT replace debug plots
        names[name]+=1
        name1=name+'_s'+str(names[name])
    else:
        names[name]=0
        name1=name
    if len(x['RA'])*len(x['DEC'])>0:
        ra='{}h{}m{}s'.format(*x['RA'].replace(':',' ').replace(',','.').split())
        dec='{}d{}m{}s'.format(*x['DEC'].replace(':',' ').replace(',','.').split())
        coordinates=SkyCoord(ra,dec,frame='icrs')
        if check:
            check_simbad(name, coordinates)
    else: coordinates=search_simbad(name).coord
    try: objects[name1]={'target':FixedTarget(name=name1, coord=coordinates),'exp':x['ExpTime'],'n_exp':x['Number'],'start':Time(x['Start']),'stop':Time(x['End']),'mag':x['Mag'],'full':x}
    except:
        start=Time(date+' '+x['Start']+':00')
        stop=Time(date+' '+x['End']+':00')
        if start.datetime.hour<12: start+=1*u.day
        if stop.datetime.hour<12: stop+=1*u.day

        if len(objects)>0:
            if start<list(objects.values())[0]['start']: start+=1*u.day

        if stop<start: stop+=1*u.day

        objects[name1]={'target':FixedTarget(name=name1, coord=coordinates),'exp':x['ExpTime'],'n_exp':x['Number'],'start':start,'stop':stop,'mag':x['Mag'],'full':x}



#plantime=Time(list(objects.values())[0]['start'].strftime('%Y-%m-%d')+' '+str(12-int(round(observatory.longitude.value/15))).rjust(2,'0')+':00:00')    #approx. local noon (in UTC)
#calculate sunset, sunrise and midnight times + set some time ranges

#sunrise/sunset calculation with 1 hour extend -> for scheduling intervals and plots
#could be modified for speed up -> add horizon=-12*u.deg (nautical twilight) or -18 (astronomical) and remove additional hour.
#midnight=observatory.midnight(plantime,n_grid_points=10, which='next')
suns=observatory.sun_set_time(Time(list(objects.values())[0]['start']),n_grid_points=10, which='previous')-1*u.hour
sunr=observatory.sun_rise_time(Time(list(objects.values())[-1]['stop']),n_grid_points=10, which='next')+1*u.hour

night=sunr-suns
obstime=suns+night*np.linspace(0, 1, 100)    #range of observing scheduling

if transitions: transitioner = Transitioner(slew_rate)
else: read_out=0

#add targets to schedule
schedule=Schedule(suns,sunr)
schedule.observer = observatory
for obj in objects.values():
    b=ObservingBlock.from_exposures(obj['target'],1,obj['exp']*u.second,obj['n_exp'],read_out)
    b.observer = observatory
    #print(obj['target'].name)
    t0=obj['start']
    if len(schedule.scheduled_blocks)>0 and transitions:
        tr=transitioner(schedule.scheduled_blocks[-1], b,schedule.scheduled_blocks[-1].end_time,observatory)
        if tr is not None: schedule.insert_slot(schedule.scheduled_blocks[-1].end_time,tr)
        t0=schedule.scheduled_blocks[-1].end_time
    schedule.insert_slot(t0, b)
    #schedule.insert_slot(objects[0]['start'], b)

objlist=objlist.split('/')[-1]
#print schedule in table and save to files
outname='schedule/'+objlist[:objlist.rfind(('.'))]+'/'+objlist[:objlist.rfind(('.'))]+'_'+observatory.name

#check east/west position
if observatory.name=='lasilla':
    if config['debug']:
        if not os.path.isdir('debug'): os.mkdir('debug')
        if not os.path.isdir('debug/positions'): os.mkdir('debug/positions')
        if not os.path.isdir('debug/positions/'+objlist[:objlist.rfind('.')]): os.mkdir('debug/positions/'+objlist[:objlist.rfind('.')])
        check_limits(schedule,plots=True,path=outname.replace('schedule/','debug/positions/')+'_')
    else: check_limits(schedule)

if not os.path.isdir('schedule/'+objlist[:objlist.rfind(('.'))]): os.mkdir('schedule/'+objlist[:objlist.rfind(('.'))])

print('#######################\nGenerate outputs...\n#######################\n')
tab=schedule_table(schedule,objects)

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

schedule_batch=batch(schedule,objects)
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

#plt.show()
print('#######################\nPlots generated!\n#######################\n')
