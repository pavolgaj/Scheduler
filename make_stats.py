import sys
import os
import json
import csv
import glob
import datetime

from astropy.coordinates import get_sun
from astropy.time import Time
import astropy.units as u

import numpy as np

obs_lon=-70.739     #longitude of observatory
obs_lat=-29.25572   #latitude of observatory

def make_stats():
    '''make statistics of observations'''
    stats={}
    observations={}
    new=[]
    names={}  #utilize similar objects names - spaces, lower/upper case etc.
    all_names={}  # all used name for same target
    logs=list(sorted(glob.glob('static/logs/*_log.csv')))   #path to log files
    if len(logs)==0: return

    if os.path.isfile('db/statistics.csv'):
        # read old stats
        f=open('db/last','r')
        last=f.readline().strip()   #last file in made stats
        f.close()
        if logs[-1]==last: return   #no new logs -> nothing to do...

        #loads old stats
        f=open('db/statistics.csv','r')
        lines=f.readlines()
        f.close()
        for l in lines[1:]:
            tmp=l.strip().split(',')
            target=tmp[0]
            inst=tmp[1]
            exp=float(tmp[2])
            n=int(tmp[3])
            last0=tmp[4]
            if target in stats:
                if inst in stats[target]:
                    stats[target][inst][exp]={'n':n,'last':last0}
                else: stats[target][inst]={exp:{'n':n,'last':last0}}
            else: stats[target]={inst:{exp:{'n':n,'last':last0}}}

        f=open('db/observations.json','r')
        observations=json.load(f)
        f.close()

        f=open('db/names.json','r')
        names=json.load(f)
        f.close()
        
        f=open('db/all_names.json','r')
        all_names=json.load(f)
        f.close()

        new=logs[logs.index(last)+1:]   #detect new logs (not in stats)
    else: new=logs

    #work only with new logs
    for log in new:
        f=open(log,'r')
        reader = csv.DictReader(f)
        last=os.path.basename(log)[:10]
        for obs in reader:
            target=obs['object'].replace('?','').replace('ttarget-','').replace('ttarget_','').replace('_',' ').strip()
            #remove calibrations and tests
            if target.lower() in ['bias','flat','comp','test','zero','thar','','dark','pokus','neco','xx','calibration','djdj','rtjhrstjh','shgdfz','shs','shswh','ttt','yflju','t','twst','ttarget']: continue
            if 'test' in target.lower(): continue
            if 'thar' in target.lower(): continue
            if 'flat' in target.lower(): continue
            if 'dark' in target.lower(): continue
            if 'dome' in target.lower(): continue
            if 'pok' in target.lower(): continue
            if 'front' in target.lower(): continue
            if 'spektrum' in target.lower(): continue
            if 'comp' in target.lower(): continue
            if 'bias' in target.lower(): continue
            if 'zero' in target.lower(): continue
            if 'comb' in target.lower(): continue

            #utilize similar objects names - spaces, lower/upper case etc.
            if not target.lower().replace('-','').replace(' ','') in names: 
                names[target.lower().replace('-','').replace(' ','')]=target
                all_names[target.lower().replace('-','').replace(' ','')]=[target.replace(' ','_')]
            elif not target.replace(' ','_') in all_names[target.lower().replace('-','').replace(' ','')]:
                all_names[target.lower().replace('-','').replace(' ','')].append(target.replace(' ','_'))
            target=names[target.lower().replace('-','').replace(' ','')]
            exp=float(obs['exposure'])
            inst=obs['instrument']
            #add obj to stats -> for specific exp. time
            if target in stats:
                if inst in stats[target]:
                    if exp in stats[target][inst]:
                        if stats[target][inst][exp]['last']==last: continue
                        stats[target][inst][exp]['n']+=1
                        stats[target][inst][exp]['last']=last
                    else: stats[target][inst][exp]={'n':1,'last':last}
                else: stats[target][inst]={exp:{'n':1,'last':last}}
            else: stats[target]={inst:{exp:{'n':1,'last':last}}}

            if target in observations:
                if not last in observations[target]:
                    observations[target].append(last)
            else: observations[target]=[last]
        f.close()

    #save stats + name of last log
    f=open('db/last','w')
    f.write(new[-1])
    f.close()
    os.chmod('db/last', 0o666)

    #last night observations (for stats page)
    f=open('db/statistics.csv','w')
    f.write('object,instrument,exposure,nights,last\n')
    for target in sorted(stats):
        for inst in sorted(stats[target]):
            for exp in sorted(stats[target][inst]):
                f.write(target+',')
                f.write(inst+',')
                f.write(str(exp)+',')
                f.write(str(stats[target][inst][exp]['n'])+',')
                f.write(stats[target][inst][exp]['last']+'\n')
    f.close()
    os.chmod('db/statistics.csv', 0o666)

    #all obs (for search page)
    f=open('db/observations.json','w')
    json.dump(observations,f)
    f.close()
    os.chmod('db/observations.json', 0o666)

    f=open('db/names.json','w')
    json.dump(names,f)
    f.close()
    os.chmod('db/names.json', 0o666)
    
    f=open('db/all_names.json','w')
    json.dump(all_names,f)
    f.close()
    os.chmod('db/all_names.json', 0o666)

