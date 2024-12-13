import os
from datetime import datetime,timezone

from astropy import units as u
from astropy.coordinates import SkyCoord
from astropy.time import Time
from astropy.coordinates import get_moon
from astropy.table import Table
from astropy.coordinates import EarthLocation, AltAz

from astroquery.simbad import Simbad

from astroplan import FixedTarget
from astroplan import Observer
from astroplan import download_IERS_A
from astroplan import AltitudeConstraint,AirmassConstraint,AtNightConstraint,TimeConstraint
from astroplan import MoonIlluminationConstraint,MoonSeparationConstraint
from astroplan import is_observable, is_always_observable, months_observable,is_event_observable
from astroplan import observability_table
from astroplan import ObservingBlock,TransitionBlock
from astroplan import EclipsingSystem,PeriodicEvent
from astroplan import PrimaryEclipseConstraint,PhaseConstraint,SecondaryEclipseConstraint
from astroplan import Scorer
from astroplan.constraints import Constraint,max_best_rescale,_get_altaz,min_best_rescale
from astroplan.scheduling import SequentialScheduler,PriorityScheduler,Transitioner,Schedule
from astroplan.plots import plot_airmass,plot_altitude,plot_schedule_airmass
from astroplan.utils import time_grid_from_range, stride_array

from bettersky import plot_sky,plot_sky_24hr

import matplotlib
import matplotlib.pyplot as plt
import matplotlib.patches as patches
from matplotlib.collections import PatchCollection

import numpy as np
import pandas as pd

import warnings
warnings.filterwarnings("ignore")

# main routine platospec E152 scheduler
# (c) Pavol Gajdos, 2024

def_priority=3

def load_config(path,verbose=False):
    '''read config'''
    f=open(path,'r')
    config={}
    for line in f:
        tmp=[x.strip() for x in line.split('=')]
        if tmp[0]=='observatory': config['obs_name']=tmp[1]
        elif tmp[0]=='latitude': config['obs_lat']=tmp[1]
        elif tmp[0]=='longitude': config['obs_lon']=tmp[1]
        elif tmp[0]=='elevation': config['obs_ele']=float(tmp[1].split()[0])*u.meter
        elif tmp[0]=='readout': config['read_out']=float(tmp[1].split()[0])*u.second
        elif tmp[0]=='slewrate': config['slew_rate']=float(tmp[1].split()[0])*u.deg/u.minute
        elif tmp[0]=='minAlt': config['minAlt']=float(tmp[1].split()[0])*u.deg
        elif tmp[0]=='maxAlt': config['maxAlt']=float(tmp[1].split()[0])*u.deg
        elif tmp[0]=='maxAirmass': config['airmass']=float(tmp[1])
        elif tmp[0]=='moonSep': config['moon']=float(tmp[1].split()[0])*u.deg
        elif tmp[0]=='scheduler': config['scheduler']=tmp[1].split()[0]
        elif tmp[0]=='debug': config['debug']=bool(int(tmp[1].split()[0]))
        elif tmp[0]=='presort': config['presort']=bool(int(tmp[1].split()[0]))
        elif tmp[0]=='prefilter': config['prefilter']=bool(int(tmp[1].split()[0]))
    f.close()

    if config['obs_name'] in EarthLocation.get_site_names(): config['observatory']=Observer.at_site(config['obs_name'])
    elif 'obs_lat' in config and 'obs_lon' in config:
        if not 'obs_ele' in config: config['obs_ele']=0*u.meter

        config['observatory'] = Observer(name=config['obs_name'],longitude=config['obs_lon'],latitude=config['obs_lat'],elevation=config['obs_ele'])
    else:
        if verbose: print('Unknown observatory "',config['obs_name'],'"! Set latitude, longitude and elevation!')
        return

    return config

def load_objects(path,check=True,verbose=False):
    '''read object list from csv...?'''
    df=pd.read_csv(path)
    df = df.rename(columns={'target': 'Target', 'ra': 'RA', 'dec': 'DEC', 'number exposures': 'Number', 'exposure (seconds)': 'ExpTime','V':'mag','Vmag':'Mag','mag':'Mag'})
    if not 'Number' in df.columns: df['Number']=np.full(len(df),np.nan)
    if not 'Priority' in df.columns: df['Priority']=np.full(len(df),np.nan)
    if not 'RA' in df.columns: df['RA']=np.full(len(df),'')
    if not 'DEC' in df.columns: df['DEC']=np.full(len(df),'')
    if not 'Mag' in df.columns: df['Mag']=np.full(len(df),'')

    df['Number'][pd.isna(df['Number'])]=1    #if missing number of exp. -> 1 exp.
    df['Priority'][pd.isna(df['Priority'])]=def_priority   #if missing priority -> default value
    df['Mag'][pd.isna(df['Mag'])]=''

    objects=[]
    for i,x in df.iterrows():
        name=x['Target'].strip()
        if len(x['RA'])*len(x['DEC'])>0:
            ra='{}h{}m{}s'.format(*x['RA'].replace(':',' ').replace(',','.').split())
            dec='{}d{}m{}s'.format(*x['DEC'].replace(':',' ').replace(',','.').split())
            coordinates=SkyCoord(ra,dec,frame='icrs')
            if check:
                check_simbad(name, coordinates,verbose=verbose)
        else: coordinates=search_simbad(name).coord
        objects.append({'target':FixedTarget(name=name, coord=coordinates),'exp':x['ExpTime'],'n_exp':x['Number'],'priority':x['Priority'],'mag':x['Mag'],'full':x})
    return objects


def check_simbad(name,coords,radius=15,verbose=False):
    '''check if object is in simbad and check if coordinates are good (tol. "radius" arcsec)'''
    if '/' in name or '=' in name:
        names=[x.strip() for x in name.replace('/','=').split('=')]
        for n in names:
            simbad=search_simbad(n,warning=False,verbose=verbose)
            if simbad is not None:
                if verbose: print('Input object "'+name+'" found under name "'+n+'".')
                break
        if verbose:
            if simbad is None: print('Object "'+name+'" NOT found in Simbad!')
    else: simbad=search_simbad(name,verbose=verbose)
    if simbad is None:
        if verbose: print('Searching for close objects...')
        result_table=Simbad.query_region(coords, radius=radius*u.arcsec)
        if result_table is None:
            if verbose: print('NO close object found!\n')
            return False
        elif len(result_table)==0:
                if verbose: print('NO close object found!\n')
                return False
        result=result_table[0]
        if 'ra' in result_table.colnames:
            #astroquery>=0.4.8
            ra=result['ra']*u.deg
            dec=result['dec']*u.deg
        else:
            ra='{}h{}m{}s'.format(*result['RA'].replace(':',' ').replace(',','.').split())
            dec='{}d{}m{}s'.format(*result['DEC'].replace(':',' ').replace(',','.').split())
        dist=coords.separation(SkyCoord(ra,dec,frame='icrs'))
        if 'main_id' in result_table.colnames:
            #astroquery>=0.4.8
            if verbose: print('"'+result['main_id']+'" found! Distance to input object "'+name+'" is',round(dist.value*3600,2),'arcsec.\n')
        elif verbose:  print('"'+result['MAIN_ID']+'" found! Distance to input object "'+name+'" is',round(dist.value*3600,2),'arcsec.\n')
        return True
    else:
        dist=coords.separation(simbad.coord)
        if dist>radius*u.arcsec:
            if verbose: print('Coordinates of "'+name+'" do NOT agree with values in Simbad! Error is',round(dist.value*3600,2),'arcsec =',round(dist.value*60,2),'arcmin.')

            if verbose: print('Searching for close objects...')
            result_table=Simbad.query_region(coords, radius=radius*u.arcsec)
            if result_table is None:
                if verbose: print('NO close object found!\n')
                return False
            elif len(result_table)==0:
                if verbose: print('NO close object found!\n')
                return False
            result=result_table[0]
            if 'ra' in result_table.colnames:
                #astroquery>=0.4.8
                ra=result['ra']*u.deg
                dec=result['dec']*u.deg
            else:
                ra='{}h{}m{}s'.format(*result['RA'].replace(':',' ').replace(',','.').split())
                dec='{}d{}m{}s'.format(*result['DEC'].replace(':',' ').replace(',','.').split())
            dist=coords.separation(SkyCoord(ra,dec,frame='icrs'))
            if 'main_id' in result_table.colnames:
                #astroquery>=0.4.8
                if verbose: print('"'+result['main_id']+'" found! Distance to input coordinates is',round(dist.value*3600,2),'arcsec.\n')
            elif verbose:  print('"'+result['MAIN_ID']+'" found! Distance to input coordinates is',round(dist.value*3600,2),'arcsec.\n')
            return False
        elif ('/' in name or '=' in name) and verbose: print()
    return True

def search_simbad(name,warning=True, verbose=False):
    '''create target from name using name and simbad query -> faster than astroplan from_name function'''
    result_table = Simbad.query_object(name)
    if result_table is None:
        if warning:
            if verbose: print('Object "'+name+'" NOT found in Simbad!')
        return None
    elif len(result_table)==0:
        if warning:
            if verbose: print('Object "'+name+'" NOT found in Simbad!')
        return None
    result_table=result_table[0]
    if 'ra' in result_table.colnames:
        #astroquery>=0.4.8
        ra=result_table['ra']
        dec=result_table['dec']
        coordinates = SkyCoord(ra*u.deg,dec*u.deg, frame='icrs')
    else:
        ra=result_table['RA']
        dec=result_table['DEC']
        coordinates = SkyCoord('{}h{}m{}s'.format(*ra.split()),'{}d{}m{}s'.format(*dec.split()), frame='icrs')
    return FixedTarget(name=name, coord=coordinates)

def prefilter(objects,constraints,obs,time, verbose=False):
    '''filter of not observable objects'''
    filt_obj=[]
    if verbose: print('NOT observable objects:\n-------------------')
    for obj in objects:
        ob=objects[obj]
        name=ob['target'].name
        ob['target']=FixedTarget(name=obj, coord=ob['target'].coord)
        if is_observable(constraints, obs, ob['target'], time): filt_obj.append(ob)
        elif verbose: print(name)
    return filt_obj

def presort(objects,obs,date,key='meridian'):
    '''sort objects according to setting/rising/meridian time'''
    if key=='set': return sorted(objects,key=lambda x: obs.target_set_time(date,x['target'],n_grid_points=10,which='nearest'))
    if key=='rise': return sorted(objects,key=lambda x: obs.target_rise_time(date,x['target'],n_grid_points=10,which='nearest'))
    if key=='meridian': return sorted(objects,key=lambda x: obs.target_meridian_transit_time(date,x['target'],n_grid_points=10,which='nearest'))

def load_limits():
    '''load and process limits'''
    east=np.loadtxt('limits_east.txt')
    west=np.loadtxt('limits_west.txt')
    limits=np.append(east,west[::-1],axis=0)

    i=np.where(np.abs(np.diff(np.sign(limits[:,1]+90)))>0)[0]
    limits1=np.append(limits[:i[0]+1], [[(limits[i[0]+1,0]-limits[i[0],0])/(limits[i[0]+1,1]-limits[i[0],1])*(-90-limits[i[0],1])+limits[i[0],0],-89.99]],axis=0)
    limits1=np.append(limits1, [[(limits[i[0]+1,0]-limits[i[0],0])/(limits[i[0]+1,1]-limits[i[0],1])*(-90-limits[i[0],1])+limits[i[0],0],-90.01]],axis=0)
    limits1=np.append(limits1,limits[i[0]+1:i[1]+1],axis=0)
    limits1=np.append(limits1, [[(limits[i[1]+1,0]-limits[i[1],0])/(limits[i[1]+1,1]-limits[i[1],1])*(-90-limits[i[1],1])+limits[i[1],0],-90.01]],axis=0)
    limits1=np.append(limits1, [[(limits[i[1]+1,0]-limits[i[1],0])/(limits[i[1]+1,1]-limits[i[1],1])*(-90-limits[i[1],1])+limits[i[1],0],-89.99]],axis=0)
    limits1=np.append(limits1,limits[i[1]+1:],axis=0)

    eastLim=limits1[np.where(limits1[:,1]>-90)]
    westLim=limits1[np.where(limits1[:,1]<-90)]

    return eastLim, westLim