def night_time(date):
    '''get lenght of night for given day in hours'''
    #approx. local noon (in UTC)
    dt=Time(date.strftime('%Y-%m-%d')+' '+str(12-int(round(float(obs_lon)/15))).rjust(2,'0')+':00:00')

    sun=get_sun(dt)
    
    dec=sun.dec.degree

    #sunrise
    # ha=np.rad2deg(np.arccos(-np.tan(np.deg2rad(dec))*np.tan(np.deg2rad(float(obs_lat)))))
    # sid=360-ha+sun.ra.degree-dt.sidereal_time('mean',float(obs_lon)*u.deg).degree
    # if sid>=360: sid-=360
    # 
    # rise=dt+TimeDelta(sid/15*3600*u.second)
    
    #astro twilight
    ha=np.rad2deg(np.arccos((np.sin(np.deg2rad(-18))-np.sin(np.deg2rad(dec))*np.sin(np.deg2rad(float(obs_lat))))/(np.cos(np.deg2rad(dec))*np.cos(np.deg2rad(float(obs_lat))))))
    # sid=360-ha+sun.ra.degree-dt.sidereal_time('mean',float(obs_lon)*u.deg).degree
    # if sid>=360: sid-=360
    # 
    # astro=dt+TimeDelta(sid/15*3600*u.second)
    
    return 24-2*ha/15

if __name__ == '__main__':
    import PDFReportClass as pdf

    if len(sys.argv)>1:
        # run as python make_stats.py start(Y-m-d) end(Y-m-d)
        start=datetime.datetime.strptime(sys.argv[1],'%Y-%m-%d').date()
        end=datetime.datetime.strptime(sys.argv[2],'%Y-%m-%d').date()
    else:
        #previous month
        now=datetime.date.today()
        start=(now-datetime.timedelta(days=now.day)).replace(day=1)
        end=(now-datetime.timedelta(days=now.day))


    make_stats()

    f=open('db/observations.json','r')
    observed=json.load(f)
    f.close()

    f=open('db/names.json','r')
    names=json.load(f)
    f.close()

    objects={}
    f=open('db/objects.csv','r')
    reader = csv.DictReader(f)
    for row in reader:
        if not row['Target'] in objects:
            objects[row['Target'].lower().replace('-','').replace(' ','').replace('_','')]=row['Supervisor']
    f.close()

    stats={}
    for obj in observed:
        n=len([x for x in observed[obj] if datetime.datetime.strptime(x,'%Y-%m-%d').date()>=start and datetime.datetime.strptime(x,'%Y-%m-%d').date()<=end])
        if n>0:
            name=obj.lower().replace('-','').replace(' ','').replace('_','')
            stats[name]={'nights': n}
            if name in objects: stats[name]['supervisor']=objects[name]
            else: stats[name]['supervisor']='unknown'
            
            total=0
            number=0
            for night in [x for x in observed[obj] if datetime.datetime.strptime(x,'%Y-%m-%d').date()>=start and datetime.datetime.strptime(x,'%Y-%m-%d').date()<=end]:            
                f=open('static/logs/'+night+'_log.csv','r')
                reader = csv.DictReader(f)
                for row in reader:
                    #utilize similar objects names - spaces, lower/upper case etc.
                    if row['object'].replace('?','').replace('ttarget-','').replace('ttarget_','').replace('_',' ').strip().lower().replace('-','').replace(' ','')==name:
                        total+=float(row['exposure'])
                        number+=1
                f.close() 
            stats[name]['number']=number
            stats[name]['time']=total

    if start.strftime('%Y%m')==end.strftime('%Y%m'): outname=start.strftime('%Y%m')
    else: outname=start.strftime('%Y%m')+'-'+end.strftime('%Y%m')

    f=open('statistics/statistics_'+outname+'.csv','w')
    writer = csv.DictWriter(f, fieldnames=['Target','Supervisor','Nights','ExpNumber','TotalTime_hours'])
    writer.writeheader()
    if len(stats)>0:
        data=[['Target','Supervisor','Nights','ExpNumber','TotalTime (h)']]
        for obj in sorted(stats):
            writer.writerow({'Target':names[obj],'Supervisor':stats[obj]['supervisor'],'Nights':stats[obj]['nights'],'ExpNumber':stats[obj]['number'],'TotalTime_hours':stats[obj]['time']/3600})
            data.append([names[obj],stats[obj]['supervisor'],stats[obj]['nights'],stats[obj]['number'],round(stats[obj]['time']/3600,2)])
    f.close()

    p=pdf.PDFReport('statistics/statistics_'+outname+'.pdf')
    if start.strftime('%Y%m')==end.strftime('%Y%m'): p.set_title(start.strftime('%Y%m'))
    else: p.set_title(start.strftime('%Y%m')+'-'+end.strftime('%Y%m'))
    p.set_author('Scheduler')

    if len(stats)>0:
        #get lenght of all nights
        nights=0
        date=start
        while date<=end:
            nights+=night_time(date)
            date+=datetime.timedelta(days=1)
        
        p.nights=nights
        p.put_dataframe_on_pdfpage(data)
    else:
        p.put_dataframe_on_pdfpage(df=None)

    p.write_pdfpage()

    print(outname)