def plot_limits(ha0,ha1,dec,title=None):
    '''plot east/west telescope limits from hour angle and dec'''
    eastLim, westLim=load_limits()

    if ha0<-90*u.deg: ha0+=360*u.deg
    if ha0>270*u.deg: ha0-=360*u.deg

    if ha1<-90*u.deg: ha1+=360*u.deg
    if ha1>270*u.deg: ha1-=360*u.deg

    ha0W=ha0+180*u.deg
    if ha0W>270*u.deg: ha0W-=360*u.deg

    ha1W=ha1+180*u.deg
    if ha1W>270*u.deg: ha1W-=360*u.deg
    decW=-180*u.deg-dec

    fig=plt.figure()
    ax1 = fig.add_subplot(111)
    ax1.plot(eastLim[:,0],eastLim[:,1],'b-')
    ax1.plot(westLim[:,0],westLim[:,1],'r-')
    ax1.plot(ha0,dec,'b<')
    ax1.plot(ha0W,decW,'r<')
    ax1.plot(ha1,dec,'b>')
    ax1.plot(ha1W,decW,'r>')
    ax1.set_xlim(-90,270)
    ax1.set_ylim(-240,60)
    ax1.hlines(-90, -90, 270, colors='k',linestyles=':')

    ax1.xaxis.set_ticks(range(-90,270+1,30),[str(int(round(i/15))) for i in range(-90-180,270-180+1,30)])
    ax1.set_xlabel('West position - Hour angle (hours)')

    ax2 = ax1.twiny()
    ax2.set_xlim(-90,270)
    ax2.xaxis.set_ticks(range(-90,270+1,30),[str(int(round(i/15))) for i in range(-90,270+1,30)])
    ax2.set_xlabel('East position - Hour angle (hours)')

    ax1.yaxis.set_ticks(range(-240,60+1,30),[str(i) if i>=-90 else str(-i-180) for i in range(-240,60+1,30)])
    ax1.set_ylabel('Declination (deg)')
    if title: plt.title(title)
    ax1.text(220, -70, 'east', color='blue')
    ax1.text(220, -110, 'west', color='red')
    plt.tight_layout()
    return fig

def check_limits(schedule,plots=False,path='',objects0={}):
    '''check east/west telescope limits'''
    import matplotlib.path as mplPath

    eastLim, westLim=load_limits()

    PathE=mplPath.Path(eastLim)
    PathW=mplPath.Path(westLim)

    for slot in schedule.slots:
        position=''
        if hasattr(slot.block, 'target'):
            ra=slot.block.target.ra
            dec=slot.block.target.dec

            lst0=schedule.observer.local_sidereal_time(slot.start).deg*u.deg
            ha0=(lst0-ra)%(360*u.deg)
            if ha0<-90*u.deg: ha0+=360*u.deg
            if ha0>270*u.deg: ha0-=360*u.deg

            lst1=schedule.observer.local_sidereal_time(slot.end).deg*u.deg
            ha1=(lst1-ra)%(360*u.deg)
            if ha1<-90*u.deg: ha1+=360*u.deg
            if ha1>270*u.deg: ha1-=360*u.deg

            if PathE.contains_point((ha0.deg,dec.deg))*PathE.contains_point((ha1.deg,dec.deg)): position+='e'

            ha0W=ha0+180*u.deg
            if ha0W>270*u.deg: ha0W-=360*u.deg

            ha1W=ha1+180*u.deg
            if ha1W>270*u.deg: ha1W-=360*u.deg
            decW=-180*u.deg-dec

            #print(ra)
            #print(lst0,ha0,dec,ha0W,decW)
            #print(lst1,ha1,dec,ha1W,decW)

            if PathW.contains_point((ha0W.deg,decW.deg))*PathW.contains_point((ha1W.deg,decW.deg)): position+='w'

            slot.block.configuration={**slot.block.configuration,**{'position':str(position)}}

            if plots:
                fig=plot_limits(ha0,ha1,dec)
                if '_s' in slot.block.target.name:
                    tt=slot.block.target.name[:slot.block.target.name.rfind('_s')]
                    s=slot.block.target.name[slot.block.target.name.rfind('_s'):]
                else:
                    tt=slot.block.target.name
                    s=''

                if tt in objects0: name=objects0[tt]['full']['Target']+s
                else: name=slot.block.target.name

                plt.savefig(path+name.replace(' ','_').replace('/','_')+'.png',dpi=150)



def plot_constraints(cons,obs,target,time_range,time_grid_resolution=None,binary=False):
    '''plot satisfaction of observing constraints'''
    from astroplan.utils import time_grid_from_range


    if time_grid_resolution is None: time_grid_resolution=1*u.hour
    time_grid = time_grid_from_range([time_range[0], time_range[-1]],time_resolution=time_grid_resolution)

    observability_grid = np.zeros((len(cons), len(time_grid)))

    names=[]
    for i, constraint in enumerate(cons):
        # Evaluate each constraint
        observability_grid[i, :] = constraint(obs, target, times=time_grid)
        names.append(constraint.__class__.__name__)

    # Create plot showing observability of the target:

    extent = [-0.5, -0.5+len(time_grid), -0.5, len(cons)-0.5]

    if binary: cmap = matplotlib.colors.ListedColormap(['red','green'])  #setting colormap = False, True (observable)   ['red','yellow','green']
    else: cmap='RdYlGn'

    fig, ax = plt.subplots()
    ax.imshow(observability_grid, extent=extent,cmap=cmap)

    ax.set_yticks(range(len(cons)-1,-1,-1))
    ax.set_yticklabels(names)

    ax.set_xticks(range(len(time_grid)))
    ax.set_xticklabels([t.datetime.strftime("%H:%M") for t in time_grid])

    ax.set_xticks(np.arange(extent[0], extent[1]), minor=True)
    ax.set_yticks(np.arange(extent[2], extent[3]), minor=True)

    ax.grid(which='minor', color='w', linestyle='-', linewidth=1)
    ax.tick_params(axis='x', which='minor', bottom='off')
    plt.setp(ax.get_xticklabels(), rotation=30, ha='right')

    ax.tick_params(axis='y', which='minor', left='off')
    ax.set_xlabel('Time on {0} UTC'.format(time_grid[0].datetime.date()))
    #fig.subplots_adjust(left=0.35, right=0.9, top=0.9, bottom=0.1)

    plt.tight_layout()
    return ax

#colors for star track ploting, add if needed more...
colors=['indigo','darkred','darkorange','darkmagenta','darkgreen','brown','blue','red','teal','magenta','green','gold','navy','olive','tomato','royalblue','peru','dodgerblue','darkolivegreen','crimson','blueviolet',
    'slategrey','deeppink','deepskyblue','dimgray','firebrick','forestgreen','fuchsia','darkblue','darkcyan','darkgoldenrod','darkorchid','darksalmon','darkslateblue','darkslategray','darkturquoise','darkviolet','lime',
    'mediumblue','maroon','mediumseagreen','mediumslateblue','mediumspringgreen','mediumvioletred','midnightblue','purple','seagreen','slateblue']
# colors=['darkred','darkorange','darkmagenta','darkgreen','cyan','brown','lightgreen','green','gold','olive','navy','yellow','tomato','royalblue','peru','dodgerblue','darkolivegreen','turquoise','plum','slategrey','blue','crimson',
#     'aqua','aquamarine','blueviolet','chocolate','cadetblue','chartreuse','coral','cornflowerblue','darkblue','darkcyan',
#     'darkgoldenrod','darkkhaki','darkorchid','darksalmon','darkslateblue','darkseagreen','darkslategray','darkturquoise','darkviolet','deeppink','deepskyblue','dimgray','firebrick','forestgreen',
#     'fuchsia','goldenrod','gray','greenyellow','hotpink','indianred','indigo','lawngreen','lightseagreen','lightskyblue','lime','lightslategray','magenta','lightsteelblue','limegreen','mediumblue',
#     'maroon','mediumorchid','mediumaquamarine','mediumpurple','mediumseagreen','mediumslateblue','mediumspringgreen','mediumturquoise','mediumvioletred','midnightblue','olivedrab','orange',
#     'orangered','orchid','purple','red','saddlebrown','seagreen','salmon','skyblue','sandybrown','sienna','slateblue','violet','slategray','springgreen','steelblue','teal','yellowgreen']
colors=500*colors

def plot_schedule(schedule,plottype='alt',show_night=True,legend=False,index=True,moon=False,slots=False,objects0={}):
    '''plot startrack in schedule on altitude (alt), airmass or sky plot'''
    import operator

    ax=None
    if plottype=='alt':
        plot=plot_altitude
        ax=plt.gca()
    elif plottype=='airmass':
        plot=plot_airmass
        ax=plt.gca()

    if show_night and not plottype=='sky':
        start = schedule.start_time.datetime

        # Calculate and order twilights and set plotting alpha for each
        twilights0 = [
            (schedule.observer.sun_set_time(Time(start), which='next',n_grid_points=10).datetime, 0.0,'sunset'),
            (schedule.observer.twilight_evening_civil(Time(start), which='next',n_grid_points=10).datetime, 0.1,'civil'),
            (schedule.observer.twilight_evening_nautical(Time(start), which='next',n_grid_points=10).datetime, 0.2,'nautic'),
            (schedule.observer.twilight_evening_astronomical(Time(start), which='next',n_grid_points=10).datetime, 0.3,'astro'),
            (schedule.observer.twilight_morning_astronomical(Time(start), which='next',n_grid_points=10).datetime, 0.4,'astro'),
            (schedule.observer.twilight_morning_nautical(Time(start), which='next',n_grid_points=10).datetime, 0.3,'nautic'),
            (schedule.observer.twilight_morning_civil(Time(start), which='next',n_grid_points=10).datetime, 0.2,'civil'),
            (schedule.observer.sun_rise_time(Time(start), which='next',n_grid_points=10).datetime, 0.1,'sunrise'),
        ]

        twilights=[]
        for t in twilights0:
            if not isinstance(t[0],np.ndarray): twilights.append(t)  #remove if not twilight

        if plottype=='alt': ymin=1
        else: ymin=2.98
        twilights.sort(key=operator.itemgetter(0))
        if slots:
            for tw in twilights: plt.vlines(tw[0], ymin=0, ymax=91, colors='k',linestyles=':',linewidth=1,alpha=0.5)
        else:
            for i, twi in enumerate(twilights[1:], 1):
                plt.axvspan(twilights[i - 1][0], twilights[i][0],ymin=0, ymax=1, color='grey', alpha=twi[1])

        for i,tw in enumerate(twilights):
            if i<4: plt.text(tw[0],ymin,tw[2],horizontalalignment='right',verticalalignment='bottom',fontsize=8,rotation='vertical')
            else: plt.text(tw[0],ymin,tw[2],horizontalalignment='left',verticalalignment='bottom',fontsize=8,rotation='vertical')


    stime=schedule.start_time+(schedule.end_time-schedule.start_time)*np.linspace(0,1,100)

    if moon:
        mtime=stime[::2]
        if plottype=='sky': mtime=mtime[::2]
        moon_altaz=schedule.observer.moon_altaz(mtime)

        #moon phase (0-1)
        k=round((schedule.start_time.datetime.year+schedule.start_time.datetime.month/12.+schedule.start_time.datetime.day/365.-2000)*12.3685)
        T=k/1236.85
        newm=2451550.09766+29.530588861*k+0.00015437*T**2-0.000000150*T**3+0.00000000073*T**4  #predosli nov
        while schedule.start_time.jd<newm: newm-=29.530588861
        age=schedule.start_time.jd-newm
        phase0=age/29.530588861

        phase=phase0
        if phase0>0.5: phase-=0.5
        arg=2*np.pi*phase

        a=np.arange(0,2*np.pi,0.1)
        x0=np.cos(a)
        y0=np.sin(a)

        ym=np.arange(-1,1.01,0.1)
        xm=np.cos(arg)*np.sqrt(1-ym**2)
        y1=np.arange(1,-1.01,-0.1)
        x1=np.sqrt(1-y1**2)
        xm=np.append(xm,x1)
        ym=np.append(ym,y1)
        xy=np.zeros([xm.shape[0],2])
        xy[:,0]=xm
        xy[:,1]=ym

        xy0=np.zeros([x0.shape[0],2])
        xy0[:,0]=x0
        xy0[:,1]=y0

        if phase0<=0.5:
            polygon=patches.Polygon(xy0,closed=True)
            p1=PatchCollection([polygon],color='k',zorder=3)
            polygon=patches.Polygon(xy,closed=True)
            p2=PatchCollection([polygon],color='yellow',zorder=3)
        else:
            polygon=patches.Polygon(xy0,closed=True)
            p1=PatchCollection([polygon],color='yellow',zorder=3)
            polygon=patches.Polygon(xy,closed=True)
            p2=PatchCollection([polygon],color='k',zorder=3)

    i=0
    for slot in schedule.slots:
        if hasattr(slot.block, 'target'):
            #full track
            if plottype=='sky':
                ax=plot_sky(slot.block.target, schedule.observer, stime,ax=ax,style_kwargs={'lw':1,'color':colors[i],'alpha':0.4})
            else: ax=plot(slot.block.target, schedule.observer, stime,brightness_shading=False,ax=ax,style_kwargs={'lw':1,'color':colors[i],'fmt':'-','alpha':0.4})

            #observing part
            if plottype=='sky': ax=plot_sky(slot.block.target, schedule.observer, slot.start+slot.duration*np.linspace(0, 1, 20),ax=ax,style_kwargs={'lw':3,'color':colors[i]})
            else: ax=plot(slot.block.target, schedule.observer, slot.start+slot.duration*np.linspace(0, 1, 20),brightness_shading=False,style_kwargs={'lw':3,'color':colors[i],'fmt':'-'})

            if slots and not plottype=='sky': plt.axvspan(slot.start.plot_date, slot.end.plot_date,fc=colors[i],ymin=0, ymax=91, lw=0, alpha=0.1)

            if index:
                altaz=schedule.observer.altaz(slot.start,slot.block.target)
                if plottype=='sky':
                    ax.text(altaz.az*(1/u.deg)*(np.pi/180.0),(91*u.deg-altaz.alt)*(1/u.deg),str(i+1),color=colors[i],horizontalalignment='right',verticalalignment='bottom')
                elif plottype=='alt':
                    ax.text(slot.start.plot_date,altaz.alt*(1/u.deg),str(i+1),color=colors[i],horizontalalignment='right',verticalalignment='bottom')
                else:
                    ax.text(slot.start.plot_date,altaz.secz,str(i+1),color=colors[i],horizontalalignment='right',verticalalignment='bottom')

            i+=1
    if moon:
        mi=np.where(moon_altaz.alt>0)
        if plottype=='sky':
            ax.plot((moon_altaz.az[mi]*(1/u.deg)*(np.pi/180.0)).value,((91*u.deg-moon_altaz.alt[mi])*(1/u.deg)).value,'o-',color='gray',alpha=0.8)
            moonx=0.2
            moony=0.85
        elif plottype=='alt':
            ax.plot(mtime[mi].plot_date,moon_altaz.alt[mi],'o-',color='gray',alpha=0.8)
            moonx=0.12
            moony=0.83
        else:
            ax.plot(mtime[mi].plot_date,moon_altaz.secz[mi],'o-',color='gray',alpha=0.8)
            moonx=0.14
            moony=0.81

        axM = ax.figure.add_axes([moonx, moony, 0.1*ax.figure.get_figheight()/ax.figure.get_figwidth(), 0.1])
        axM.set_yticklabels([])
        axM.set_xticklabels([])
        axM.grid(False)
        axM.set_axis_off()
        axM.set_xlim(-1,1)
        axM.set_ylim(-1,1)
        axM.figure.tight_layout()
        axM.add_collection(p1)
        axM.add_collection(p2)

    if not plottype=='sky':
        ax.set_xlim(schedule.start_time.plot_date,schedule.end_time.plot_date)
        ax.set_xlabel("Time from {0} [{1}]".format(schedule.start_time.datetime.date(), schedule.start_time.scale.upper()))
    if plottype=='alt':
        ax.set_ylim(0,91)
        ax.yaxis.set_major_locator(matplotlib.ticker.MultipleLocator(10))
    elif plottype=='airmass': ax.set_ylim(3,0.95)
    if legend:
        lines0, labels0 = ax.get_legend_handles_labels()
        lines=[]
        labels=[]
        for i,l in enumerate(labels0):
            if '_s' in l:
                tt=l[:l.rfind('_s')]
                s=l[l.rfind('_s'):]
            else:
                tt=l
                s=''

            if tt in objects0: name=objects0[tt]['full']['Target']+s
            else: name=l

            if not name in labels:
                labels.append(name)
                lines.append(lines0[i+1])
        if plottype=='sky': plt.legend(lines,labels,loc='lower center',bbox_to_anchor=(0.5, -0.4),fontsize=6,ncol=3)
        else: ax.legend(lines,labels,loc='center right',bbox_to_anchor=(1.4, 0.5),fontsize=7)

    plt.tight_layout()
    return ax

def plot_timeline(schedule,show_night=True,legend=False,index=True,objects0={}):
    '''plot timeslots in schedule'''
    import operator

    ax = plt.gca()
    if show_night:
        start = schedule.start_time.datetime

        # Calculate and order twilights and set plotting alpha for each
        twilights0 = [
            (schedule.observer.sun_set_time(Time(start), which='next',n_grid_points=10).datetime, 0.0,'sunset'),
            (schedule.observer.twilight_evening_civil(Time(start), which='next',n_grid_points=10).datetime, 0.1,'civil'),
            (schedule.observer.twilight_evening_nautical(Time(start), which='next',n_grid_points=10).datetime, 0.2,'nautic'),
            (schedule.observer.twilight_evening_astronomical(Time(start), which='next',n_grid_points=10).datetime, 0.3,'astro'),
            (schedule.observer.twilight_morning_astronomical(Time(start), which='next',n_grid_points=10).datetime, 0.4,'astro'),
            (schedule.observer.twilight_morning_nautical(Time(start), which='next',n_grid_points=10).datetime, 0.3,'nautic'),
            (schedule.observer.twilight_morning_civil(Time(start), which='next',n_grid_points=10).datetime, 0.2,'civil'),
            (schedule.observer.sun_rise_time(Time(start), which='next',n_grid_points=10).datetime, 0.1,'sunrise'),
        ]

        twilights=[]
        for t in twilights0:
            if not isinstance(t[0],np.ndarray): twilights.append(t)   #remove if not twilight

        twilights.sort(key=operator.itemgetter(0))
        for i, twi in enumerate(twilights[1:], 1):
            plt.axvspan(twilights[i - 1][0], twilights[i][0],ymin=0, ymax=1, color='grey', alpha=twi[1])

        for i,tw in enumerate(twilights):
            if i<4: plt.text(tw[0],0.02,tw[2],horizontalalignment='right',verticalalignment='bottom',fontsize=8,rotation='vertical')
            else: plt.text(tw[0],0.02,tw[2],horizontalalignment='left',verticalalignment='bottom',fontsize=8,rotation='vertical')

    i=0
    for slot in schedule.slots:
        if hasattr(slot.block, 'target'):
            plt.axvspan(slot.start.plot_date, slot.end.plot_date,fc=colors[i],ymin=0, ymax=1, lw=0, alpha=.6)
            if '_s' in slot.block.target.name: tt=slot.block.target.name[:slot.block.target.name.rfind('_s')]
            else: tt=slot.block.target.name
            #TODO!
            if tt in objects0: name=objects0[tt]['full']['Target']
            else: name=tt

            plt.axhline(3, color=colors[i], label=name)
            if index:
                c=matplotlib.colors.to_rgba(colors[i])
                ax.text((slot.start.plot_date+slot.end.plot_date)/2,0.5,str(i+1),color=(1-c[0],1-c[1],1-c[2],c[3]),horizontalalignment='center',verticalalignment='center')
            i+=1
        elif hasattr(slot.block, 'components'):
            plt.axvspan(slot.start.plot_date, slot.end.plot_date,ymin=0, ymax=1,color='k')
    plt.axhline(3, color='k', label='---Transitions---')
    plt.ylim(0,1)
    plt.yticks([])

    date_formatter = matplotlib.dates.DateFormatter('%H:%M')
    ax.xaxis.set_major_formatter(date_formatter)
    ax.xaxis.set_major_locator(matplotlib.dates.HourLocator())
    plt.setp(ax.get_xticklabels(), rotation=30, ha='right')
    ax.set_xlabel("Time from {0} [{1}]".format(schedule.start_time.datetime.date(), schedule.start_time.scale.upper()))
    ax.set_xlim(schedule.start_time.plot_date,schedule.end_time.plot_date)

    if legend: plt.legend(loc='center right',bbox_to_anchor=(1.4, 0.5),fontsize=7)
    plt.tight_layout()
    return ax

def plot_score(blocks,schedule,constraints,legend=False,index=True,objects0={}):
    ax = plt.gca()

    score=Scorer(blocks, schedule.observer, schedule,constraints).create_score_array()
    time=schedule.start_time+(schedule.end_time-schedule.start_time)*np.linspace(0,1,len(score[0]))

    ind=[]
    obj=[]
    ra=[]
    dec=[]
    maxscore=[]
    maxtime=[]
    obsdur=[]
    obsstart=[]
    obsend=[]
    for i in range(len(blocks)):
        if '_s' in blocks[i].target.name: tt=blocks[i].target.name[:blocks[i].target.name.rfind('_s')]
        else: tt=blocks[i].target.name

        if tt in objects0: ob=objects0[tt]['full']['Target']
        else: ob=tt

        if ob in obj: continue

        obj.append(ob)

        plt.plot(time.plot_date,score[i,:],color=colors[i],label=obj[-1])
        loc=np.argmax(score[i,:])
        if index: plt.text(time.plot_date[loc],score[i,loc],str(i+1),color=colors[i],horizontalalignment='right',verticalalignment='bottom')

        ind.append(i+1)

        ra.append(str(blocks[i].target.ra/15).replace('d',':').replace('m',':').replace('s',''))
        dec.append(str(blocks[i].target.dec).replace('d',':').replace('m',':').replace('s',''))
        maxscore.append(round(score[i,loc],3))
        if score[i,loc]>0: maxtime.append(time[loc].strftime('%Y-%m-%d %H:%M:%S'))
        else: maxtime.append('')

        obs=np.where(score[i,:]>0)[0]
        if len(obs)>0:
            obsdur.append(round((time[obs[-1]]-time[obs[0]]).sec/3600,2))
            obsstart.append(time[obs[0]].strftime('%Y-%m-%d %H:%M:%S'))
            obsend.append(time[obs[-1]].strftime('%Y-%m-%d %H:%M:%S'))
        else:
            obsdur.append(0)
            obsstart.append('')
            obsend.append('')


    ax.set_ylim(0,1.05)
    ax.set_ylabel('Score')
    date_formatter = matplotlib.dates.DateFormatter('%H:%M')
    ax.xaxis.set_major_formatter(date_formatter)
    ax.xaxis.set_major_locator(matplotlib.dates.HourLocator())
    plt.setp(ax.get_xticklabels(), rotation=30, ha='right')
    ax.set_xlabel("Time from {0} [{1}]".format(schedule.start_time.datetime.date(), schedule.start_time.scale.upper()))
    ax.set_xlim(schedule.start_time.plot_date,schedule.end_time.plot_date)

    if legend: plt.legend(loc='center right',bbox_to_anchor=(1.4, 0.5),fontsize=7)
    plt.tight_layout()

    tab=Table([ind,obj,ra,dec,maxscore,maxtime,obsdur,obsstart,obsend],names=['index','target','ra','dec','max_score','max_score time','observability duration (h)', 'observability start','observability end'])
    return ax,tab

def schedule_table(schedule,objects0={}):
    '''add altitude and airmass columns'''
    tab=schedule.to_table()
    air=[]
    alt=[]
    azm=[]
    prior=[]
    exp=[]
    n_exp=[]
    ra=[]
    dec=[]
    index=[]
    pos=[]
    target=[]
    mag=[]
    #notes=[]
    full=[]

    i=1
    for slot in schedule.slots:
        if hasattr(slot.block, 'target'):
            t=slot.block.start_time+slot.block.duration/2
            altaz=slot.block.observer.altaz(t, slot.block.target)
            alt.append(str(altaz.alt.degree.round(2)))
            air.append(str(altaz.secz.round(2)))
            azm.append(str(altaz.az.degree.round(2)))
            prior.append(str(slot.block.priority))
            n_exp.append(str(int(slot.block.number_exposures)))
            exp.append(str(int(round(slot.block.time_per_exposure.value))))
            ra.append(str(slot.block.target.ra/15).replace('d',':').replace('m',':').replace('s',''))
            dec.append(str(slot.block.target.dec).replace('d',':').replace('m',':').replace('s',''))
            index.append(str(i))
            if 'position' in slot.block.configuration:
                if len(slot.block.configuration['position'])>0: pos.append(slot.block.configuration['position'])
                else: pos.append('-')
            else: pos.append('')
            if '_s' in slot.block.target.name: tt=slot.block.target.name[:slot.block.target.name.rfind('_s')]
            else: tt=slot.block.target.name
            #TODO!
            if tt in objects0:
                mag.append(objects0[tt]['mag'])
                target.append(objects0[tt]['full']['Target'])
                #if pd.isna(objects0[tt]['full']['Remarks']): notes.append('')
                #else: notes.append(objects0[tt]['full']['Remarks'])
                full.append(objects0[tt]['full'].to_dict())
            else:
                mag.append('')
                target.append(tt)
                #notes.append('')
                full.append({})
            i+=1
        elif hasattr(slot.block, 'components'):
            alt.append('')
            air.append('')
            azm.append('')
            prior.append('')
            n_exp.append('')
            exp.append('')
            ra.append('')
            dec.append('')
            index.append('')
            pos.append('')
            target.append('TransitionBlock')
            mag.append('')
            #notes.append('')
            full.append({})

    full_keys=set([key for x in full for key in x])    #list of all additional columns
    fulls={key:[] for key in full_keys}
    for x in full:
        for key in full_keys:
            if key in x:
                if pd.isna(x[key]): fulls[key].append('')
                else: fulls[key].append(x[key])
            else: fulls[key].append('')

    tab.add_column(alt,name='altitude')
    tab.add_column(air,name='airmass')
    tab.add_column(azm,name='azimut')
    tab.add_column(prior,name='priority')
    tab.add_column(exp,name='exposure (seconds)')
    tab.add_column(n_exp,name='number exposures')
    tab.add_column(mag,name='mag')
    #tab.add_column(notes,name='notes')
    #adding all additional columns
    for key in full_keys:
        tab.add_column(fulls[key],name='_'+key)

    if len(''.join(pos))>0: tab.add_column(pos,name='position')
    tab.add_column(index,name='index',index=0)
    tab['duration (minutes)']=np.round(tab['duration (minutes)'],2)
    tab['ra']=ra
    tab['dec']=dec
    tab['target']=target
    return tab

def batch(schedule,objects0={}):
    #{"name":"alp Lyr","v_mag":"0.03","note":"","ra":"18:36","de":"38:47","type":"target","exptime":"30","caltime":"360","iodinecell":false,"count_repeat":null,"count_of_pulses":null,"fiber":0,"spectral_range":null,"ga_sf":null,"start":"18:50","ha":"1.1-1.1","alt":"74-74"}

    output=[]
    for slot in schedule.slots:
        if hasattr(slot.block, 'target'):
            tmp={}
            tmp['name']=slot.block.target.name
            if slot.block.target.name in objects0: tmp['v_mag']=objects0[slot.block.target.name]['mag']
            else: tmp['v_mag']=None
            tmp['note']=''     #TODO!
            tmp['ra']=str(slot.block.target.ra/15).replace('d',':').replace('m',':').replace('s','').split('.')[0]
            tmp['de']=str(slot.block.target.dec).replace('d',':').replace('m',':').replace('s','').split('.')[0]
            tmp['type']='target'
            tmp['exptime']=str(int(round(slot.block.time_per_exposure.value)))
            tmp['caltime']='360'
            tmp['iodinecell']=False
            tmp['count_repeat']=str(int(slot.block.number_exposures))
            tmp['count_of_pulses']=None
            tmp['fiber']=0
            tmp['spectral_range']=None
            tmp['ga_sf']=None
            tmp['start']=slot.block.start_time.strftime('%H:%M')
            altaz0=slot.block.observer.altaz(slot.block.start_time, slot.block.target)
            altaz1=slot.block.observer.altaz(slot.block.end_time, slot.block.target)
            tmp['ha']=str(slot.block.observer.target_hour_angle(slot.block.start_time, slot.block.target).hour.round(1))+'-'+str(slot.block.observer.target_hour_angle(slot.block.end_time, slot.block.target).hour.round(1))
            tmp['alt']=str(int(round(altaz0.alt.degree)))+'-'+str(int(round(altaz1.alt.degree)))
            output.append(tmp)
    return output



class StdPriorityScheduler(PriorityScheduler):
    """
    A scheduler that optimizes a prioritized list.  That is, it
    finds the best time for each ObservingBlock, in order of priority.
    """

    def __init__(self, *args, **kwargs):
        """

        """
        super(StdPriorityScheduler, self).__init__(*args, **kwargs)

    def _make_schedule(self, blocks):
        # Combine individual constraints with global constraints, and
        # retrieve priorities from each block to define scheduling order

        _all_times = []
        _block_priorities = np.zeros(len(blocks))

        # make sure we don't schedule below the horizon
        if self.constraints is None:
            self.constraints = [AltitudeConstraint(min=0 * u.deg)]
        else:
            self.constraints.append(AltitudeConstraint(min=0 * u.deg))

        for i, b in enumerate(blocks):
            b._duration_offsets = u.Quantity([0 * u.second, b.duration / 2, b.duration])
            _block_priorities[i] = b.priority
            _all_times.append(b.duration)
            b.observer = self.observer

        # Define a master schedule
        # Generate grid of time slots, and a mask for previous observations

        time_resolution = self.time_resolution
        times = time_grid_from_range([self.schedule.start_time, self.schedule.end_time],
                                     time_resolution=time_resolution)

        # generate the score arrays for all of the blocks
        scorer = Scorer(blocks, self.observer, self.schedule,
                        global_constraints=self.constraints)
        score_array = scorer.create_score_array(time_resolution)

        # Sort the list of blocks by priority
        sorted_indices = np.argsort(_block_priorities, kind='mergesort')

        scheduled_std=[]   #priorites of already sheduled std (<1) - only one per night and priority
        unscheduled_blocks = []
        # Compute the optimal observation time in priority order
        for i in sorted_indices:
            b = blocks[i]
            if b.priority in scheduled_std: continue    #priorites of already sheduled std (<1) - only one per night and priority
            # Compute possible observing times by combining object constraints
            # with the master open times mask
            constraint_scores = score_array[i]

            # Add up the applied constraints to prioritize the best blocks
            # And then remove any times that are already scheduled
            is_open_time = self._get_filled_indices(times)
            constraint_scores[~is_open_time] = 0

            # Select the most optimal time

            # calculate the number of time slots needed for this exposure
            _stride_by = int(np.ceil(float(b.duration / time_resolution)))

            # Stride the score arrays by that number
            _strided_scores = stride_array(constraint_scores, _stride_by)

            # Collapse the sub-arrays
            # (run them through scorekeeper again? Just add them?
            # If there's a zero anywhere in there, def. have to skip)
            good = np.all(_strided_scores > 1e-5, axis=1)
            sum_scores = np.zeros(len(_strided_scores))
            sum_scores[good] = np.sum(_strided_scores[good], axis=1)

            if np.all(constraint_scores == 0) or np.all(~good):
                # No further calculation if no times meet the constraints
                _is_scheduled = False
            else:
                # schedulable in principle, provided the transition
                # does not prevent us from fitting it in.
                # loop over valid times and see if it fits
                # TODO: speed up by searching multiples of time resolution?
                for idx in np.argsort(-sum_scores, kind='mergesort'):
                    if sum_scores[idx] <= 0.0:
                        # we've run through all optimal blocks
                        _is_scheduled = False
                        break
                    try:
                        start_time_idx = idx
                        new_start_time = times[start_time_idx]
                        # attempt to schedule block
                        _is_scheduled = self.attempt_insert_block(b, new_start_time, start_time_idx)
                        if _is_scheduled:
                            break
                    except IndexError:
                        # idx can extend past end of _strided_open_time
                        _is_scheduled = False
                        break

            if not _is_scheduled:
                unscheduled_blocks.append(b)
            elif b.priority<1: scheduled_std.append(b.priority)    #priorites of already sheduled std (<1) - only one per night and priority

        return self.schedule


class ModifAltitudeConstraint(AltitudeConstraint):
    """
    Constrain the altitude of the target.

    .. note::
        This can misbehave if you try to constrain negative altitudes, as
        the `~astropy.coordinates.AltAz` frame tends to mishandle negative


    Parameters
    ----------
    min : `~astropy.units.Quantity` or `None`
        Minimum altitude of the target (inclusive). `None` indicates no limit.
    max : `~astropy.units.Quantity` or `None`
        Maximum altitude of the target (inclusive). `None` indicates no limit.
    boolean_constraint : bool
        If True, the constraint is treated as a boolean (True for within the
        limits and False for outside).  If False, the constraint returns a
        float on [0, 1], where 0 is the min altitude and 1 is the max.
    """

    def __init__(self, min=None, max=None, boolean_constraint=True):
        super(ModifAltitudeConstraint, self).__init__(min, max, boolean_constraint)

    def compute_constraint(self, times, observer, targets):
        cached_altaz = _get_altaz(times, observer, targets)
        alt = cached_altaz['altaz'].alt
        if self.boolean_constraint:
            lowermask = self.min <= alt
            uppermask = alt <= self.max
            return lowermask & uppermask
        else:
            #scaling according to culmination altitude
            if observer.latitude>=0*u.deg: maxalt=90*u.deg-observer.latitude+targets.dec
            else: maxalt=180*u.deg-(90*u.deg-observer.latitude+targets.dec)
            maxalt[maxalt<=self.min]=90*u.deg
            maxalt[maxalt>90*u.deg]=180*u.deg-maxalt[maxalt>90*u.deg]
            maxalts=np.full(alt.shape,np.float64(maxalt))*u.deg
            return max_best_rescale(alt, self.min, maxalts)

class ModifAirmassConstraint(AirmassConstraint):
    """
    Constrain the airmass of a target.

    In the current implementation the airmass is approximated by the secant of
    the zenith angle.

    .. note::
        The ``max`` and ``min`` arguments appear in the order (max, min)
        in this initializer to support the common case for users who care
        about the upper limit on the airmass (``max``) and not the lower
        limit.

    Parameters
    ----------
    max : float or `None`
        Maximum airmass of the target. `None` indicates no limit.
    min : float or `None`
        Minimum airmass of the target. `None` indicates no limit.
    boolean_contstraint : bool

    Examples
    --------
    To create a constraint that requires the airmass be "better than 2",
    i.e. at a higher altitude than airmass=2::

        AirmassConstraint(2)
    """

    def __init__(self, max=None, min=1, boolean_constraint=True):
        super(ModifAirmassConstraint, self).__init__(max, min, boolean_constraint)

    def compute_constraint(self, times, observer, targets):
        cached_altaz = _get_altaz(times, observer, targets)
        secz = cached_altaz['altaz'].secz.value
        if self.boolean_constraint:
            if self.min is None and self.max is not None:
                mask = secz <= self.max
            elif self.max is None and self.min is not None:
                mask = self.min <= secz
            elif self.min is not None and self.max is not None:
                mask = (self.min <= secz) & (secz <= self.max)
            else:
                raise ValueError("No max and/or min specified in "
                                 "AirmassConstraint.")
            return mask
        else:
            if self.max is None:
                raise ValueError("Cannot have a float AirmassConstraint if max is None.")
            else:
                mx = self.max

            #scaling according to culmination altitude
            if observer.latitude>=0*u.deg: maxalt=90*u.deg-observer.latitude+targets.dec
            else: maxalt=180*u.deg-(90*u.deg-observer.latitude+targets.dec)
            maxalt[maxalt<=(90-np.rad2deg(np.arccos(1/self.max)))*u.deg]=90*u.deg
            maxalt[maxalt>90*u.deg]=180*u.deg-maxalt[maxalt>90*u.deg]
            maxalts=np.full(secz.shape,np.float64(maxalt))

            mi = 1/np.cos(np.deg2rad(90-maxalts)) if self.min is None else self.min
            # values below 1 should be disregarded
            return min_best_rescale(secz, mi, mx, less_than_min=0)

class ModifMoonSeparationConstraint(MoonSeparationConstraint):
    """
    Constrain the distance between the Earth's moon and some targets.

    From Konkoly RC80 Scheduler
    """

    def __init__(self, min=None, max=None, ephemeris=None,
                 boolean_constraint=True):
        """
        Parameters
        ----------
        min : `~astropy.units.Quantity` or `None` (optional)
            Minimum acceptable separation between moon and target (inclusive).
            `None` indicates no limit.
        max : `~astropy.units.Quantity` or `None` (optional)
            Maximum acceptable separation between moon and target (inclusive).
            `None` indicates no limit.
        ephemeris : str, optional
            Ephemeris to use.  If not given, use the one set with
            ``astropy.coordinates.solar_system_ephemeris.set`` (which is
            set to 'builtin' by default).
        """
        super(ModifMoonSeparationConstraint, self).__init__(min, max, ephemeris)
        self.boolean_constraint = boolean_constraint

    def compute_constraint(self, times, observer, targets):
        # print(f"targets: {targets}")
        # print(f"times: {times}")
        # removed the location argument here, which causes small <1 deg
        # innacuracies, but it is needed until astropy PR #5897 is released
        # which should be astropy 1.3.2
        moon = get_moon(times,
                        ephemeris=self.ephemeris)

        # note to future editors - the order matters here
        # moon.separation(targets) is NOT the same as targets.separation(moon)
        # the former calculates the separation in the frame of the moon coord
        # which is GCRS, and that is what we want.
        moon_separation = moon.separation(targets)
        # print(f"moon_separation, len objects:\n {moon_separation}")

        if self.min is None and self.max is not None:
            mask = self.max >= moon_separation
        elif self.max is None and self.min is not None:
            mask = self.min <= moon_separation
        elif self.min is not None and self.max is not None:
            mask = ((self.min <= moon_separation) &
                    (moon_separation <= self.max))
        else:
            raise ValueError("No max and/or min specified in "
                             "MoonSeparationConstraint.")

        #fix scale of scores
        mx = 2*self.min if self.max is None else self.max

        if self.boolean_constraint:
            return mask
        else:
            return max_best_rescale(moon_separation, self.min, mx)

