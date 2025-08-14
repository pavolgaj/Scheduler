import logging.handlers
from flask import Flask, redirect, url_for, render_template, request, send_file,session,flash, make_response
from flask_caching import Cache
from astroquery.simbad import Simbad
from astroquery.vizier import Vizier
from astroquery.ipac.nexsci.nasa_exoplanet_archive import NasaExoplanetArchive
import os
from send_mail import SendMail
import traceback
import csv
import io
import glob
import hashlib
import json
import re
import uuid
import base64
from datetime import datetime,timezone,timedelta
import requests

from make_stats import make_stats

import matplotlib
matplotlib.use('Agg')

import matplotlib.path as mplPath

from scipy import interpolate
from scheduler import *

import logging

import gc

# main file for web interface for platospec E152 scheduler and objects DB
# (c) Pavol Gajdos, 2024

app = Flask(__name__,static_url_path='/scheduler')
app.secret_key = 'e152logs'  # Used to secure the session

app.config['TRAP_HTTP_EXCEPTIONS']=True

# Disable trailing slash strictness for all routes
app.url_map.strict_slashes = False

#create cache
cache = Cache(app,config={'CACHE_TYPE': 'RedisCache',"CACHE_DEFAULT_TIMEOUT": 3600,'CACHE_REDIS_URL': 'redis://localhost:6379/0'})

#md5 hash of passwords
adminPassword = '21232f297a57a5a743894a0e4a801fc3'  # admin - CHANGE!
password='ee11cbb19052e40b07aac0ca060c23ee'         # user - CHANGE!
obs_lon=-70.739     #longitude of observatory

#configure for logger
logHandler=logging.FileHandler('errors.log')
logHandler.setLevel(logging.DEBUG)
app.logger.addHandler(logHandler)

errMail=SendMail(cc='')
errMail.load_cfg('mail/conf-error')
mailLog=logging.handlers.SMTPHandler(mailhost=errMail.mail['smtp'],fromaddr=errMail.mail['from'],toaddrs=errMail.mail['to'],subject=errMail.mail['subject'])
mailLog.setLevel(logging.ERROR)
app.logger.addHandler(mailLog)

if not os.path.isdir('db'): os.mkdir('db')
if not os.path.isdir('schedules'): os.mkdir('schedules')

@app.route("/health")
def health():
    '''check if everything works -> monitoring on server side'''
    #if not access to logs dir (symlink to /mnt/ESO/logs) -> error code 555
    if not os.path.isdir('static/logs'): 
        return 'Path not found!', 555
    try:
        if len(glob.glob('static/logs/*_log.csv'))==0:
            return 'Path not found!', 555
    except:
        return 'Path not found!', 555
    
    gc.collect()
    return 'OK'

if not app.debug:
    #user error pages
    @app.errorhandler(404)
    def page_not_found(e):
        return render_template('error.html',error=e.description), 404

    @app.errorhandler(403)
    def page_forbidden(e):
        return render_template('error.html',error=e.description), 403

    @app.errorhandler(500)
    def server_error(e):
        app.logger.exception(e)
        return render_template('error.html',error=traceback.format_exc().splitlines()[-1]), 500

    @app.errorhandler(Exception)
    def error(e):
        app.logger.exception(e)
        return render_template('error.html',error=traceback.format_exc().splitlines()[-1]), 500

@app.route("/scheduler")
def main():
    '''index page -> just frames for subpages'''
    gc.collect()
    return render_template('index.html')

@app.route('/scheduler/intro')
def intro():
    '''some introduction (first page)'''
    gc.collect()
    return render_template('intro.html')

@app.route('/scheduler/admins')
def admins():
    '''mail to admins'''
    send=SendMail('')
    mails=send.mail["to"]
    return redirect('mailto:'+mails+'?subject=Scheduler DB') 
@app.route('/scheduler/login', methods=['GET', 'POST'])
def login():
    '''login page'''
    if request.method == 'POST':
        # Get the password from the form
        entered_password = request.form.get('password')
        hashpass=hashlib.md5(entered_password.encode()).hexdigest()   #make md5 hash of input pass

        # Check if the entered password matches (compare hash)
        if hashpass == adminPassword:
            # Set session variable - admin login
            session['logged_in'] = 'admin'
            return redirect(request.args.get('next'))  #redirect to wanted page
        elif hashpass == password:
            # Set session variable - user login
            session['logged_in'] = 'user'
            if 'admin' in request.args.get('next'): flash('Incorrect password. Please try again.')    
            else: return redirect(request.args.get('next'))  #redirect to wanted page
        else:
            flash('Incorrect password. Please try again.')

    gc.collect()
    return render_template('login.html')

def dms(dd):
    #convert value in degree to deg, min,sec
    mult = -1 if dd < 0 else 1
    mnt,sec = divmod(abs(dd)*3600, 60)
    deg,mnt = divmod(mnt, 60)
    return int(mult*deg), int(mnt), sec

def is_int(text):
    #check if text is int
    try: int(text)
    except ValueError: return False
    return True

def is_float(text):
    #check if text is float
    try: float(text)
    except ValueError: return False
    return True


def check_dns(text,min=None,max=None):
    #check text if is in DMS format
    tmp=text.replace(':',' ').split()
    if not len(tmp)==3: return False
    if not is_int(tmp[0]): return False
    if not is_int(tmp[1]): return False
    if not is_float(tmp[2]): return False
    if min is not None:
        if int(tmp[0])<min: return False
    if max is not None: 
        if int(tmp[0])>max: return False
    if int(tmp[1])>=60 or int(tmp[1])<0: return False
    if float(tmp[2])>=60 or float(tmp[2])<0: return False    
    return True    


#header for obj in DB
header='Target,RA,DEC,Mag,Period,Epoch,ExpTime,Number,Nights,Priority,Type,Remarks,MoonPhase,StartPhase,EndPhase,StartDate,EndDate,Conditions,Frequency,OtherRequests,Supervisor'
header+='\n'

# Route for displaying the form
@app.route('/scheduler/new', methods=['GET', 'POST'])
def new():
    '''adding new obj to DB'''
    if not session.get('logged_in'):
        return redirect(url_for('login', next=request.path))
    
    groups=["Exoplanet","Eclipsing binary","Flaring star","Pulsating star","RV Standard","SpecPhot Standard"]
    
    if os.path.isfile('db/objects.csv'):
        f=open('db/objects.csv','r')
        reader = csv.DictReader(f)
        for obj in reader: 
            if not obj['Type'] in groups: groups.append(obj['Type'])
        f.close()
        
    
    if request.method == 'POST':
        errors = {}

        #get data from form
        name = request.form['name'].strip()
        ra = request.form['ra'].strip()
        dec = request.form['dec'].strip()
        mag = request.form['mag'].strip()
        per = request.form['per'].strip()
        t0 = request.form['t0'].strip()

        exp = request.form['exp'].strip()
        number = request.form['number']
        night = request.form['night']
        series = (request.form.get('series')=='checked')
        simcal=request.form['simcal']
        ic = (request.form.get('ic')=='checked')
        readout=request.form['readout']
        phot = (request.form.get('phot')=='checked')
        phot_input = request.form['phot_input'].strip()
        remarks = request.form['remarks'].strip()

        prior = request.form['prior']
        group = request.form['group'].strip()

        moon = (request.form.get('moon')=='checked')
        moon_input = request.form['moon_input'].strip()
        phase = (request.form.get('phase')=='checked')
        phase_start = request.form['phase_start'].strip()
        phase_end = request.form['phase_end'].strip()
        time = (request.form.get('time')=='checked')
        time_start = request.form['time_start']
        time_end = request.form['time_end']
        condi = request.form['condi']  
        freq = request.form['freq']  
        other = request.form['other'].strip()        

        supervis = request.form['supervis'].strip()
        progID = request.form['progID'].strip()
        email = request.form['email'].strip()
        mess=request.form['mess'].strip()

        if 'simbad' in request.form:
            #search obj in simbad - ra,dec,mag
            if name:
                ra=''
                dec=''
                mag=''
               
                try:
                    #astroquery>=0.4.8
                    Simbad.add_votable_fields("allfluxes")
                except KeyError:
                    #astroquery==0.4.7
                    Simbad.add_votable_fields("flux(V)",'flux(g)','flux(R)','flux(r)','flux(I)','flux(i)','flux(B)','flux(u)','flux(U)','flux(G)','flux(J)','flux(z)','flux(H)','flux(K)')
                
                result_table = Simbad.query_object(name)
                if result_table is None:
                    Simbad.reset_votable_fields()
                    result_table = Simbad.query_object(name)  #try to search without mag
                    if result_table is None: errors['name'] = 'Object "'+name+'" NOT found in Simbad!'
                    else:
                        result_table=result_table[0]
                        # format ra/dec with leading zero
                        if 'ra' in result_table.colnames:
                            #astroquery>=0.4.8
                            ra='%02d %02d %05.2f' %dms(result_table['ra']/15)
                            dec='%02d %02d %05.2f' %dms(result_table['dec'])
                        else:
                            #astroquery==0.4.7
                            ra=result_table['RA']
                            dec=result_table['DEC'].replace('+','')
                        mag=''
                elif len(result_table)==0:
                    Simbad.reset_votable_fields()
                    result_table = Simbad.query_object(name)  #try to search without mag
                    if len(result_table)==0: errors['name'] = 'Object "'+name+'" NOT found in Simbad!'
                    else:
                        result_table=result_table[0]
                        if 'ra' in result_table.colnames:
                            #astroquery>=0.4.8
                            ra='%02d %02d %05.2f' %dms(result_table['ra']/15)
                            dec='%02d %02d %05.2f' %dms(result_table['dec'])
                        else:
                            #astroquery==0.4.7
                            ra=result_table['RA']
                            dec=result_table['DEC'].replace('+','')
                        mag=''
                else:
                    result_table=result_table[0]
                    if 'ra' in result_table.colnames:
                        #astroquery>=0.4.8
                        ra='%02d %02d %05.2f' %dms(result_table['ra']/15)
                        dec='%02d %02d %05.2f' %dms(result_table['dec'])
                        #select used mag (closest to V)
                        if not hasattr(result_table['V'],'mask'): mag=round(result_table['V'],2)
                        elif not hasattr(result_table['g'],'mask'): mag=round(result_table['g'],2)
                        elif not hasattr(result_table['R'],'mask'): mag=round(result_table['R'],2)
                        elif not hasattr(result_table['r'],'mask'): mag=round(result_table['r'],2)
                        elif not hasattr(result_table['I'],'mask'): mag=round(result_table['I'],2)
                        elif not hasattr(result_table['i'],'mask'): mag=round(result_table['i'],2)
                        elif not hasattr(result_table['B'],'mask'): mag=round(result_table['B'],2)
                        elif not hasattr(result_table['u'],'mask'): mag=round(result_table['u'],2)
                        elif not hasattr(result_table['U'],'mask'): mag=round(result_table['U'],2)
                        elif not hasattr(result_table['G'],'mask'): mag=round(result_table['G'],2)
                        elif not hasattr(result_table['J'],'mask'): mag=round(result_table['J'],2)
                        elif not hasattr(result_table['z'],'mask'): mag=round(result_table['z'],2)
                        elif not hasattr(result_table['H'],'mask'): mag=round(result_table['H'],2)
                        elif not hasattr(result_table['K'],'mask'): mag=round(result_table['K'],2)
                    else:
                        #astroquery==0.4.7
                        ra=result_table['RA']
                        dec=result_table['DEC'].replace('+','')
                        #select used mag (closest to V)
                        if not hasattr(result_table['FLUX_V'],'mask'): mag=round(result_table['FLUX_V'],2)
                        elif not hasattr(result_table['FLUX_g'],'mask'): mag=round(result_table['FLUX_g'],2)
                        elif not hasattr(result_table['FLUX_R'],'mask'): mag=round(result_table['FLUX_R'],2)
                        elif not hasattr(result_table['FLUX_r'],'mask'): mag=round(result_table['FLUX_r'],2)
                        elif not hasattr(result_table['FLUX_I'],'mask'): mag=round(result_table['FLUX_I'],2)
                        elif not hasattr(result_table['FLUX_i'],'mask'): mag=round(result_table['FLUX_i'],2)
                        elif not hasattr(result_table['FLUX_B'],'mask'): mag=round(result_table['FLUX_B'],2)
                        elif not hasattr(result_table['FLUX_u'],'mask'): mag=round(result_table['FLUX_u'],2)
                        elif not hasattr(result_table['FLUX_U'],'mask'): mag=round(result_table['FLUX_U'],2)
                        elif not hasattr(result_table['FLUX_G'],'mask'): mag=round(result_table['FLUX_G'],2)
                        elif not hasattr(result_table['FLUX_J'],'mask'): mag=round(result_table['FLUX_J'],2)
                        elif not hasattr(result_table['FLUX_z'],'mask'): mag=round(result_table['FLUX_z'],2)
                        elif not hasattr(result_table['FLUX_H'],'mask'): mag=round(result_table['FLUX_H'],2)
                        elif not hasattr(result_table['FLUX_K'],'mask'): mag=round(result_table['FLUX_K'],2)
            else: errors['name'] = 'Name is required.'
            
            gc.collect()
            return render_template('add.html', name=name, ra=ra, dec=dec, mag=mag, per=per, t0=t0, exp=exp, number=number, night=night, series=series, ic=ic, phot=phot, phot_input=phot_input, prior=prior, group=group, moon=moon, moon_input=moon_input, phase=phase, phase_start=phase_start, phase_end=phase_end, time=time, time_start=time_start, time_end=time_end, other=other, supervis = supervis, email=email, mess=mess, errors=errors, remarks=remarks, simcal=simcal,readout=readout,condi=condi,progID=progID,groups=groups,freq=freq)
        
        elif 'exofop' in request.form:
            #search obj in exofop - ra,dec,mag
            if name:
                ra=''
                dec=''
                mag=''
                
                urlID = 'https://exofop.ipac.caltech.edu/tess/gototicid.php?target='+name+'&json'
                urlData = 'https://exofop.ipac.caltech.edu/tess/target.php?id='
                response = requests.get(urlID)
                
                if response.status_code == 200:
                    dicID=json.loads(response.content)
                    if not dicID['status']=='OK':
                        errors['name'] = 'Object "'+name+'" NOT found in ExoFOP!'
                    else:
                        tic=dicID['TIC']

                        response = requests.get(urlData+tic+'&json')
                        
                        if response.status_code == 200:
                            data=json.loads(response.content)
                            if not 'coordinates' in data:
                                errors['name'] = 'Object "'+name+'" NOT found in ExoFOP!'
                            else:
                                ra='%02d %02d %05.2f' %dms(float(data['coordinates']['ra'])/15)
                                dec='%02d %02d %05.2f' %dms(float(data['coordinates']['dec']))

                                if 'magnitudes' in data:
                                    mags={x['band']: x['value'] for x in data['magnitudes']}
                                    if 'V' in mags: mag=mags['V']
                                    elif 'Kepler' in mags: mag=mags['Kepler']
                                    elif 'Gaia' in mags: mag=mags['Gaia']
                                    elif 'TESS' in mags: mag=mags['TESS']
                                    elif 'B' in mags: mag=mags['B']
                                    elif 'J' in mags: mag=mags['J']
                                    elif 'H' in mags: mag=mags['H']
                                    elif 'K' in mags: mag=mags['K']
                                    else: mag=''                        
                        
                        else: errors['name'] = 'Object "'+name+'" NOT found in ExoFOP!'
                else:
                    errors['name'] = 'Object "'+name+'" NOT found in ExoFOP!'
            
            else: errors['name'] = 'Name is required.'
            
            gc.collect()
            return render_template('add.html', name=name, ra=ra, dec=dec, mag=mag, per=per, t0=t0, exp=exp, number=number, night=night, series=series, ic=ic, phot=phot, phot_input=phot_input, prior=prior, group=group, moon=moon, moon_input=moon_input, phase=phase, phase_start=phase_start, phase_end=phase_end, time=time, time_start=time_start, time_end=time_end, other=other, supervis = supervis, email=email, mess=mess, errors=errors, remarks=remarks, simcal=simcal,readout=readout,condi=condi,progID=progID,groups=groups,freq=freq)
        
        elif 'vsx' in request.form:
            #search P,t0 in VSX cat
            if name:
                per=''
                t0=''
                catn=list(Vizier.find_catalogs('vsx').keys())[0]
                vsx=Vizier(catalog=catn, columns=["Epoch","Period"])
                vsx.ROW_LIMIT = -1

                rV=vsx.query_object(name)
                if rV is None: errors['name'] = 'Object "'+name+'" NOT found in VSX!'
                elif len(rV)==0: errors['name'] = 'Object "'+name+'" NOT found in VSX!'
                else:
                    per=float(rV[0][0]['Period'])
                    if str(per)=='nan': per=''
                    if 'Epoch' in rV[0][0].colnames: t0=float(rV[0][0]['Epoch'])
                    else: t0=float(rV[0][0]['_tab1_15'])
                    if str(t0)=='nan': t0=''
            else: errors['name'] = 'Name is required.'
            
            gc.collect()
            return render_template('add.html', name=name, ra=ra, dec=dec, mag=mag, per=per, t0=t0, exp=exp, number=number, night=night, series=series, ic=ic, phot=phot, phot_input=phot_input, prior=prior, group=group, moon=moon, moon_input=moon_input, phase=phase, phase_start=phase_start, phase_end=phase_end, time=time, time_start=time_start, time_end=time_end, other=other, supervis = supervis, email=email, mess=mess, errors=errors,remarks=remarks, simcal=simcal,readout=readout,condi=condi,progID=progID,groups=groups,freq=freq)
        elif 'exoarch' in request.form:
            #search P,t0 in exoplanet archive
            if name:
                per=''
                t0=''
                ea=NasaExoplanetArchive.query_object(name,table="pscomppars", select="pl_name,pl_orbper,pl_tranmid,pl_orbtper",regularize=True)

                if ea is None: errors['name'] = 'Object "'+name+'" NOT found in ExoArchive!'
                elif len(ea)==0: errors['name'] = 'Object "'+name+'" NOT found in ExoArchive!'
                else:
                    #select first row in data -> first planet if more
                    per=float(ea[0]['pl_orbper'].value)
                    if str(per)=='nan': per=''
                    if str(ea[0]['pl_tranmid'].value)=='nan': t0=float(ea[0]['pl_orbtper'].value)
                    else: t0=float(ea[0]['pl_tranmid'].value)
                    if str(t0)=='nan': t0=''
            else: errors['name'] = 'Name is required.'
            
            gc.collect()
            return render_template('add.html', name=name, ra=ra, dec=dec, mag=mag, per=per, t0=t0, exp=exp, number=number, night=night, series=series, ic=ic, phot=phot, phot_input=phot_input, prior=prior, group=group, moon=moon, moon_input=moon_input, phase=phase, phase_start=phase_start, phase_end=phase_end, time=time, time_start=time_start, time_end=time_end, other=other, supervis = supervis, email=email, mess=mess, errors=errors, remarks=remarks, simcal=simcal,readout=readout,condi=condi,progID=progID,groups=groups,freq=freq)



        elif 'submit' in request.form:
            #send form = save obj in DB
            
            #make some checks of format
            if not name:
                errors['name'] = 'Name is required.'
            if not ra:
                errors['ra'] = 'RA is required.'
            else:
                if not check_dns(ra,min=0,max=23): errors['ra'] = 'Wrong RA format.'
            if not dec:
                errors['dec'] = 'DEC is required.'
            else:
                if not check_dns(dec,min=-89,max=89): errors['dec'] = 'Wrong DEC format.'
            if not exp:
                errors['exp'] = 'Exp. time is required.'
            if not series and not number:
                errors['number'] = 'Number of exp. is required.'
            if phot and not phot_input:
                errors['phot'] = 'Photometry parameters are required.'
            if moon and not moon_input:
                errors['moon'] = 'Moon phase is required.'
            if per:
                if float(per)<=0: errors['per']='Period has to be positive.'
            if t0:
                if float(t0)<2000000: errors['t0']='Epoch has to be in full JD.'    
            if phase:
                if not (phase_start or phase_end): errors['phase'] = 'Phase limits are required.'
                if not per: errors['per'] = 'Period is required.'
                if not t0: errors['t0'] = 'Epoch is required.'
            if time and not (time_start or time_end): errors['time'] = 'Date limits are required.'

            if not supervis and group not in ['RV Standard','SpecPhot Standard']:
                errors['supervis'] = 'Supervisor is required.'
            if not progID and group not in ['RV Standard','SpecPhot Standard']:
                errors['progID'] = 'Program ID is required.'                  
            else:  
                #TODO check?  
                f=open('db/progID.json','r')
                ids=json.load(f)
                f.close()       
                
                #if progID not in ids: errors['progID'] = 'Program ID is incorrect/unknown.'        
            
            if not email:
                errors['email'] = 'Email is required.'


            if errors:
                gc.collect()
                
                return render_template('add.html', name=name, ra=ra, dec=dec, mag=mag, per=per, t0=t0, exp=exp, number=number, night=night, series=series, ic=ic, phot=phot, phot_input=phot_input, prior=prior, group=group, moon=moon, moon_input=moon_input, phase=phase, phase_start=phase_start, phase_end=phase_end, time=time, time_start=time_start, time_end=time_end, other=other, supervis = supervis, email=email, mess=mess, errors=errors, remarks=remarks, simcal=simcal,readout=readout,condi=condi,progID=progID,groups=groups)

            #set priority for standards
            if group=='RV Standard': 
                progID=''    #TODO?
                prior='0.1'
                if ic: prior='0.2'
                supervis='Standards'
                night=''
                condi=''
                freq=''
            elif group=='SpecPhot Standard': 
                progID=''
                prior='0.5'
                if ic: prior='0.6'
                supervis='Standards'
                night=''
                condi=''
                freq=''

            name=name.replace('*','').strip()
            # save the data to a database
            tmp='"'+name+'"'+','
            tmp+=ra+','
            tmp+=dec+','
            tmp+=mag+','
            tmp+=per+','
            tmp+=t0+','
            tmp+=exp+','
            if series: tmp+='series,'
            else: tmp+=number+','
            tmp+=night+','
            tmp+=prior+','
            tmp+='"'+group+'"'+','
            notes=''
            notes+=readout+'; '
            if ic: notes+='IC (FE); '
            if simcal=='thar': notes+='sim. ThAr; '
            elif simcal=='ic': notes+='sim. IC (CU); '
            if phot: notes+='photometry: '+phot_input+'; '
            notes+=remarks
            if len(notes)>0: notes='"'+notes+'"'
            tmp+=notes+','
            if moon: tmp+=moon_input+','
            else: tmp+=','
            if phase:
                tmp+=phase_start+','
                tmp+=phase_end+','
            else: tmp+=',,'
            if time:
                if time_start: tmp+=time_start+' 00:00:00,'
                else: tmp+=','
                if time_end: tmp+=time_end+' 23:59:59,'
                else: tmp+=','
            else: tmp+=',,'
            tmp+=condi+','
            tmp+=freq+','
            tmp+=other+','
            tmp+='"'+supervis+'",'
            tmp+=progID
            tmp+='\n'

            #write obj in file
            if not os.path.isfile('db/new_objects.csv'):
                f=open('db/new_objects.csv','w')
                f.write(header.strip()+',ProgramID\n')
            else: f=open('db/new_objects.csv','a')
            f.write(tmp)
            f.close()
            os.chmod('db/new_objects.csv', 0o666)

            #send mail to admins and supervisior of added obj
            send=SendMail(email)
            
            #update message
            if series: number='series'
            send.message=render_template('message',supervisor=supervis.replace('"',''),name=name.replace('"',''),ra=ra,dec=dec,mag=mag,exp=exp,number=number,night=night,prior=prior,group=group.replace('"',''),notes=notes.replace('"',''),message=mess,progID=progID)
            
            try:
                send.run()
            except:
                traceback.print_exc()
                send.mail['cc']=''
                try: send.send_mail("ERROR: exception", traceback.format_exc())
                except: pass   #incorrect mail config

            gc.collect()
            return redirect(url_for('success'))

    gc.collect()
    return render_template('add.html', name='', ra='', dec='', mag='', per='', t0='', exp='', number=1, night=1, series=False, ic=False, phot=False, phot_input='', prior=3, group='', moon=False, moon_input='', phase=False, phase_start='', phase_end='', time=False, time_start='', time_end='', other='', supervis = '', email='', mess='', errors={}, remarks='', simcal='thar',readout='fast',condi='good',groups=groups,freq='unspecified')


# Route for the success page
@app.route('/scheduler/success')
def success():
    gc.collect()
    
    return "Form submitted successfully!"


def check(row):
    '''check format of added data'''
    errors=[]
    row=dict(row)
    #check inputs!!!
    
    for x in row:
        if row[x]=='ERROR':
            raise KeyError('Parameter '+x+' is missing!')
    
    if len(row['Target'])==0:
        errors.append('Missing name of target.')
        return row,errors
    else: row['Target']=row['Target'].replace('*','').strip()

    if len(row['RA'])==0: errors.append(row['Target']+': missing RA.')
    else:
        if not check_dns(row['RA'],min=0,max=23): errors.append(row['Target']+': wrong RA format.')

    if len(row['DEC'])==0: errors.append(row['Target']+': missing DEC.')
    else:
        if not check_dns(row['DEC'],min=-89,max=89): errors.append(row['Target']+': wrong DEC format.')

    if len(row['Period'])>0:
        if not is_float(row['Period']): errors.append(row['Target']+': period has to be number.')
        elif not float(row['Period'])>0: errors.append(row['Target']+': period has to be positive.')

    if len(row['Epoch'])>0:
        if not is_float(row['Epoch']): errors.append(row['Target']+': epoch has to be number.')
        elif float(row['Epoch'])<2000000: errors.append(row['Target']+': epoch has to be in full JD.') 

    if len(row['ExpTime'])==0: errors.append(row['Target']+': missing ExpTime.')
    else:
        if not is_int(row['ExpTime']): errors.append(row['Target']+': ExpTime has to be number.')

    if len(row['Number'])==0: row['Number']=1
    elif not row['Number']=='series':
        if not is_int(row['Number']): errors.append(row['Target']+': number of exp. has to be number or "series".')

    if len(row['Nights'])>0:
        if not is_int(row['Nights']): errors.append(row['Target']+': number of nights has to be number.')

    if len(row['Priority'])==0: row['Priority']=3
    else:
        if not is_float(row['Priority']): errors.append(row['Target']+': priority has to be number.')
    
    #set priority for standards
    if row['Type']=='RV Standard': 
        row['Priority']='0.1'
        if 'IC' in row['Remarks']: row['Priority']='0.2'
    elif row['Type']=='SpecPhot Standard': 
        row['Priority']='0.5'
        if 'IC' in row['Remarks']: row['Priority']='0.6'

    if len(row['MoonPhase'])>0:
        if not is_float(row['MoonPhase']): errors.append(row['Target']+': moon phase has to be number.')
        elif float(row['MoonPhase'])<0 or float(row['MoonPhase'])>1:
            errors.append(row['Target']+': moon phase has to from 0 to 1.')

    if len(row['StartPhase'])>0:
        if not is_float(row['StartPhase']): errors.append(row['Target']+': start phase has to be number.')
        elif float(row['StartPhase'])<0 or float(row['StartPhase'])>1:
            errors.append(row['Target']+': start phase has to from 0 to 1.')

    if len(row['EndPhase'])>0:
        if not is_float(row['EndPhase']): errors.append(row['Target']+': end phase has to be number.')
        elif float(row['EndPhase'])<0 or float(row['EndPhase'])>1:
            errors.append(row['Target']+': end phase has to from 0 to 1.')

    if len(row['StartPhase'])+len(row['EndPhase'])>0 and len(row['Period'])*len(row['Epoch'])==0:
        errors.append(row['Target']+': period and epoch is required.')

    if len(row['StartDate'])>0:
        try: datetime.strptime(row['StartDate'],'%Y-%m-%dT%H:%M:%S')
        except ValueError: 
            try: datetime.strptime(row['StartDate'],'%Y-%m-%d %H:%M:%S')
            except ValueError: errors.append(row['Target']+': wrong StartDate format.')

    if len(row['EndDate'])>0:
        try: datetime.strptime(row['EndDate'],'%Y-%m-%dT%H:%M:%S')
        except ValueError: 
            try: datetime.strptime(row['EndDate'],'%Y-%m-%d %H:%M:%S')
            except ValueError: errors.append(row['Target']+': wrong EndDate format.')
        
    if len(row['Conditions'])==0: row['Conditions']='good'
    elif row['Conditions'] not in ['excellent', 'good', 'poor']:
        errors.append(row['Target']+': wrong Conditions type. Available options: excellent, good, poor.')
        
    if len(row['Frequency'])==0: row['Frequency']='unspecified'
    elif row['Frequency'] not in ['everynight', 'twiceweek', 'onceweek', 'twicemonth', 'oncemonth', 'unspecified']:
        errors.append(row['Target']+': wrong Frequency option. Available options: everynight, twiceweek, onceweek, twicemonth, oncemonth, unspecified.')
    
    return row,errors


@app.route("/scheduler/bulk", methods=['GET', 'POST'])
def bulk():
    '''bulk import of obj from file'''
    if not session.get('logged_in'):
        return redirect(url_for('login', next=request.path))
    
    if request.method == 'POST':
        errors = {}

        if 'submit' in request.form:
            #get data from form
            supervis = request.form['supervis'].strip()
            progID = request.form['progID'].strip()
            email = request.form['email'].strip()
            mess=request.form['mess'].strip()

            #get file with obj
            if 'file' not in request.files:
                errors['file']="No file part."
            file = request.files['file']
            if file.filename == '':
                errors['file']="No selected file."

            if not supervis:
                errors['supervis'] = 'Supervisor is required.'
            if not email:
                errors['email'] = 'Email is required.'
            if not progID:
                errors['progID'] = 'Program ID is required.'   
            else:  
                #TODO check?  
                f=open('db/progID.json','r')
                ids=json.load(f)
                f.close()       
                
                #if progID not in ids: errors['progID'] = 'Program ID is incorrect/unknown.'   
            

            if errors:
                gc.collect()
                
                return render_template('import.html', supervis = supervis, email=email, mess=mess, errors=errors, progID=progID)

            output = io.StringIO()   # create "file-like" output for writing
            objects=[]
            errors['data']=[]
            if file:
                #read data from input file and save them in list
                file_content = [x.decode() for x in file.readlines()]
                csvreader = csv.DictReader(file_content,restval='ERROR')
                if not (csvreader.fieldnames+['Supervisor'])==header.strip().split(','):
                    errors['data'].append('Incorrect CSV format. Check the template!')  
                    return render_template('import.html', supervis = supervis, email=email, mess=mess, errors=errors, progID=progID)  
                csvwriter = csv.DictWriter(output,fieldnames=csvreader.fieldnames+['Supervisor','ProgramID'])
                for row in csvreader:
                    #check inputs!!! 
                    if 'Target' not in row:
                        errors['data'].append('Missing name of target.')
                        continue
                    elif len(row['Target'])==0:
                        errors['data'].append('Missing name of target.')
                        continue
                    
                    try: 
                        row,err=check(row)
                        errors['data']+=err
                    except KeyError: 
                        errors['data'].append(row['Target']+': incorrect row format. Check the template!')  
                        continue                  
                   
                    if row['Type'] in ['RV Standard','SpecPhot Standard']: 
                        row['ProgramID']=''   #TODO?
                        row['Supervisor']='Standards'
                        row['Conditions']=''
                        row['Frequency']=''
                    else: 
                        row['ProgramID']=progID
                        row['Supervisor']=supervis
                    row['Target']=row['Target'].strip()
                    objects.append(row['Target'])
                    csvwriter.writerow(row)

                news=output.getvalue()  #loads contents of "output file"
            if len(errors['data'])==0: del(errors['data'])

            if errors:
                gc.collect()
                
                return render_template('import.html', supervis = supervis, email=email, mess=mess, errors=errors, progID=progID)

            # save the data to a database
            if not os.path.isfile('db/new_objects.csv'):
                f=open('db/new_objects.csv','w')
                f.write(header.strip()+',ProgramID\n')
            else: f=open('db/new_objects.csv','a')
            f.write(news)
            f.close()
            os.chmod('db/new_objects.csv', 0o666)

            #send mail to admins and supervisior of added obj
            send=SendMail(email)

            #update message
            send.message=render_template('message_bulk',supervisor=supervis.replace('"',''),objects=objects,message=mess, progID=progID)

            try:
                send.run()
            except:
                traceback.print_exc()
                send.mail['cc']=''
                try: send.send_mail("ERROR: exception", traceback.format_exc())
                except: pass   #incorrect mail config

            gc.collect()
            return redirect(url_for('success'))

    gc.collect()
    return render_template('import.html', supervis = '', email='', mess='', errors={}, progID='')

@app.route("/scheduler/modif_obj", methods=['GET', 'POST'])
def modif_obj():
    '''modif obj in DB'''
    if not session.get('logged_in'):
        return redirect(url_for('login', next=request.path))
    
    if not os.path.isfile('db/objects.csv'): obj=[]
    else: 
        f=open('db/objects.csv','r')
        reader = csv.DictReader(f)
        obj_all={}
        i=0
        for obj in reader: 
            obj_all[i]=obj
            i+=1 
        f.close()
        
        #print(obj_all)
        ids=sorted(obj_all, key=lambda v: obj_all[v]['Target'].replace('-','').replace(' ','').upper())  #sort ignoring upper/lower case
        
    if request.method == 'POST':
        #get data from form
        errors={}
        status=''
        info=''
        
        supervis = request.form['supervis'].strip()
        progID = request.form['progID'].strip()
        email = request.form['email'].strip()
        mess=request.form['mess'].strip()
        if 'target' in request.form:
            target=int(request.form['target'])
            obj=obj_all[target]
            
            info=obj['Target']+': '+str(obj['Number'])+' x '+str(obj['ExpTime'])+' s; '+str(obj['Remarks'])+'; '+('observation finished' if obj['Done']=='1' else 'observations running')
            status=('done' if obj['Done']=='1' else 'obs')
        else:
            target=''
            errors['name'] = 'Name is required.'
                
        if 'send' in request.form and len(errors)==0:
            status0=status
            status=request.form['status']
            if not supervis:
                errors['supervis'] = 'Supervisor is required.'
            if not email:
                errors['email'] = 'Email is required.'
                
            if not errors:        
                        
                if (status==status0) and len(mess)==0:
                    gc.collect()
                    return "Nothing to change!"
                
                elif len(mess)==0:
                    if progID==obj['ProgramID'] and len(obj['ProgramID'])>0:
                        #correct ID, change status automatically                                               
                        obj['Done']=int(not (bool(int(obj['Done']))))  #change status
                        
                        f=open('db/objects.csv','w')
                        writer=csv.DictWriter(f,fieldnames=header.strip().split(',')+['ProgramID','Done'])
                        writer.writeheader()
                        for i in obj_all:
                            if not i==target: writer.writerow(obj_all[i])
                            else: writer.writerow(obj)
                        f.close() 
                        
                        result='Status changed automatically!'
                    else:
                        #incorrect ID, send request to admin
                        result='Program ID incorrect or missing! Status will be changed manually by admins.'                      
                    
                    #send mail to admins and supervisior of added obj
                    send=SendMail(email)
                  
                    send.message=render_template('message_status',supervisor=supervis,name=obj['Target'],ra=obj['RA'],dec=obj['DEC'],mag=obj['Mag'],exp=obj['ExpTime'],number=obj['Number'],night=obj['Nights'],prior=obj['Priority'],group=obj['Type'],notes=obj['Remarks'],progID=progID,result=result,status=('observation finished' if status=='done' else 'observations running'))
            
                    send.mail["subject"]='Change of observing status'

                    try:
                        send.run()
                    except:
                        traceback.print_exc()
                        send.mail['cc']=''
                        try: send.send_mail("ERROR: exception", traceback.format_exc())
                        except: pass   #incorrect mail config
                        
                    gc.collect()
                    return result
                
                else:
                    #bigger change, send request to admin
                    send=SendMail(email)
                  
                    send.message=render_template('message_change',supervisor=supervis,name=obj['Target'],ra=obj['RA'],dec=obj['DEC'],mag=obj['Mag'],exp=obj['ExpTime'],number=obj['Number'],night=obj['Nights'],prior=obj['Priority'],group=obj['Type'],notes=obj['Remarks'],progID=progID,status=('observation finished' if status=='done' else 'observations running')+(' (not changed)' if status==status0 else ''),message=mess)
            
                    send.mail["subject"]='Change of observing target'

                    try:
                        send.run()
                    except:
                        traceback.print_exc()
                        send.mail['cc']=''
                        try: send.send_mail("ERROR: exception", traceback.format_exc())
                        except: pass   #incorrect mail config
                    
                    gc.collect()
                    return redirect(url_for('success'))          

        return render_template('modify_obj.html', obj=[[x,obj_all[x]['Target']] for x in ids], target=target,info=info,supervis = supervis, email=email, mess=mess, errors=errors, progID=progID,status=status)
    
    return render_template('modify_obj.html', obj=[[x,obj_all[x]['Target']] for x in ids], target='',info='',supervis = '', email='', mess='', errors={}, progID='',status='')



@app.route("/scheduler/db", methods=['GET', 'POST'])
def show_db():
    '''show obj in final DB (NOT new obj)'''
    if not session.get('logged_in'):
        return redirect(url_for('login', next=request.path))
    
    #load obj from file
    if not os.path.isfile('db/objects.csv'): return render_template('show_db.html', header=[], data=[], done=[])
    f=open('db/objects.csv','r')
    reader = csv.DictReader(f)
    data=[]
    done=[]
    all=[]
    f=open('db/progID.json','r')
    ids=json.load(f)
    f.close()
    
    for obj in reader:
        #add program name       
        f=open('db/progID.json','r')
        ids=json.load(f)
        f.close()
        
        if len(obj['ProgramID'])==0: obj['Program']='not given'
        elif obj['ProgramID'] not in ids: obj['Program']='unknown' 
        else: obj['Program']=ids[obj['ProgramID']]['program_title']
        
        #check if obs of obj is finished=done
        if obj['Done']=='1': done.append(obj) 
        else: data.append(obj)  
        all.append(obj)
    f.close()
            
    if request.method == 'POST':
        if 'download' in request.form:
            #download full DB
            #return send_file('db/objects.csv', as_attachment=True)
            si = io.StringIO()  # create "file-like" output for writing
            
            writer=csv.DictWriter(si,fieldnames=header.strip().split(',')+['Done','Program'])
            writer.writeheader()
            writer.writerows([{x: o[x] for x in o if x not in ['ProgramID']} for o in all])
            
            #send output as response
            output = make_response(si.getvalue())
            output.headers["Content-Disposition"] = "attachment; filename=objects.csv"
            output.headers["Content-type"] = "text/csv"
            return output  
        
        elif 'observe' in request.form:
            #download only observe part of DB (not finished obj)
            si = io.StringIO()  # create "file-like" output for writing
            
            writer=csv.DictWriter(si,fieldnames=header.strip().split(',')+['Program'])
            writer.writeheader()
            writer.writerows([{x: o[x] for x in o if x not in ['Done','ProgramID']} for o in data])
            
            #send output as response
            output = make_response(si.getvalue())
            output.headers["Content-Disposition"] = "attachment; filename=objects-observe.csv"
            output.headers["Content-type"] = "text/csv"
            return output
        
        elif 'done' in request.form:
            #download only finished part of DB
            si = io.StringIO() # create "file-like" output for writing
            
            writer=csv.DictWriter(si,fieldnames=header.strip().split(',')+['Program'])
            writer.writeheader()
            writer.writerows([{x: o[x] for x in o if x not in ['Done','ProgramID']} for o in done])
            
            #send output as response
            output = make_response(si.getvalue())
            output.headers["Content-Disposition"] = "attachment; filename=objects-done.csv"
            output.headers["Content-type"] = "text/csv"
            return output    
    
    gc.collect()
    return render_template('show_db.html', header=header.strip().split(',')+['Program'], data=data, done=done)



@app.route("/scheduler/admin", methods=['GET', 'POST'])
def admin():
    '''admin of objects DB (new or final)'''
    #require admin login
    if not session.get('logged_in'):
        return redirect(url_for('login', next=request.path))
    if not session.get('logged_in')=='admin':
        return redirect(url_for('login', next=request.path))
    
    saved=False
    errors=[]
    
    if request.method == 'POST':
        db=request.form['db']
        searchInput=request.form['searchInput']
        if not searchInput: searchInput=''
        
        if db=='new' and os.path.isfile('db/new_objects.csv'): 
            # working with new_objects
            
            #regex delete/accept row buttons 
            r_del = re.compile("delete_*")
            r_acc = re.compile("accept_*")
            
            updated_data = request.form.to_dict(flat=False)  # get all data from form
            if 'id' in updated_data:
                #get order after sorting
                ids={int(i): updated_data['id'].index(i) for i in updated_data['id']}
            else: ids={int(i): int(i) for i in range(len(updated_data))}
                
            if 'download' in request.form:
                #download csv -> without saving DB on server
                
                si = io.StringIO()    # create "file-like" output for writing
                
                # Get the data from the form and sort them based on original order              
                targets=[{x: updated_data[x][ids[i]] for x in header.strip().split(',')+['ProgramID']} for i in sorted(ids)]                
                
                writer=csv.DictWriter(si,fieldnames=header.strip().split(',')+['ProgramID'])
                writer.writeheader()
                writer.writerows(targets)
                
                #make output for web
                output = make_response(si.getvalue())
                output.headers["Content-Disposition"] = "attachment; filename=new_objects.csv"
                output.headers["Content-type"] = "text/csv"
                return output
    
            elif 'delete_all' in request.form:
                #delete all targets
                f=open('db/new_objects.csv','w')
                f.write(header.strip()+',ProgramID\n') 
                f.close()
                
            elif 'accept_all' in request.form:
                #accept all targets
                
                # Get the data from the form and sort them based on original order   
                targets=[{**{x: updated_data[x][ids[i]] for x in header.strip().split(',')+['ProgramID']}, 'Done': '0'} for i in sorted(ids)]
                
                #make data check
                for target in targets:
                    tmp,err=check(target) 
                    errors+=err
                
                f=open('db/new_objects.csv','w')
                writer=csv.DictWriter(f,fieldnames=header.strip().split(',')+['ProgramID'])
                writer.writeheader()                
                if len(errors)>0: writer.writerows([{x:t[x] for x in header.strip().split(',')+['ProgramID']} for t in targets])  #problems with data
                f.close()
                
                if len(errors)==0: 
                    #if all data OK
                    if not os.path.isfile('db/objects.csv'):
                        f=open('db/objects.csv','w')
                        f.write(header.strip()+',ProgramID,Done\n')
                    else: f=open('db/objects.csv','a')                
                                    
                    writer=csv.DictWriter(f,fieldnames=header.strip().split(',')+['ProgramID','Done'])
                    writer.writerows(targets)
                    f.close()  
                    
            elif 'delete_select' in request.form:
                #delete selected targets
                # Get the data from the form and sort them based on original order                  
                targets0=[{x: updated_data[x][ids[i]] for x in header.strip().split(',')+['ProgramID']} for i in sorted(ids)]
                
                selected=request.form['selected']
                id_select=[int(x) for x in selected[:-1].split(',')]
                targets=[]
                if len(selected)>0:
                    for id in ids:
                        if not id in id_select: targets.append(targets0[id])
                    
                    f=open('db/new_objects.csv','w')
                    writer=csv.DictWriter(f,fieldnames=header.strip().split(',')+['ProgramID'])
                    writer.writeheader()
                    writer.writerows(targets)
                    f.close()
                
            elif 'accept_select' in request.form:
                #accept selected targets
                # Get the data from the form and sort them based on original order                  
                targets0=[{x: updated_data[x][ids[i]] for x in header.strip().split(',')+['ProgramID']} for i in sorted(ids)]
                
                selected=request.form['selected']
                id_select=[int(x) for x in selected[:-1].split(',')]
                targets=[]
                if len(selected)>0:
                    for id in ids:
                        if not id in id_select: targets.append(targets0[id])
                        else:
                            target=dict(targets0[id]) 
                            target['Done']='0'      
                            tmp,errors=check(target)   #make data check
                            
                            if len(errors)==0:        
                                #if data OK        
                                if not os.path.isfile('db/objects.csv'):
                                    f=open('db/objects.csv','w')
                                    f.write(header.strip()+',ProgramID,Done\n')
                                else: f=open('db/objects.csv','a')
                                writer=csv.DictWriter(f,fieldnames=header.strip().split(',')+['ProgramID','Done'])
                                writer.writerow(target)
                                f.close() 
                    
                    f=open('db/new_objects.csv','w')
                    writer=csv.DictWriter(f,fieldnames=header.strip().split(',')+['ProgramID'])
                    writer.writeheader()
                    writer.writerows(targets)
                    f.close()
                
            elif len(list(filter(r_del.match,request.form.keys())))>0:
                #delete one target
                id=int(list(filter(r_del.match,request.form.keys()))[0].split('_')[1]) 
                
                # Get the data from the form and sort them based on original order                  
                targets=[{x: updated_data[x][ids[i]] for x in header.strip().split(',')+['ProgramID']} for i in sorted(ids)]
                del(targets[id]) 
                
                f=open('db/new_objects.csv','w')
                writer=csv.DictWriter(f,fieldnames=header.strip().split(',')+['ProgramID'])
                writer.writeheader()
                writer.writerows(targets)
                f.close()
                
            
            elif len(list(filter(r_acc.match,request.form.keys())))>0:
                #accept one target
                id=int(list(filter(r_acc.match,request.form.keys()))[0].split('_')[1])   
                
                # Get the data from the form and sort them based on original order      
                targets=[{x: updated_data[x][ids[i]] for x in header.strip().split(',')+['ProgramID']} for i in sorted(ids)]
                target=dict(targets[id])  
                target['Done']='0'      
                tmp,errors=check(target)   #make data check
                
                if len(errors)==0:        
                    #if data OK        
                    if not os.path.isfile('db/objects.csv'):
                        f=open('db/objects.csv','w')
                        f.write(header.strip()+',ProgramID,Done\n')
                    else: f=open('db/objects.csv','a')
                    writer=csv.DictWriter(f,fieldnames=header.strip().split(',')+['ProgramID','Done'])
                    writer.writerow(target)
                    f.close() 

                    del(targets[id])    
                        
                f=open('db/new_objects.csv','w')
                writer=csv.DictWriter(f,fieldnames=header.strip().split(',')+['ProgramID'])
                writer.writeheader()
                writer.writerows(targets)
                f.close() 
                
            if os.path.isfile('db/objects.csv'): os.chmod('db/objects.csv', 0o666)
            if os.path.isfile('db/new_objects.csv'): os.chmod('db/new_objects.csv', 0o666)
            
        elif db=='objects' and os.path.isfile('db/objects.csv'):  
            #working with objects
            
            r_del = re.compile("delete_*")   #regex delete row buttons 
            
            updated_data = request.form.to_dict(flat=False)           
                       
            if 'id' in updated_data:
                ids={int(i): updated_data['id'].index(i) for i in updated_data['id']}  #get order after sorting
                #update Done values 
                done=[]
                for i in updated_data['id']:
                    if 'Done' in updated_data:
                        if str(i) in updated_data['Done']: done.append('1')  #is checked
                        else: done.append('0')
                    else: done.append('0')    
                updated_data['Done']=done   
                           
            if 'download' in request.form:                               
                #download csv -> without saving DB on server
                
                si = io.StringIO()    # create "file-like" output for writing
                
                # Get the data from the form and sort them based on original order   
                targets=[{x: updated_data[x][ids[i]] for x in header.strip().split(',')+['ProgramID','Done']} for i in sorted(ids)]
                
                writer=csv.DictWriter(si,fieldnames=header.strip().split(',')+['ProgramID','Done'])
                writer.writeheader()
                writer.writerows(targets)
                
                output = make_response(si.getvalue())
                output.headers["Content-Disposition"] = "attachment; filename=objects.csv"
                output.headers["Content-type"] = "text/csv"
                return output    
            
            elif 'save' in request.form:   
                #save changes in table to DB
                            
                # Get the data from the form and sort them based on original order   
                targets=[{x: updated_data[x][ids[i]] for x in header.strip().split(',')+['ProgramID','Done']} for i in sorted(ids)]
                
                #make data check
                for target in targets:
                    tmp,err=check(target) 
                    errors+=err
                
                if len(errors)==0:
                    #if all data OK -> save
                    f=open('db/objects.csv','w')
                    writer=csv.DictWriter(f,fieldnames=header.strip().split(',')+['ProgramID','Done'])
                    writer.writeheader()
                    writer.writerows(targets)
                    f.close() 
                
                    saved=True
                
            elif len(list(filter(r_del.match,request.form.keys())))>0:
                #delete one target
                id=int(list(filter(r_del.match,request.form.keys()))[0].split('_')[1])
                
                # Get the data from the form and sort them based on original order   
                targets=[{x: updated_data[x][ids[i]] for x in header.strip().split(',')+['ProgramID','Done']} for i in sorted(ids)]
                
                #make data check
                for i,target in enumerate(targets):
                    if i==id: continue   #ignore deleting row
                    tmp,err=check(target) 
                    errors+=err
                
                if len(errors)==0: del(targets[id])  #delete row
                
                if len(errors)==0:
                    #if all data OK -> save
                    f=open('db/objects.csv','w')
                    writer=csv.DictWriter(f,fieldnames=header.strip().split(',')+['ProgramID','Done'])
                    writer.writeheader()
                    writer.writerows(targets)
                    f.close()
                
            if len(errors)>0:
                #if some error reload displayed data - NO saved in file!
                return render_template('admin_db.html', db=db, header=header.strip().split(',')+['ProgramID'], data=targets, saved=saved, errors=errors)
                               
            
            if os.path.isfile('db/objects.csv'): os.chmod('db/objects.csv', 0o666)            
        
    else:
        db='new'
        searchInput=''
    
    #load DB from file    
    if db=='new': 
        if not os.path.isfile('db/new_objects.csv'): return render_template('admin_db.html', db=db, header=header.strip().split(',')+['ProgramID'], data=[], saved=saved, errors=errors)
        f=open('db/new_objects.csv','r')
    elif db=='objects': 
        if not os.path.isfile('db/objects.csv'): return render_template('admin_db.html', db=db, header=header.strip().split(',')+['ProgramID'], data=[], saved=saved, errors=errors)
        f=open('db/objects.csv','r')
    reader = csv.DictReader(f)
    data=[]
    for i,obj in enumerate(reader):
        data.append(obj)
    f.close()
    
    gc.collect()
    return render_template('admin_db.html', db=db, header=header.strip().split(',')+['ProgramID'], data=data, saved=saved, errors=errors, searchInput=searchInput)

@app.route("/scheduler/run", methods=['GET','POST'])
def scheduler():
    '''run automatic scheduler'''
    if not session.get('logged_in'):
        return redirect(url_for('login', next=request.path))    
       
    if not os.path.isfile('db/objects.csv'):
        return render_template('run_scheduler.html',night=datetime.now(timezone.utc).strftime('%Y-%m-%d'),number=1,name='',groups={},scheduler='StdPriority',use_group=[],time=False)
    objects0=load_objects('db/objects.csv',check=False)   #check in Simbad?
       
    groups={}
    condi={'excellent':0,'good':0,'poor':0,'na':0}
    freq={'everynight':0, 'twiceweek':0, 'onceweek':0, 'twicemonth':0, 'oncemonth':0, 'unspecified':0}
    programs={}
    
    f=open('db/progID.json','r')
    ids=json.load(f)
    f.close()
    
    for obj in objects0:
        if obj['full']['Done']==1: continue  #remove already finished targets
        group=obj['full']['Type']
        if pd.isna(group): group='None'
        if group in groups: groups[group]+=1
        else: groups[group]=1
        
        #add program name       
        progID=obj['full']['ProgramID']
        if pd.isna(progID): progID=''
        if len(str(progID))==0: prog='not given'
        elif str(progID) not in ids: prog='unknown' 
        else: prog=ids[str(progID)]['program_title']
        
        if prog in programs: programs[prog]['n']+=1
        else: 
            programs[prog]={'n':1}
            if prog in ['not given','unknown']:
                programs[prog]['service']=False
                programs[prog]['readout']='unknown'
            else:
                programs[prog]['service']=(True if ids[str(progID)]['mode'].split()[0]=='service' else False)
                programs[prog]['readout']=ids[str(progID)]['readout'].split()[0].lower()
        
        
        if group not in ['RV Standard','SpecPhot Standard']: 
            #ignore conditons for standards
            if pd.isna(obj['full']['Conditions']): condi['na']+=1
            else: condi[obj['full']['Conditions']]+=1   
            if pd.isna(obj['full']['Frequency']): freq['unspecified']+=1
            else: freq[obj['full']['Frequency']]+=1   
    
    if request.method == 'POST':
        if 'run' in request.form:
            #get values from form
            date=request.form['night']
            nights0=int(request.form['number'])   
            name=request.form['name']  
            limits=request.form['position']           
            series = (request.form.get('series')=='checked')
            scheduler=request.form['scheduler']      
            expF=request.form['exp']  
            prior = (request.form.get('prior')=='checked')  #rescale priorities
            
            if not expF: expF=1
            else: expF=float(expF)
            
            if not 'use_group' in request.form:
                return 'NO objects to schedule!'     
            use_group=request.form.to_dict(flat=False)['use_group'] 
            if not 'use_condi' in request.form: use_condi=[]    
            else: use_condi=request.form.to_dict(flat=False)['use_condi']     
            if 'na' in use_condi: use_condi.append('')
            
            if not 'use_freq' in request.form: use_freq=[]    
            else: use_freq=request.form.to_dict(flat=False)['use_freq']     
            
            if not 'use_program' in request.form: use_program=[]    
            else: use_program=request.form.to_dict(flat=False)['use_program']     
            if 'na' in use_program: use_program.append('')
            
            mag_min=request.form['mag_min'].strip()
            if len(mag_min)==0: mag_min=-20
            else: mag_min=float(mag_min)
            
            mag_max=request.form['mag_max'].strip()
            if len(mag_max)==0: mag_max=20
            else: mag_max=float(mag_max)
            
            if mag_min>mag_max:
                x=mag_max
                mag_max=mag_min
                mag_min=x
            
            n_selected=[]
            n_obs=[]
            n_sch=[]
            out_names=[]
            
            #load last observations
            if prior: make_stats()    
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
                if len(str(progID))==0: prog='not given'
                elif str(progID) not in ids: prog='unknown' 
                else: prog=ids[str(progID)]['program_title']
                
                plandate=datetime.strptime(date,'%Y-%m-%d')
        
                if group in use_group and prog in use_program: 
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
            
                        if not pd.isna(mag):
                            if mag<mag_min or mag>mag_max: continue    #filter of magnitude
                        objects1[str(uuid.uuid4())]=obj
            
            #load config - based on observatory!
            config=load_config('lasilla_config.txt')
            
            observatory=config['observatory']
            
            read_out = config['read_out']     #read_out time of camera + comp (with readout) + ...
            slew_rate = config['slew_rate']   #slew rate of the telescope
            
            #set used scheduler: SequentialScheduler / PriorityScheduler -> select on web
            if scheduler=='Sequential': Scheduler=SequentialScheduler
            elif scheduler=='Priority': Scheduler=PriorityScheduler
            elif scheduler=='StdPriority': Scheduler=StdPriorityScheduler
                       
            #general constraints
            constraints0 = [ModifAltitudeConstraint(config['minAlt'],config['maxAlt'],boolean_constraint=True), 
                        ModifAirmassConstraint(config['airmass'],boolean_constraint=False),AtNightConstraint.twilight_nautical(), MoonSeparationConstraint(config['moon'])]
            
            #load telescope restrictions and set constraint
            limE,limW=load_limits()
            if limits=='both': constraints0.append(LimitConstraint(limE,limW))
            elif limits=='east': constraints0.append(LimitConstraint(limEast=limE))
            elif limits=='west': constraints0.append(LimitConstraint(limWest=limW))
            
            #set azimuth constr.
            if (request.form.get('azm')=='checked'):
                azm0=None
                if request.form['azm_start']: 
                    azm0=float(request.form['azm_start'])*u.deg
                    if azm0<0*u.deg: azm0+=360*u.deg
                    if azm0>360*u.deg: azm0-=360*u.deg
                azm1=None
                if request.form['azm_end']: 
                    azm1=float(request.form['azm_end'])*u.deg
                    if azm1<0*u.deg: azm1+=360*u.deg
                    if azm1>360*u.deg: azm1-=360*u.deg
                constraints0.append(AzimuthConstraint(azm0,azm1))
                    
            plantime=Time(date+' '+str(12-int(round(observatory.longitude.value/15))).rjust(2,'0')+':00:00')    #approx. local noon (in UTC)
            
            nights=nights0
            while nights>0:                                   
                #calculate sunset, sunrise and midnight times + set some time ranges

                #sunrise/sunset calculation with 1 hour extend -> for scheduling intervals and plots
                #could be modified for speed up -> add horizon=-12*u.deg (nautical twilight) or -18 (astronomical) and remove additional hour.
                midnight=observatory.midnight(plantime,n_grid_points=10, which='next')
                suns=observatory.sun_set_time(midnight,n_grid_points=10, which='nearest')-1*u.hour
                sunr=observatory.sun_rise_time(midnight,n_grid_points=10, which='nearest')+1*u.hour

                night=sunr-suns
                obstime=suns+night*np.linspace(0, 1, 100)    #range of observing scheduling              

                constraints=list(constraints0)
                if (request.form.get('time')=='checked'):
                    #constraints on time -> obs. part of night
                    date=plantime.strftime('%Y-%m-%d')
                    if request.form['time_start']: 
                        start=Time(date+' '+request.form['time_start'])
                        if start<plantime:
                            #value after midnight
                            start+=1*u.day
                    else: start=suns   #start time not given -> use sunset
                    if request.form['time_end']: 
                        end=Time(date+' '+request.form['time_end'])
                        if end<plantime:
                            #value after midnight
                            end+=1*u.day
                    else: end=sunr   #stop time not given -> use sunrise
                    constraints.append(TimeConstraint(start,end))                                       
            
                # Prefiltering
                objects=prefilter(objects1,constraints,observatory,obstime)
                
                # presort needed mainly for Seq. scheduler
                if scheduler=='Sequential':
                    objects=presort(objects, observatory, midnight,key='meridian')     #meridian/set/rise
                
                n_selected.append(len(objects1))
                n_obs.append(len(objects))
            
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
                            blocks.append(ObservingBlock.from_exposures(FixedTarget(name=name1, coord=obj['target'].coord),obj['priority'],expF*obj['exp']*u.second,5,read_out,constraints=cons))
                            if blocks[-1].duration<60*u.second: blocks[-1].duration=60*u.second   #fix for short blocks
                    else:
                        if obj['target'].name in names:  #repeating objects -> NOT replace debug plots
                            names[obj['target'].name]+=1
                            name1=obj['target'].name+'_s'+str(names[obj['target'].name])
                        else:
                            names[obj['target'].name]=0
                            name1=obj['target'].name
                        blocks.append(ObservingBlock.from_exposures(FixedTarget(name=name1, coord=obj['target'].coord),obj['priority'],expF*obj['exp']*u.second,obj['n_exp'],read_out,constraints=cons))
                        if blocks[-1].duration<60*u.second: blocks[-1].duration=60*u.second   #fix for short blocks
            
                if len(blocks)==0:
                    return '<p>Schedule is EMPTY!</p>'+'<p>Selected objects: '+str(n_selected[0])+'<br>'+'Observable objects: '+str(n_obs[0])+'</p>'                    
            
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
                
                if nights0>1:
                    #save schedules...
                    out=name+'_'+plantime.strftime('%Y-%m-%d')
                    out_names.append(out)
                    
                    tab=schedule_table(schedule,objects1)    
                    df=tab[~(tab['target']=='TransitionBlock')].to_pandas()
                    
                    cols={'target':'Target', 'ra':'RA', 'dec':'DEC', 'mag':'Mag','exposure (seconds)':'ExpTime', 'number exposures':'Number','_Remarks':'Remarks', 'start time (UTC)':'Start', 'end time (UTC)':'End','altitude':'Altitude', 'airmass':'Airmass', 'azimut':'Azimut','altitude-start':'AltitudeStart', 'airmass-start':'AirmassStart', 'azimut-start':'AzimutStart','altitude-end':'AltitudeEnd', 'airmass-end':'AirmassEnd', 'azimut-end':'AzimutEnd','position':'Position','priority':'Priority','moon-separation':'MoonSeparation'}
    
                    df=df.rename(columns=cols)
                    df.to_csv('schedules/'+out+'.csv',index=False)
                    n_sch.append(len(df))
                    
                    
                    #remove old figs
                    if os.path.isfile('schedules/'+out+'_alt.png'):
                        os.remove('schedules/'+out+'_alt.png')
                        os.remove('schedules/'+out+'_sky.png')
                    
                    #remove already scheduled targets for next night scheduling
                    objectsS=[x.target.name for x in schedule.observing_blocks]
                    tmp={}
                    for i,obj in enumerate(objects1.values()):
                        if obj['target'].name in objectsS:
                            del(objectsS[objectsS.index(obj['target'].name)])
                            if obj['priority']<1: tmp[obj['target'].name]=obj     #every-night objects (RV std...)
                        else: tmp[obj['target'].name]=obj

                    objects1=dict(tmp)
                
                if len(objects0)==0: break

                nights-=1
                plantime+=1*u.day             
            
            gc.collect()                   
            if nights0==1: 
                code=str(uuid.uuid4())    #unique hash for different users
                cache.set(code,[schedule,objects1])    #save in cache
                
                return redirect(url_for('new_schedule', selected=n_selected[0], observable=n_obs[0],code=code))
            else:
                return render_template('multi_schedule.html', names=out_names, selected=n_selected, observable=n_obs, scheduled=n_sch)
    
    gc.collect()
    return render_template('run_scheduler.html',night=datetime.now(timezone.utc).strftime('%Y-%m-%d'),number=1,name='',groups=groups,scheduler='StdPriority',use_group=groups,time=False,azm=False,position='both',condi=condi,use_condi=['good','poor','na'],programs=programs,exp=1,freq=freq,use_freq=['everynight', 'twiceweek', 'onceweek', 'twicemonth', 'oncemonth', 'unspecified'],prior=True)


@app.route("/scheduler/new_schedule", methods=['GET','POST'])
def new_schedule():
    '''show new scheduler'''
    gc.collect()
    
    if not (session.get('logged_in') or request.args.get('user')):
        return redirect(url_for('login', next=request.path))
    
    #get unique hash code -> load data from cache
    code=request.args.get('code')
    schedule,objects=cache.get(code)  
       
    #make output table
    tab=schedule_table(schedule,objects)
    
    df=tab[~(tab['target']=='TransitionBlock')].to_pandas()
    
    if len(df)==0:
        return '<p>Schedule is EMPTY!</p>'+'<p>Selected objects: '+str(request.args.get('selected'))+'<br>'+'Observable objects: '+str(request.args.get('observable'))+'</p>'
    
    #cols to save in CSV
    cols={'target':'Target', 'ra':'RA', 'dec':'DEC', 'mag':'Mag','exposure (seconds)':'ExpTime', 'number exposures':'Number','_Remarks':'Remarks', 'start time (UTC)':'Start', 'end time (UTC)':'End','altitude':'Altitude', 'airmass':'Airmass', 'azimut':'Azimut','altitude-start':'AltitudeStart', 'airmass-start':'AirmassStart', 'azimut-start':'AzimutStart','altitude-end':'AltitudeEnd', 'airmass-end':'AirmassEnd', 'azimut-end':'AzimutEnd','position':'Position','priority':'Priority','moon-separation':'MoonSeparation'}
    cols1=['Target','RA', 'DEC', 'Mag','ExpTime', 'Number','Remarks', 'Start', 'End','Position','Priority','Azimut','Altitude','Airmass']   #for download
  
    if not 'position' in df.columns: del(cols['position'])
    
    #get created data from cache
    tmp=cache.get(code+'dfPlot') 
    if tmp is not None:
        df,alt_plot,sky_plot=tmp  
    else: 
        df=df.rename(columns=cols)
        alt_plot,sky_plot=web_plot(schedule)    
        cache.set(code+'dfPlot',[pd.DataFrame(df),alt_plot,sky_plot])    
    
    if request.method == 'POST':
        if 'download' in request.form:
            #download csv
            si = io.StringIO()    # create "file-like" output for writing
            
            if 'AltitudeStart' in df.columns:
                df['Altitude']=df.apply(lambda row: '%.0f - %.0f' %(float(row['AltitudeStart']),float(row['AltitudeEnd'])), axis=1)
                df['Azimut']=df.apply(lambda row: '%.0f - %.0f' %(float(row['AzimutStart']),float(row['AzimutEnd'])), axis=1)
                df['Airmass']=df.apply(lambda row: '%.1f - %.1f' %(float(row['AirmassStart']),float(row['AirmassEnd'])), axis=1)
                                        
            
            df[cols1].to_csv(si,index=False)
            output = make_response(si.getvalue())
            output.headers["Content-Disposition"] = "attachment; filename=schedule.csv"
            output.headers["Content-type"] = "text/csv"
            return output 
        
        if 'json' in request.form:
            #download json
            si = io.StringIO()    # create "file-like" output for writing
            
            if 'AltitudeStart' in df.columns:
                df['Altitude']=df.apply(lambda row: '%.0f - %.0f' %(float(row['AltitudeStart']),float(row['AltitudeEnd'])), axis=1)
                df['Azimut']=df.apply(lambda row: '%.0f - %.0f' %(float(row['AzimutStart']),float(row['AzimutEnd'])), axis=1)
                df['Airmass']=df.apply(lambda row: '%.1f - %.1f' %(float(row['AirmassStart']),float(row['AirmassEnd'])), axis=1)
            
            df[cols1].to_json(si,orient='records',index=False)
            output = make_response(si.getvalue())
            output.headers["Content-Disposition"] = "attachment; filename=schedule.json"
            output.headers["Content-type"] = "text/json"
            return output 
        
        if 'batch' in request.form:
            #download batch
            si = io.StringIO()    # create "file-like" output for writing
            
            df1=pd.DataFrame()
            df1['Target']=df['Target']
            df1['Object']='target'
            df1['Count repeat']=[int(x) for x in df['Number']]
            df1['Exposure length']=[int(x) for x in df['ExpTime']]
            tmp=[x.replace(':','').replace(' ','') for x in df['RA']]
            df1['RA']=[x if '.' in x else x+'.0' for x in tmp]
            tmp=[x.replace(':','').replace(' ','') for x in df['DEC']]
            df1['DEC']=[x if '.' in x else x+'.0' for x in tmp]
            df1['TelPos']="east"
            df1['Tracking']="on"
            df1['Sleep']=1
            df1['CamPos']=0
            df1['FocPos']=0
            df1['Filter']="same"
            df1['Grating angle']=""
            df1['Spectral filter']="same"
            df1['Count of pulses']=0.0
            df1['Iodine cell']=[('IC' in x) for x in df['Remarks']]
            df1['Enable']=True
            
            df1.to_json(si,orient='records',index=False)
            output = make_response(si.getvalue())
            output.headers["Content-Disposition"] = "attachment; filename=schedule_batch.json"
            output.headers["Content-type"] = "text/json"
            return output 
        
        if 'save' in request.form:
            #save scheduler on server
            name=request.form['name'].replace('/','_').replace('\\','_')
            if not '.csv' in name: name+='.csv'
            df.to_csv('schedules/'+name,index=False)
            
            #save images
            f=open('schedules/'+name.replace('.csv','_alt.png'),'wb')              
            f.write(base64.b64decode(alt_plot.encode('utf8')))
            f.close()
            
            f=open('schedules/'+name.replace('.csv','_sky.png'),'wb')              
            f.write(base64.b64decode(sky_plot.encode('utf8')))
            f.close()
                
            return 'Schedule saved with name "'+name+'"!'  
        
        if 'modify' in request.form:
            #save&modify schedule
            name=request.form['name'].replace('/','_').replace('\\','_')
            if not '.csv' in name: name+='.csv'
            df.to_csv('schedules/'+name,index=False) 
                
            return redirect(url_for('modify', name=name))   
    
    gc.collect()
    return render_template('schedule.html', selected=request.args.get('selected'),observable=request.args.get('observable'),scheduled=len(df),schedule=df.to_dict('records'), alt_plot=alt_plot,sky=sky_plot,user=request.args.get('user'))

def web_plot(schedule):
    '''make output plots for web'''      
    #make alt plot
    plt.Figure()
    ax=plot_schedule(schedule,plottype='alt',moon=True,slots=True,res=40)
    #save to "buffer" file
    buf=io.BytesIO()
    plt.savefig(buf,format='png',dpi=150)
    plt.close()
    buf.seek(0)
    #load result from buffer to html output
    alt_plot = base64.b64encode(buf.getvalue()).decode('utf8')
    buf.close()
    
    #make sky plot
    plt.Figure()
    ax=plot_schedule(schedule,plottype='sky',moon=True,res=40)
    #save to "buffer" file
    buf=io.BytesIO()
    plt.savefig(buf,format='png',dpi=150)
    plt.close()
    buf.seek(0)
    #load result from buffer to html output
    sky_plot = base64.b64encode(buf.getvalue()).decode('utf8')
    buf.close()
    
    return alt_plot,sky_plot

@app.route("/scheduler/modify", methods=['GET','POST'])
def modify():
    '''make schedule manually or modify created one'''
    gc.collect()
    
    if not session.get('logged_in'):
        return redirect(url_for('login', next=request.path))
    
    schedules=[os.path.splitext(os.path.basename(x))[0] for x in  sorted(glob.glob('schedules/*.csv'), key=os.path.getmtime)][::-1]  
    
    if not os.path.isfile('db/objects.csv'):
        return render_template('modify.html', schedules=schedules,name='',schedule=[],code='',codeF='', alt_plot='',sky='',start='',night='',groups={},use_group=[],objects=[],obs=[],indiv=True, calc=0,total_time=16,all_obj=[],codeObj='',indObj='',condi='all')
    objects0=load_objects('db/objects.csv',check=False)   #check in Simbad?
    
    f=open('db/progID.json','r')
    ids=json.load(f)
    f.close()
    
    scroll=False
    
    groups={}
    all_obj0={}
    for obj in objects0:
        if obj['full']['Done']==1: continue  #remove already finished targets
        all_obj0[str(uuid.uuid4())]={'name':obj['full']['Target'],'obj':obj}
                
        # group=obj['full']['Type']
        # if pd.isna(group): group='None'
        # if group in groups: groups[group]+=1
        # else: groups[group]=1        
        
        #add program name       
        progID=obj['full']['ProgramID']
        if pd.isna(progID): progID=''
        if len(str(progID))==0: prog='not given'
        elif str(progID) not in ids: prog='unknown' 
        else: prog=ids[str(progID)]['program_title']
        
        if prog in groups: groups[prog]+=1
        else: groups[prog]=1
    
    #cols to save in CSV
    cols={'target':'Target', 'ra':'RA', 'dec':'DEC', 'mag':'Mag','exposure (seconds)':'ExpTime', 'number exposures':'Number','_Remarks':'Remarks', 'start time (UTC)':'Start', 'end time (UTC)':'End','altitude':'Altitude', 'airmass':'Airmass', 'azimut':'Azimut','altitude-start':'AltitudeStart', 'airmass-start':'AirmassStart', 'azimut-start':'AzimutStart','altitude-end':'AltitudeEnd', 'airmass-end':'AirmassEnd', 'azimut-end':'AzimutEnd','position':'Position','priority':'Priority','moon-separation':'MoonSeparation'}
    cols1=['Target','RA', 'DEC', 'Mag','ExpTime', 'Number','Remarks', 'Start', 'End','Position','Priority','Altitude','Azimut','Airmass']   #for download
    
    #load config - based on observatory!
    config=load_config('lasilla_config.txt')
            
    observatory=config['observatory']
    read_out = config['read_out']    #read_out time of camera + comp (with readout) + ...
    slew_rate = config['slew_rate']   #slew rate of the telescope
    
    
    #recalculate transitions
    transitioner = Transitioner(slew_rate)
    
    if request.method=='GET':
        #modify schedule after save&modify        
        if 'name' in request.args:
            name=request.args['name']
            
            codeObj=str(uuid.uuid4())
            
            cache.set(codeObj,dict(all_obj0))
            if len(all_obj0)==0: codeObj=''
        
            return render_template('modify.html', schedules=schedules,name=name,schedule=[],code='',codeF='', alt_plot='',sky='',start='',night='',groups=groups,use_group=[],objects=[],obs=[],prev_plot='',indiv=True,modify=True, calc=1,total_time=16,all_obj=sorted(all_obj0.items(), key=lambda kv: (kv[1]['name'].lower().replace(' ',''), kv[0])),codeObj=codeObj,indObj='',condi='all',scroll=False,freq='all')
        
    
    if request.method=='POST':
        code=request.form['code'] 
        codeF=request.form['codeF']   
        codeObj=request.form['codeObj']  
        
        idObj=request.form['obj'] 
        
        if codeObj:
            all_obj=cache.get(codeObj)   
        else:
            codeObj=str(uuid.uuid4())     
            all_obj=all_obj0       
            cache.set(codeObj,dict(all_obj0))  
        
        if all_obj is None:
            codeObj=str(uuid.uuid4())     
            all_obj=all_obj0       
            cache.set(codeObj,dict(all_obj0))
            
        if len(all_obj)==0: codeObj=''
            
            
        name=request.form['name'] 
        nightF=request.form['night']
        startF=request.form['start']
        
        calc=request.form['calc_test']
        total_time=request.form['total_time']
        
        #regex add row and preview buttons 
        r_add = re.compile("add_*")
        r_prev = re.compile("preview_*")
        
        if not 'use_group' in request.form: use_group=[]     
        else: use_group=request.form.to_dict(flat=False)['use_group']
        
        condi=request.form['condi']
        if condi=='all': use_condi=['excellent','good','poor','']
        elif condi=='na': use_condi=['']
        else: use_condi=[condi]
        
        freq=request.form['freq']
        if freq=='all': use_freq=['everynight', 'twiceweek', 'onceweek', 'twicemonth', 'oncemonth', 'unspecified']
        else: use_freq=[freq]
        
        indiv = (request.form.get('individual')=='checked')
        
        if 'load' in request.form:   
            #load schedule
            
            if not code: code=str(uuid.uuid4())    #unique hash for different users     
                   
            df=pd.read_csv('schedules/'+name+'.csv')
            df=df.fillna('')  #remove nan 
            
            start=Time(list(df.Start)[0])
            end=Time(list(df.End)[-1])
            
            #sunrise/sunset calculation with 1 hour extend -> for scheduling intervals and plots
            #could be modified for speed up -> add horizon=-12*u.deg (nautical twilight) or -18 (astronomical) and remove additional hour.
            #get sunser/sunrise around the observing scheduler
            suns=observatory.sun_set_time(start,n_grid_points=10, which='previous')-1*u.hour
            sunr=observatory.sun_rise_time(end,n_grid_points=10, which='next')+1*u.hour 
            
            nightF=suns.strftime('%Y-%m-%d')
            
            #add targets to schedule
            schedule=Schedule(suns,sunr)
            schedule.observer = observatory
            ha0=[]
            ha1=[]
            de=[]
            t00=0
            for i,obj in df.iterrows():
                ra='{}h{}m{}s'.format(*obj.RA.replace(':',' ').replace(',','.').split())
                dec='{}d{}m{}s'.format(*obj.DEC.replace(':',' ').replace(',','.').split())
                coordinates=SkyCoord(ra,dec,frame='icrs')
                b=ObservingBlock.from_exposures(FixedTarget(name=obj.Target, coord=coordinates), obj.Priority, obj.ExpTime*u.second,obj.Number, read_out)
                b.observer = observatory
                t0=Time(obj.Start)
                if t00==0: t00=t0
                if len(schedule.scheduled_blocks)>0:
                    tr=TransitionBlock.from_duration((t0-schedule.scheduled_blocks[-1].end_time).value*86400*u.second)             
                    schedule.insert_slot(schedule.scheduled_blocks[-1].end_time,tr)                    
                schedule.insert_slot(t0, b)   
                
                lst0=observatory.local_sidereal_time(t0).deg*u.deg
                ha0.append((lst0-b.target.ra)%(360*u.deg))
                lst1=observatory.local_sidereal_time(schedule.scheduled_blocks[-1].end_time).deg*u.deg
                ha1.append((lst1-b.target.ra)%(360*u.deg)) 
                de.append(b.target.dec)
             
            total_time=(sunr-t00).sec/3600
                
            df['ha0']=ha0
            df['ha1']=ha1
            df['de']=de
            
            if os.path.isfile('schedules/'+name+'_alt.png'):
                dt=datetime.fromtimestamp(os.path.getmtime('schedules/'+name+'.csv')) - datetime.fromtimestamp(os.path.getmtime('schedules/'+name+'_alt.png'))
                if dt.total_seconds()>5:
                    #old images
                    alt_plot,sky_plot=web_plot(schedule)
                
                    f=open('schedules/'+name+'_alt.png','wb')              
                    f.write(base64.b64decode(alt_plot.encode('utf8')))
                    f.close()
                    
                    f=open('schedules/'+name+'_sky.png','wb')              
                    f.write(base64.b64decode(sky_plot.encode('utf8')))
                    f.close()
                else:
                    #load saved images
                    f=open('schedules/'+name+'_alt.png','rb')
                    alt_plot=base64.b64encode(f.read()).decode('utf8')
                    f.close()
                    
                    f=open('schedules/'+name+'_sky.png','rb')
                    sky_plot=base64.b64encode(f.read()).decode('utf8')
                    f.close()                             
            else: 
                alt_plot,sky_plot=web_plot(schedule)
                
                f=open('schedules/'+name+'_alt.png','wb')              
                f.write(base64.b64decode(alt_plot.encode('utf8')))
                f.close()
                
                f=open('schedules/'+name+'_sky.png','wb')              
                f.write(base64.b64decode(sky_plot.encode('utf8')))
                f.close()
            
            cache.set(code,[pd.DataFrame(df),alt_plot,sky_plot])    #save in cache
            
            if codeF: 
                tmp=cache.get(codeF)
                if tmp is None:
                    codeF=''
                    objects=[]
                    obs=[]
                else: objects,obs=tmp
            else: 
                objects=[]
                obs=[]
                
            
            
            startF=start.strftime('%H:%M') 
            dfW=df.to_dict('records')
            
            calc=1
        
        if 'filter' in request.form:
            #filter objects by type and observability
            
            #add selected objects by types and series
            if not codeF: codeF=str(uuid.uuid4())    #unique hash for different users    
            
            objects=[]
            for obj in objects0:
                if obj['full']['Done']==1: continue
                #if not series and obj['n_exp']=='series': continue
                progID=obj['full']['ProgramID']
                if pd.isna(progID): progID=''
                if len(str(progID))==0: prog='not given'
                elif str(progID) not in ids: prog='unknown' 
                else: prog=ids[str(progID)]['program_title']
                group=prog
                #group=obj['full']['Type']    
                con=obj['full']['Conditions']
                fr=obj['full']['Frequency']                
                if pd.isna(group): group='None'
                if pd.isna(con): con=''
                if pd.isna(fr): fr='unspecified'
                if group in use_group: 
                    if (con in use_condi and fr in use_freq) or group in ['RV Standard','SpecPhot Standard']:
                        #ignore conditons for standards
                        objects.append(obj)                
                
            obs=[]  #observability times
             
            if request.form['night']:
                #filter only observable objects
                
                plantime=Time(request.form['night']+' '+str(12-int(round(observatory.longitude.value/15))).rjust(2,'0')+':00:00')    #approx. local noon (in UTC)
                            
                #calculate sunset, sunrise and midnight times + set some time ranges

                #sunrise/sunset calculation with 1 hour extend -> for scheduling intervals and plots
                #could be modified for speed up -> add horizon=-12*u.deg (nautical twilight) or -18 (astronomical) and remove additional hour.
                midnight=observatory.midnight(plantime,n_grid_points=10, which='next')
                suns=observatory.sun_set_time(midnight,n_grid_points=10, which='nearest')-1*u.hour
                sunr=observatory.sun_rise_time(midnight,n_grid_points=10, which='nearest')+1*u.hour
                start=suns
                    
                if request.form['start']:
                    #get start time for schedule
                    start=Time(request.form['night']+' '+request.form['start'])                    
                    
                    if start<suns+1*u.hour:
                        if (start+1*u.day)>suns and (start+1*u.day)<sunr: start+=1*u.day  #obs. start after UT midnight
                        else: start=observatory.sun_set_time(midnight,n_grid_points=10, which='nearest',horizon=-12*u.deg)  #time is before sunset -> set begining of astro twilight
                    
                night=sunr-start
                obstime=start+night*np.linspace(0, 1, 50)    #range of observing scheduling
            
                #general constraints
                constraints = [ModifAltitudeConstraint(config['minAlt'],config['maxAlt'],boolean_constraint=False), 
                        AirmassConstraint(config['airmass'],boolean_constraint=True),AtNightConstraint.twilight_nautical(), MoonSeparationConstraint(config['moon'])]
                
                # Prefiltering
                if not indiv: 
                    #only general constraints
                    objects=prefilter({i:x for i,x in enumerate(objects)},constraints,observatory,obstime)
                    
                    #add time slot of observability
                    applied_constraints = [constraint(observatory, [x['target'] for x in objects], times=obstime,grid_times_targets=True) for constraint in constraints]    #apply constraints on targets
                    constraint_arr = np.logical_and.reduce(applied_constraints)    #combine all consts
                    
                    for i in range(len(objects)):
                        tObs=obstime[constraint_arr[i]]    #observable times
                        obs.append(tObs[0].strftime('%H:%M')+' - '+tObs[-1].strftime('%H:%M'))
                        
                else:
                    #apply individual const.
                    objects1=list(objects)
                    objects=[]
                    for obj in objects1:
                        cons=list(constraints)                        
                        
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
                        if not pd.isna(obj['full']['StartPhase']) or not pd.isna(obj['full']['EndPhase']):
                            #phase constraint for EB or exoplanets
                            objPer=PeriodicEvent(epoch=Time(obj['full']['Epoch'],format='jd'),period=obj['full']['Period']*u.day)
                            if pd.isna(obj['full']['StartPhase']): start=None
                            else: start=obj['full']['StartPhase']
                            if pd.isna(obj['full']['EndPhase']): end=None
                            else: end=obj['full']['EndPhase']
                            cons.append(PhaseConstraint(objPer,start,end))
                            
                        if is_observable(cons, observatory, obj['target'], obstime): 
                            objects.append(obj)                
                
                            #add time slot of observability
                            applied_constraints = [constraint(observatory, obj['target'], times=obstime,grid_times_targets=True) for constraint in cons]    #apply constraints on targets
                            constraint_arr = np.logical_and.reduce(applied_constraints)    #combine all consts
                
                            tObs=obstime[constraint_arr[0]]    #observable times
                            obs.append(tObs[0].strftime('%H:%M')+' - '+tObs[-1].strftime('%H:%M'))
                
            if code: 
                df,alt_plot,sky_plot=cache.get(code)
                dfW=df.to_dict('records')
            else: 
                dfW=[]
                alt_plot=''
                sky_plot='' 
            
            objects=[o['full'].fillna('') for o in objects]
                
            cache.set(codeF,[list(objects),obs])                
        
        if len(list(filter(r_prev.match,request.form.keys())))>0:
            #preview -> altitude plot
            id=int(list(filter(r_prev.match,request.form.keys()))[0].split('_')[1])  
            
            objects,obs=cache.get(codeF)
            obj=objects[id].to_dict()
            
            plantime=Time(request.form['night']+' '+str(12-int(round(observatory.longitude.value/15))).rjust(2,'0')+':00:00')    #approx. local noon (in UTC)
                            
            #calculate sunset, sunrise and midnight times + set some time ranges

            #sunrise/sunset calculation with 1 hour extend -> for scheduling intervals and plots
            #could be modified for speed up -> add horizon=-12*u.deg (nautical twilight) or -18 (astronomical) and remove additional hour.
            midnight=observatory.midnight(plantime,n_grid_points=10, which='next')
            suns=observatory.sun_set_time(midnight,n_grid_points=10, which='nearest')-1*u.hour
            sunr=observatory.sun_rise_time(midnight,n_grid_points=10, which='nearest')+1*u.hour
            night=sunr-suns
            
            stime=suns+night*np.linspace(0, 1, 40)
            
            start = plantime

            # Calculate and order twilights and set plotting alpha for each
            twilights0 = [
                (observatory.sun_set_time(Time(start), which='next',n_grid_points=10).datetime, 0.0,'sunset'),
                (observatory.twilight_evening_civil(Time(start), which='next',n_grid_points=10).datetime, 0.1,'civil'),
                (observatory.twilight_evening_nautical(Time(start), which='next',n_grid_points=10).datetime, 0.2,'nautic'),
                (observatory.twilight_evening_astronomical(Time(start), which='next',n_grid_points=10).datetime, 0.3,'astro'),
                (observatory.twilight_morning_astronomical(Time(start), which='next',n_grid_points=10).datetime, 0.4,'astro'),
                (observatory.twilight_morning_nautical(Time(start), which='next',n_grid_points=10).datetime, 0.3,'nautic'),
                (observatory.twilight_morning_civil(Time(start), which='next',n_grid_points=10).datetime, 0.2,'civil'),
                (observatory.sun_rise_time(Time(start), which='next',n_grid_points=10).datetime, 0.1,'sunrise'),
            ]

            twilights=[]
            for t in twilights0:
                if not isinstance(t[0],np.ndarray): twilights.append(t)  #remove if not twilight
            
            import operator
            
            ymin=1
            twilights.sort(key=operator.itemgetter(0))
            
            ax=plt.gca()
            
            for i, twi in enumerate(twilights[1:], 1):
                plt.axvspan(twilights[i - 1][0], twilights[i][0],ymin=0, ymax=1, color='grey', alpha=twi[1])
                
            for i,tw in enumerate(twilights):
                if i<4: plt.text(tw[0],ymin,tw[2],horizontalalignment='right',verticalalignment='bottom',fontsize=8,rotation='vertical')
                else: plt.text(tw[0],ymin,tw[2],horizontalalignment='left',verticalalignment='bottom',fontsize=8,rotation='vertical')
            
            #star alt track
            ra='{}h{}m{}s'.format(*obj['RA'].replace(':',' ').replace(',','.').split())
            dec='{}d{}m{}s'.format(*obj['DEC'].replace(':',' ').replace(',','.').split())
            coordinates=SkyCoord(ra,dec,frame='icrs')
            ax=plot_altitude(FixedTarget(coordinates,name=obj['Target']), observatory, stime,brightness_shading=False,ax=ax,style_kwargs={'lw':1,'fmt':'-'})
            
            plt.title(obj['Target'])
            ax.set_ylim(0,91)
            ax.yaxis.set_major_locator(matplotlib.ticker.MultipleLocator(10))
            plt.tight_layout()
            
            #save to "buffer" file
            buf=io.BytesIO()
            plt.savefig(buf,format='png',dpi=150)
            plt.close()
            buf.seek(0)
            #load result from buffer to html output
            prev_plot = base64.b64encode(buf.getvalue()).decode('utf8')
            buf.close()
            
            return render_template('image.html',image=prev_plot)            
        
        if len(list(filter(r_add.match,request.form.keys())))>0:
            #add one target
            id=int(list(filter(r_add.match,request.form.keys()))[0].split('_')[1])  
            
            objects,obs=cache.get(codeF)
            obj=objects[id].to_dict()
            
            #get data
            if code: 
                df,alt_plot,sky_plot=cache.get(code)
                data=df.to_dict('index')
            else: 
                code=str(uuid.uuid4())    #unique hash for different users     
                data={}
                alt_plot=''
                sky_plot=''
            
            if len(data)>0: last=list(sorted(data.keys()))[-1]
            else: last=-1
            
            new_obj={'_'+x: obj[x] if not pd.isna(obj[x]) else '' for x in obj}
            new_obj['index']=last+2
            new_obj['Target']=new_obj['_Target']
            new_obj['RA']=new_obj['_RA']
            new_obj['DEC']=new_obj['_DEC']
            new_obj['Mag']=new_obj['_Mag']
            new_obj['duration (minutes)']=''
            new_obj['Start']=''
            new_obj['End']=''
            new_obj['configuration']=''
            if len(data)>0:
                if 'Azimut' in data[last]:
                    #old schedule
                    new_obj['Altitude']=''
                    new_obj['Airmass']=''
                    new_obj['Azimut']=''
                else:
                    new_obj['AltitudeStart']=''
                    new_obj['AirmassStart']=''
                    new_obj['AzimutStart']=''
                    new_obj['AltitudeEnd']=''
                    new_obj['AirmassEnd']=''
                    new_obj['AzimutEnd']=''  
                    new_obj['MoonSeparation']=''
            else:
                new_obj['AltitudeStart']=''
                new_obj['AirmassStart']=''
                new_obj['AzimutStart']=''
                new_obj['AltitudeEnd']=''
                new_obj['AirmassEnd']=''
                new_obj['AzimutEnd']=''  
                new_obj['MoonSeparation']=''
            new_obj['Priority']=obj['Priority']
            new_obj['ExpTime']=obj['ExpTime']
            if obj['Number']=='series': new_obj['Number']=5
            else: new_obj['Number']=obj['Number']
            new_obj['Position']=''
            new_obj['Remarks']=new_obj['_Remarks']
            del(new_obj['_Remarks'])
            
            data[last+1]=new_obj            
            df=pd.DataFrame().from_dict(data,'index')
            
            cache.set(code,[pd.DataFrame(df),alt_plot,sky_plot])    #save in cache
            
            dfW=df.to_dict('records')
            
            calc=0
            
        if 'indiv' in request.form:
            #add individual target           
            obj=all_obj[idObj]['obj']['full'].to_dict()

            #get data
            if code: 
                df,alt_plot,sky_plot=cache.get(code)
                data=df.to_dict('index')
            else: 
                code=str(uuid.uuid4())    #unique hash for different users     
                data={}
                alt_plot=''
                sky_plot=''
            
            if len(data)>0: last=list(sorted(data.keys()))[-1]
            else: last=-1
            
            new_obj={'_'+x: obj[x] if not pd.isna(obj[x]) else '' for x in obj}
            new_obj['index']=last+2
            new_obj['Target']=new_obj['_Target']
            new_obj['RA']=new_obj['_RA']
            new_obj['DEC']=new_obj['_DEC']
            new_obj['Mag']=new_obj['_Mag']
            new_obj['duration (minutes)']=''
            new_obj['Start']=''
            new_obj['End']=''
            new_obj['configuration']=''
            if len(data)>0:
                if 'Azimut' in data[last]:
                    #old schedule
                    new_obj['Altitude']=''
                    new_obj['Airmass']=''
                    new_obj['Azimut']=''
                else:
                    new_obj['AltitudeStart']=''
                    new_obj['AirmassStart']=''
                    new_obj['AzimutStart']=''
                    new_obj['AltitudeEnd']=''
                    new_obj['AirmassEnd']=''
                    new_obj['AzimutEnd']=''  
                    new_obj['MoonSeparation']=''
            else:
                new_obj['AltitudeStart']=''
                new_obj['AirmassStart']=''
                new_obj['AzimutStart']=''
                new_obj['AltitudeEnd']=''
                new_obj['AirmassEnd']=''
                new_obj['AzimutEnd']=''            
                new_obj['MoonSeparation']=''
            new_obj['Priority']=obj['Priority']
            new_obj['ExpTime']=obj['ExpTime']
            if obj['Number']=='series': new_obj['Number']=5
            else: new_obj['Number']=obj['Number']
            new_obj['Position']=''
            new_obj['Remarks']=new_obj['_Remarks']
            del(new_obj['_Remarks'])
            
            data[last+1]=new_obj            
            df=pd.DataFrame().from_dict(data,'index')
            
            cache.set(code,[pd.DataFrame(df),alt_plot,sky_plot])    #save in cache
            
            dfW=df.to_dict('records')
            
            calc=0
            
            if codeF: objects,obs=cache.get(codeF)
            else: 
                objects=[]
                obs=[]
        
        if 'delete' in request.form:
            #delete schedule
            os.remove('schedules/'+name+'.csv')   
            if os.path.isfile('schedules/'+name+'_alt.png'):
                os.remove('schedules/'+name+'_alt.png')
                os.remove('schedules/'+name+'_sky.png')
                
            name=''
            schedules=[os.path.splitext(os.path.basename(x))[0] for x in  sorted(glob.glob('schedules/*.csv'), key=os.path.getmtime)][::-1]  
            
            if codeF: objects,obs=cache.get(codeF)
            else: 
                objects=[] 
                obs=[]
            
            if code: 
                df,alt_plot,sky_plot=cache.get(code)
                dfW=df.to_dict('records')
            else: 
                dfW=[]
                alt_plot=''
                sky_plot=''
                
                  
        
        if 'new' in request.form:  
            #create new empty schedule
            code=''
            name=''
            dfW=[]
            alt_plot=''
            sky_plot=''
            startF=''
            nightF=''
           
            if codeF: objects,obs=cache.get(codeF)
            else: 
                objects=[]   
                obs=[]      
                
            calc=0  
            total_time=16
           
        
        if 'calc' in request.form or 'plot' in request.form:
            #recalculate schedule
            
            if not 'id' in request.form:
                if codeF: objects=cache.get(codeF)
                else: objects=[]
            
                return render_template('modify.html', schedules=schedules,name=name,schedule=[],code='', codeF=codeF, alt_plot='',sky='',start=request.form['start'],night=request.form['night'],groups=groups,use_group=use_group,objects=objects,calc=0,total_time=16,all_obj=sorted(all_obj.items(), key=lambda kv: (kv[1]['name'].lower().replace(' ',''), kv[0])),codeObj=codeObj,indObj=idObj,condi=condi,scroll=False,freq=freq)
            else: ids=[int(i) for i in request.form.to_dict(flat=False)['id']]          

            df0=pd.DataFrame(cache.get(code)[0])
        
            #sort and delete rows
            data={i-1: df0.to_dict('index')[i-1] for i in ids}
            df=pd.DataFrame().from_dict(data,'index')
                         
            # get data from inputs in tab
            updated_data = request.form.to_dict(flat=False)  
            exps=[int(round(float(x))) for x in updated_data['exp']]
            nums=[int(x) for x in updated_data['number']]
            nots=updated_data['notes']
            
            #update values in df
            df['ExpTime']=exps
            df['Number']=nums
            df['Remarks']=nots
            
            #get start time for schedule
            start=Time(request.form['night']+' '+request.form['start'])
            
            plantime=Time(request.form['night']+' '+str(12-int(round(observatory.longitude.value/15))).rjust(2,'0')+':00:00')    #approx. local noon (in UTC)
                        
            #calculate sunset, sunrise and midnight times + set some time ranges

            #sunrise/sunset calculation with 1 hour extend -> for scheduling intervals and plots
            #could be modified for speed up -> add horizon=-12*u.deg (nautical twilight) or -18 (astronomical) and remove additional hour.
            midnight=observatory.midnight(plantime,n_grid_points=10, which='next')
            suns=observatory.sun_set_time(midnight,n_grid_points=10, which='previous')-1*u.hour
            sunr=observatory.sun_rise_time(midnight,n_grid_points=10, which='next')+1*u.hour
            
            if start<suns+1*u.hour:
                if (start+1*u.day)>suns and (start+1*u.day)<sunr: start+=1*u.day  #obs. start after UT midnight
                else: start=observatory.sun_set_time(midnight,n_grid_points=10, which='previous',horizon=-12*u.deg)  #time is before sunset -> set begining of astro twilight
            
            #add targets to schedule
            schedule=Schedule(suns,sunr)
            schedule.observer = observatory
            col0={x: x[1:] for x in df.columns if x[0]=='_'}  #remeber additional params
            orig=df[col0.keys()].rename(columns=col0)
            orig['index']=df['index']
            orig['Remarks']=df['Remarks']
            objects0={}
            ha0=[]
            ha1=[]
            de=[]
            t00=0
            for i,obj in df.iterrows():  
                objects0[str(obj['index'])]={'full':orig[orig['index']==obj['index']].squeeze(),'mag':obj['Mag']}    #save all values     
                ra='{}h{}m{}s'.format(*obj.RA.replace(':',' ').replace(',','.').split())
                dec='{}d{}m{}s'.format(*obj.DEC.replace(':',' ').replace(',','.').split())
                coordinates=SkyCoord(ra,dec,frame='icrs')
                #rename obj using index to save them
                b=ObservingBlock.from_exposures(FixedTarget(name=str(obj['index']), coord=coordinates), obj.Priority, np.float32(obj.ExpTime)*u.second,obj.Number, read_out)
                b.observer = observatory
                if len(schedule.scheduled_blocks)>0:
                    tr=transitioner(schedule.scheduled_blocks[-1], b,schedule.scheduled_blocks[-1].end_time,observatory)
                    if tr is not None: 
                        #changing object
                        schedule.insert_slot(schedule.scheduled_blocks[-1].end_time,tr)
                        t0=schedule.scheduled_blocks[-1].end_time#+30*u.second  #add additional time to centering object ?   
                    else: t0=schedule.scheduled_blocks[-1].end_time
                else: t0=start   
                if t00==0: t00=t0
                schedule.insert_slot(t0, b)   
                
                lst0=observatory.local_sidereal_time(t0).deg*u.deg
                ha0.append((lst0-b.target.ra)%(360*u.deg))
                lst1=observatory.local_sidereal_time(schedule.scheduled_blocks[-1].end_time).deg*u.deg
                ha1.append((lst1-b.target.ra)%(360*u.deg)) 
                de.append(b.target.dec)
            
            total_time=(sunr-t00).sec/3600
                
            #calculate positions
            check_limits(schedule)
            
            tab=schedule_table(schedule,objects0)
            df=tab[~(tab['target']=='TransitionBlock')].to_pandas()
            df=df.rename(columns=cols)
            
            df['ha0']=ha0
            df['ha1']=ha1
            df['de']=de
            
            if 'plot' in request.form: alt_plot,sky_plot=web_plot(schedule)                
            else: alt_plot,sky_plot='',''
            cache.set(code,[pd.DataFrame(df),alt_plot,sky_plot])    #save in cache
            
            if codeF: objects,obs=cache.get(codeF)
            else: 
                objects=[]
                obs=[]
            
            dfW=df.to_dict('records')
            startF=start.strftime('%H:%M')  
            
            scroll=True
            
            calc=1  
            
        if 'run' in request.form:
            #re-schedule        
            if not 'id' in request.form:
                if codeF: objects=cache.get(codeF)
                else: objects=[]
            
                return render_template('modify.html', schedules=schedules,name=name,schedule=[],code='', codeF=codeF, alt_plot='',sky='',start=request.form['start'],night=request.form['night'],groups=groups,use_group=use_group,objects=objects,all_obj=sorted(all_obj.items(), key=lambda kv: (kv[1]['name'].lower().replace(' ',''), kv[0])),codeObj=codeObj,indObj=idObj,freq=freq,condi=condi)
            else: ids=[int(i) for i in request.form.to_dict(flat=False)['id']]          

            df0=pd.DataFrame(cache.get(code)[0])
        
            #sort and delete rows
            data={i-1: df0.to_dict('index')[i-1] for i in ids}
            df=pd.DataFrame().from_dict(data,'index')
                         
            # get data from inputs in tab
            updated_data = request.form.to_dict(flat=False)  
            exps=[int(round(float(x))) for x in updated_data['exp']]
            nums=[int(x) for x in updated_data['number']]
            nots=updated_data['notes']
            
            #update values in df
            df['ExpTime']=exps
            df['Number']=nums
            df['Remarks']=nots
            
            #get start time for schedule
            start=Time(request.form['night']+' '+request.form['start'])
            
            plantime=Time(request.form['night']+' '+str(12-int(round(observatory.longitude.value/15))).rjust(2,'0')+':00:00')    #approx. local noon (in UTC)
                        
            #calculate sunset, sunrise and midnight times + set some time ranges

            #sunrise/sunset calculation with 1 hour extend -> for scheduling intervals and plots
            #could be modified for speed up -> add horizon=-12*u.deg (nautical twilight) or -18 (astronomical) and remove additional hour.
            midnight=observatory.midnight(plantime,n_grid_points=10, which='next')
            suns=observatory.sun_set_time(midnight,n_grid_points=10, which='previous')-1*u.hour
            sunr=observatory.sun_rise_time(midnight,n_grid_points=10, which='next')+1*u.hour
            
            if start<suns+1*u.hour:
                if (start+1*u.day)>suns and (start+1*u.day)<sunr: start+=1*u.day  #obs. start after UT midnight
                else: start=observatory.sun_set_time(midnight,n_grid_points=10, which='previous',horizon=-12*u.deg)  #time is before sunset -> set begining of astro twilight
                
            night=sunr-suns
            obstime=suns+night*np.linspace(0, 1, 100)    #range of observing scheduling   
                
            #general constraints
            constraints = [ModifAltitudeConstraint(config['minAlt'],config['maxAlt'],boolean_constraint=True), 
                        ModifAirmassConstraint(config['airmass'],boolean_constraint=False),AtNightConstraint.twilight_nautical(), MoonSeparationConstraint(config['moon'])]
            constraints.append(TimeConstraint(start,sunr))  #schedule only same part of night
            
            Scheduler=SequentialScheduler
            
            #add objects
            col0={x: x[1:] for x in df.columns if x[0]=='_'}  #remeber additional params
            orig=df[col0.keys()].rename(columns=col0)
            orig['index']=df['index']
            orig['Remarks']=df['Remarks']
            objects1={}
            for i,obj in df.iterrows():  
                tmp={'full':orig[orig['index']==obj['index']].squeeze(),'mag':obj['Mag']}    #save all values   
                ra='{}h{}m{}s'.format(*obj.RA.replace(':',' ').replace(',','.').split())
                dec='{}d{}m{}s'.format(*obj.DEC.replace(':',' ').replace(',','.').split())
                coordinates=SkyCoord(ra,dec,frame='icrs')
                tmp['target']=FixedTarget(name=obj.Target, coord=coordinates)
                tmp['exp']=np.float32(obj.ExpTime)
                tmp['n_exp']=obj.Number
                tmp['priority']=obj.Priority                
                
                objects1[str(uuid.uuid4())]=tmp
                
            
            # Prefiltering
            objects=prefilter(objects1,constraints,observatory,obstime)
                
            n_selected=len(objects1)
            n_obs=len(objects)
            
            #fill blocks
            blocks=[]
            names={}
            for obj in objects:
                cons=[]
                if len(obj['full']['StartDate'])+len(obj['full']['EndDate'])>0:
                    #constraint on obs date
                    try: start=Time(obj['full']['StartDate'])
                    except: start=None
                    try: end=Time(obj['full']['EndDate'])
                    except: end=None
                    cons.append(TimeConstraint(start,end))
                if not pd.isna(obj['full']['MoonPhase']):
                    #constraint on Moon phase
                    try: cons.append(MoonIlluminationConstraint(0,float(obj['full']['MoonPhase'])))
                    except: pass
                    #TODO remove later?
                if (not pd.isna(obj['full']['StartPhase']) or not pd.isna(obj['full']['EndPhase'])) and len(str(obj['full']['StartPhase']))+len(str(obj['full']['EndPhase']))>0:
                    #phase constraint for EB or exoplanets
                    objPer=PeriodicEvent(epoch=Time(obj['full']['Epoch'],format='jd'),period=float(obj['full']['Period'])*u.day)
                    if pd.isna(obj['full']['StartPhase']) or len(str(obj['full']['StartPhase']))==0: start=None
                    else: start=float(obj['full']['StartPhase'])
                    if pd.isna(obj['full']['EndPhase']) or len(str(obj['full']['EndPhase']))==0: end=None
                    else: end=float(obj['full']['EndPhase'])
                    cons.append(PhaseConstraint(objPer,start,end))
                
                if obj['target'].name in names:  #repeating objects -> NOT replace debug plots
                    names[obj['target'].name]+=1
                    name1=obj['target'].name+'_s'+str(names[obj['target'].name])
                else:
                    names[obj['target'].name]=0
                    name1=obj['target'].name
                blocks.append(ObservingBlock.from_exposures(FixedTarget(name=name1, coord=obj['target'].coord),obj['priority'],obj['exp']*u.second,obj['n_exp'],read_out,constraints=cons))
                if blocks[-1].duration<60*u.second: blocks[-1].duration=60*u.second   #fix for short blocks
            
            
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
            
            code=str(uuid.uuid4())    #unique hash for different users
            cache.set(code,[schedule,objects1])    #save in cache
            
            return redirect(url_for('new_schedule', selected=n_selected, observable=n_obs,code=code))
        
        if 'twilight' in request.form:
            #set obs. start from astro. twilight
            plantime=Time(request.form['night']+' '+str(12-int(round(observatory.longitude.value/15))).rjust(2,'0')+':00:00')    #approx. local noon (in UTC)
            midnight=observatory.midnight(plantime,n_grid_points=10, which='next')
            start=observatory.sun_set_time(midnight,n_grid_points=10, which='previous',horizon=-12*u.deg)
            
            if code: 
                df,alt_plot,sky_plot=cache.get(code)
                dfW=df.to_dict('records')
            else: 
                dfW=[]
                alt_plot=''
                sky_plot=''     
                
            if codeF: objects,obs=cache.get(codeF)
            else: 
                objects=[]   
                obs=[]        
            
            startF=start.strftime('%H:%M')        
            
            calc=0             
            total_time=(observatory.sun_rise_time(midnight,n_grid_points=10, which='next')+1*u.hour-start).sec/3600  
        
        if 'save' in request.form:
            #save scheduler on server
            
            df,alt_plot,sky_plot=cache.get(code)
            
            name=request.form['save-name'].replace('/','_').replace('\\','_')
            if not '.csv' in name: name+='.csv'
            if '_index' in df.columns: df=df.drop(columns=['_index'])
            df.rename(columns=cols).to_csv('schedules/'+name,index=False)
            
            if len(alt_plot)>0:
                #save images
                f=open('schedules/'+name.replace('.csv','_alt.png'),'wb')              
                f.write(base64.b64decode(alt_plot.encode('utf8')))
                f.close()
                
                f=open('schedules/'+name.replace('.csv','_sky.png'),'wb')              
                f.write(base64.b64decode(sky_plot.encode('utf8')))
                f.close()
            elif os.path.isfile('schedules/'+name.replace('.csv','_alt.png')):
                os.remove('schedules/'+name.replace('.csv','_alt.png'))
                os.remove('schedules/'+name.replace('.csv','_sky.png'))
            
            return 'Schedule saved with name "'+name+'"!'    
        
        if 'download' in request.form:
            #download csv
            df=cache.get(code)[0]
            
            si = io.StringIO()    # create "file-like" output for writing
            
            if 'AltitudeStart' in df.columns:
                df['Altitude']=df.apply(lambda row: '%.0f - %.0f' %(float(row['AltitudeStart']),float(row['AltitudeEnd'])), axis=1)
                df['Azimut']=df.apply(lambda row: '%.0f - %.0f' %(float(row['AzimutStart']),float(row['AzimutEnd'])), axis=1)
                df['Airmass']=df.apply(lambda row: '%.1f - %.1f' %(float(row['AirmassStart']),float(row['AirmassEnd'])), axis=1)
            
            df[cols1].to_csv(si,index=False)
            output = make_response(si.getvalue())
            output.headers["Content-Disposition"] = "attachment; filename=schedule.csv"
            output.headers["Content-type"] = "text/csv"
            return output 
        
        if 'json' in request.form:
            #download json
            df=cache.get(code)[0]
            si = io.StringIO()    # create "file-like" output for writing
            
            if 'AltitudeStart' in df.columns:
                df['Altitude']=df.apply(lambda row: '%.0f - %.0f' %(float(row['AltitudeStart']),float(row['AltitudeEnd'])), axis=1)
                df['Azimut']=df.apply(lambda row: '%.0f - %.0f' %(float(row['AzimutStart']),float(row['AzimutEnd'])), axis=1)
                df['Airmass']=df.apply(lambda row: '%.1f - %.1f' %(float(row['AirmassStart']),float(row['AirmassEnd'])), axis=1)
            
            df[cols1].to_json(si,orient='records',index=False)
            output = make_response(si.getvalue())
            output.headers["Content-Disposition"] = "attachment; filename=schedule.json"
            output.headers["Content-type"] = "text/json"
            return output 
        
        if 'batch' in request.form:
            #download batch
            df=cache.get(code)[0]
            si = io.StringIO()    # create "file-like" output for writing
            
            df1=pd.DataFrame()
            df1['Target']=df['Target']
            df1['Object']='target'
            df1['Count repeat']=[int(x) for x in df['Number']]
            df1['Exposure length']=[int(x) for x in df['ExpTime']]
            tmp=[x.replace(':','').replace(' ','') for x in df['RA']]
            df1['RA']=[x if '.' in x else x+'.0' for x in tmp]
            tmp=[x.replace(':','').replace(' ','') for x in df['DEC']]
            df1['DEC']=[x if '.' in x else x+'.0' for x in tmp]
            df1['TelPos']=['east' if 'e' in x else 'western' for x in df['Position']]
            df1['Tracking']="on"
            df1['Sleep']=1
            df1['CamPos']=0
            df1['FocPos']=0
            df1['Filter']="same"
            df1['Grating angle']=""
            df1['Spectral filter']="same"
            df1['Count of pulses']=0.0
            df1['Iodine cell']=[('IC' in x) for x in df['Remarks']]
            df1['Enable']=True
            
            df1.to_json(si,orient='records',index=False)
            output = make_response(si.getvalue())
            output.headers["Content-Disposition"] = "attachment; filename=schedule_batch.json"
            output.headers["Content-type"] = "text/json"
            return output 
        
        return render_template('modify.html', schedules=schedules,name=name,schedule=dfW,code=code, codeF=codeF,alt_plot=alt_plot,sky=sky_plot,start=startF,night=nightF,groups=groups,use_group=use_group,objects=objects,obs=obs,indiv=indiv,calc=calc,total_time=total_time,all_obj=sorted(all_obj.items(), key=lambda kv: (kv[1]['name'].lower().replace(' ',''), kv[0])),codeObj=codeObj,indObj=idObj,condi=condi,scroll=scroll,freq=freq)
    else:
        all_obj=all_obj0
        
        codeObj=str(uuid.uuid4())
        cache.set(codeObj,dict(all_obj0))
        if len(all_obj)==0: codeObj=''
    
    gc.collect()
    
    return render_template('modify.html', schedules=schedules,name='',schedule=[],code='',codeF='', alt_plot='',sky='',start='',night='',groups=groups,use_group=[],objects=[],obs=[],indiv=True,calc=0,total_time=16,all_obj=sorted(all_obj.items(), key=lambda kv: (kv[1]['name'].lower().replace(' ',''), kv[0])),codeObj=codeObj,indObj='',condi='all',scroll=False,freq='all')

@app.route("/scheduler/limits", methods=['GET'])
def limits():
    '''plot object in telescope limits'''
    from astropy.coordinates import Angle
    
    gc.collect()
    
    if not session.get('logged_in'):
        return redirect(url_for('login', next=request.path))
    
    try:
        ha0=Angle(request.args.get('ha0'))
        ha1=Angle(request.args.get('ha1'))
        dec=Angle(request.args.get('dec'))
        title=request.args.get('title')
    except TypeError:
        return 'Incorrect format of input parameters!'
    
    fig=plot_limits(ha0,ha1,dec,title)
    #save to "buffer" file
    buf=io.BytesIO()
    plt.savefig(buf,format='png',dpi=150)
    plt.close()
    buf.seek(0)
    #load result from buffer to html output
    plot = base64.b64encode(buf.getvalue()).decode('utf8')
    buf.close()
    
    return render_template('image.html',image=plot) 
    

@app.route("/scheduler/show", methods=['GET','POST'])
def show():
    '''show created scheduler'''
    gc.collect()
    
    if not session.get('logged_in'):
        return redirect(url_for('login', next=request.path))
    

    schedules=[os.path.splitext(os.path.basename(x))[0] for x in  sorted(glob.glob('schedules/*.csv'), key=os.path.getmtime)][::-1]    
    
    if len(schedules)==0: return('NO schedules to show!!!')
    
    #cols to save in CSV
    cols=['Target','RA', 'DEC', 'Mag','ExpTime', 'Number','Remarks', 'Start', 'End','Position','Priority','Altitude','Azimut','Airmass']
    
    if request.method=='POST':
        name=request.form['name']    
        
        df=pd.read_csv('schedules/'+name+'.csv')
        df=df.fillna('')  #remove nan
        
        if 'download' in request.form:
            #download csv
            si = io.StringIO()    # create "file-like" output for writing
            
            if 'AltitudeStart' in df.columns:
                df['Altitude']=df.apply(lambda row: '%.0f - %.0f' %(row['AltitudeStart'],row['AltitudeEnd']), axis=1)
                df['Azimut']=df.apply(lambda row: '%.0f - %.0f' %(row['AzimutStart'],row['AzimutEnd']), axis=1)
                df['Airmass']=df.apply(lambda row: '%.1f - %.1f' %(row['AirmassStart'],row['AirmassEnd']), axis=1)
            
            df[cols].to_csv(si,index=False)
            output = make_response(si.getvalue())
            output.headers["Content-Disposition"] = "attachment; filename="+name+".csv"
            output.headers["Content-type"] = "text/csv"
            return output   
        
        if 'json' in request.form:
            #download json
            si = io.StringIO()    # create "file-like" output for writing
            
            if 'AltitudeStart' in df.columns:
                df['Altitude']=df.apply(lambda row: '%.0f - %.0f' %(row['AltitudeStart'],row['AltitudeEnd']), axis=1)
                df['Azimut']=df.apply(lambda row: '%.0f - %.0f' %(row['AzimutStart'],row['AzimutEnd']), axis=1)
                df['Airmass']=df.apply(lambda row: '%.1f - %.1f' %(row['AirmassStart'],row['AirmassEnd']), axis=1)
            
            df[cols].to_json(si,orient='records',index=False)
            output = make_response(si.getvalue())
            output.headers["Content-Disposition"] = "attachment; filename=schedule.json"
            output.headers["Content-type"] = "text/json"
            return output 
        
        if 'batch' in request.form:
            #download batch
            si = io.StringIO()    # create "file-like" output for writing
            
            df1=pd.DataFrame()
            df1['Target']=df['Target']
            df1['Object']='target'
            df1['Count repeat']=[int(x) for x in df['Number']]
            df1['Exposure length']=[int(x) for x in df['ExpTime']]
            tmp=[x.replace(':','').replace(' ','') for x in df['RA']]
            df1['RA']=[x if '.' in x else x+'.0' for x in tmp]
            tmp=[x.replace(':','').replace(' ','') for x in df['DEC']]
            df1['DEC']=[x if '.' in x else x+'.0' for x in tmp]
            df1['TelPos']=['east' if 'e' in x else 'western' for x in df['Position']]
            df1['Tracking']="on"
            df1['Sleep']=1
            df1['CamPos']=0
            df1['FocPos']=0
            df1['Filter']="same"
            df1['Grating angle']=""
            df1['Spectral filter']="same"
            df1['Count of pulses']=0.0
            df1['Iodine cell']=[('IC' in x) for x in df['Remarks']]
            df1['Enable']=True
            
            df1.to_json(si,orient='records',index=False)
            output = make_response(si.getvalue())
            output.headers["Content-Disposition"] = "attachment; filename=schedule_batch.json"
            output.headers["Content-type"] = "text/json"
            return output  
        
        #load config - based on observatory!
        config=load_config('lasilla_config.txt')
                
        observatory=config['observatory']
        read_out = config['read_out']    #read_out time of camera + comp (with readout) + ...
        #slew_rate = config['slew_rate']   #slew rate of the telescope
        
        
        #recalculate transitions
        #transitioner = Transitioner(slew_rate)
        
        start=Time(df.Start[0])
        end=Time(list(df.End)[-1])
        
        #sunrise/sunset calculation with 1 hour extend -> for scheduling intervals and plots
        #could be modified for speed up -> add horizon=-12*u.deg (nautical twilight) or -18 (astronomical) and remove additional hour.
        #get sunser/sunrise around the observing scheduler
        suns=observatory.sun_set_time(start,n_grid_points=10, which='previous')-1*u.hour
        sunr=observatory.sun_rise_time(end,n_grid_points=10, which='next')+1*u.hour 
        
        #add targets to schedule
        schedule=Schedule(suns,sunr)
        schedule.observer = observatory
        ha0=[]
        ha1=[]
        de=[]
        for i,obj in df.iterrows():
            ra='{}h{}m{}s'.format(*obj.RA.replace(':',' ').replace(',','.').split())
            dec='{}d{}m{}s'.format(*obj.DEC.replace(':',' ').replace(',','.').split())
            coordinates=SkyCoord(ra,dec,frame='icrs')
            b=ObservingBlock.from_exposures(FixedTarget(name=obj.Target, coord=coordinates), obj.Priority, obj.ExpTime*u.second,obj.Number, read_out)
            b.observer = observatory
            t0=Time(obj.Start)
            if len(schedule.scheduled_blocks)>0:
                tr=TransitionBlock.from_duration((t0-schedule.scheduled_blocks[-1].end_time).value*86400*u.second)             
                schedule.insert_slot(schedule.scheduled_blocks[-1].end_time,tr)                    
            schedule.insert_slot(t0, b)  
            
            lst0=observatory.local_sidereal_time(t0).deg*u.deg
            ha0.append((lst0-b.target.ra)%(360*u.deg))
            lst1=observatory.local_sidereal_time(schedule.scheduled_blocks[-1].end_time).deg*u.deg
            ha1.append((lst1-b.target.ra)%(360*u.deg)) 
            de.append(b.target.dec)
            
        df['ha0']=ha0
        df['ha1']=ha1
        df['de']=de
              
        if os.path.isfile('schedules/'+name+'_alt.png'):
            dt=datetime.fromtimestamp(os.path.getmtime('schedules/'+name+'.csv')) - datetime.fromtimestamp(os.path.getmtime('schedules/'+name+'_alt.png'))
            if dt.total_seconds()>5:
                #old images
                alt_plot,sky_plot=web_plot(schedule)
            
                f=open('schedules/'+name+'_alt.png','wb')              
                f.write(base64.b64decode(alt_plot.encode('utf8')))
                f.close()
                
                f=open('schedules/'+name+'_sky.png','wb')              
                f.write(base64.b64decode(sky_plot.encode('utf8')))
                f.close()
            else:
                #load saved images
                f=open('schedules/'+name+'_alt.png','rb')
                alt_plot=base64.b64encode(f.read()).decode('utf8')
                f.close()
                
                f=open('schedules/'+name+'_sky.png','rb')
                sky_plot=base64.b64encode(f.read()).decode('utf8')
                f.close()                             
        else: 
            alt_plot,sky_plot=web_plot(schedule)
            
            f=open('schedules/'+name+'_alt.png','wb')              
            f.write(base64.b64decode(alt_plot.encode('utf8')))
            f.close()
            
            f=open('schedules/'+name+'_sky.png','wb')              
            f.write(base64.b64decode(sky_plot.encode('utf8')))
            f.close()
    
        gc.collect()
        
        return render_template('show_schedule.html', schedules=schedules,name=name,schedule=df.to_dict('records'), alt_plot=alt_plot,sky=sky_plot)
        
    name=schedules[0]   
             
    return render_template('show_schedule.html', schedules=schedules,name=name,schedule=[], alt_plot='',sky='')


@app.route('/scheduler/object',methods=['GET'])
def object_info():
    gc.collect()
    
    if not session.get('logged_in'):
        return redirect(url_for('login', next=request.path))
    if session.get('logged_in')=='admin': 
        keys=['program_title','code','name','institution','mail','mode','strategy','calibration','readout','special_requests','instructions']
    else: 
        keys=['program_title','name','institution','mode','strategy','calibration','readout','special_requests','instructions']
        
    if 'name' in request.args: name0=request.args['name']
    else: return 'Missing object name!'
    
    if 'done' in request.args: done=request.args['done']
    else: done='0'
    
    name=name0.lower().replace(' ', '').replace('-','').replace('_','').replace('+','').replace('.','')
    
    P=''
    t0=''
    objects=[]
    f=open('db/objects.csv','r')
    reader = csv.DictReader(f)
    for obj in reader:
        if obj['Done']==done and obj['Target'].lower().replace('-','').replace(' ','').replace('_','')==name: 
            objects.append(obj)    #load object and select only obj. for observations
            if not (P and t0):
                if obj['Period']: P=obj['Period']
                if obj['Epoch']: t0=obj['Epoch']
    f.close()          
    
    if len(objects)==0: return 'Object "'+name0+'" not found in DB!'
    
    f=open('db/progID.json','r')
    progs=json.load(f)
    f.close()
    
    programs=[]
    for ob in objects:
        prID=ob['ProgramID']
        if prID in progs: programs.append(progs[prID])
        else: programs.append({})
        
        
    make_stats()    
    last=''
    nobs=0
    snr=''
    obs_name=''
    if os.path.isfile('db/observations.json'): 
        f=open('db/names.json','r')
        names=json.load(f)
        f.close()
        
        if name in names:
            obs_name=names[name]
            
            f=open('db/observations.json','r')
            obsAll=json.load(f)
            f.close()
            
            if obs_name in obsAll:            
                obs=obsAll[obs_name]
                nobs=len(obs)
                last=obs[-1]
                
                #read logs
                snrs=[]
                for night in obs:
                    f=open('static/logs/'+night+'_log.csv','r')
                    reader = csv.DictReader(f)
                    for row in reader:
                        #utilize similar objects names - spaces, lower/upper case etc.
                        if name==row['object'].replace('?','').replace('ttarget-','').replace('ttarget_','').replace('-thar','').replace('_thar','').replace('_',' ').strip().lower().replace('-','').replace(' ','').replace('+','').replace('.',''):                  
                            if 'snr' in row:
                                if len(row['snr'])>0: snrs.append(float(row['snr']))
                                else: continue                        
                            else: continue  
                    f.close() 
                snr=round(np.mean(snrs),1)                

    
    gc.collect()
    
    return render_template('object.html',obj=objects,prog=programs,P=P,t0=t0,day=datetime.now(timezone.utc).strftime('%Y-%m-%d'),time=datetime.now(timezone.utc).strftime('%H:%M'),keys=keys,last=last,nobs=nobs,snr=snr,obs_name=obs_name)


	

@app.route('/scheduler/proposal',methods=['GET'])
def proposal_info():
    gc.collect()
    
    if not session.get('logged_in'):
        return redirect(url_for('login', next=request.path))
    if session.get('logged_in')=='admin': 
        keys=['program_title','code','name','institution','mail','mode','strategy','calibration','readout','special_requests','instructions']
    else: 
        keys=['program_title','name','institution','mode','strategy','calibration','readout','special_requests','instructions']
        
    if 'name' in request.args: name=request.args['name'].strip()
    else: return 'Missing proposal name!'
    
    if len(name)==0: return 'Missing proposal name!'
        
    f=open('db/progID.json','r')
    progs=json.load(f)
    f.close()
    
    program={}
    if name[0]=='2':
        #search by ID
        if name in progs: program=progs[name]         
    else:
        for prog in progs:
            if name in progs[prog]['program_title']:
                program=progs[prog]
                break
    
    if len(program)==0: return 'Proposal "'+name+'" not found!'        
    
    gc.collect()
    
    return render_template('proposal.html',prog=program,keys=keys)

@app.route('/scheduler/check_output', methods=['GET'])
def check_output():
    ''' Endpoint to check if user filter finished'''
    if not 'output_ready' in session: return "working", 202    
    if session.get('output_ready'):
        session['output_ready']=False    #reset to default state, after obtain results
        return "ready", 200
    return "working", 202

@app.route("/scheduler/user", methods=['GET','POST'])
def user():
    '''import user objects and prefilter them (+schedule?)'''
    gc.collect()
    session['output_ready']=False
    
    #load observatories
    f=open('static/sites.json','r')
    data=json.load(f)
    f.close()
    
    observatories={}
    for o in data:
        tmp={'lat':data[o]['latitude'], 'lon':data[o]['longitude'], 'alt':data[o]['elevation']}
        #use all names
        observatories[o]=dict(tmp)
        observatories[data[o]['name']]=dict(tmp)
        for alias in data[o]['aliases']:
            if len(alias)>0: observatories[alias]=dict(tmp)
            
    errors={}
    
    if request.method=='POST':
        session['output_ready']=False
        
        obs=request.form['obs']
        lat=float(request.form['lat'])*u.deg
        lon=float(request.form['lon'])*u.deg
        ele=float(request.form['alt'])*u.m
        
        minAlt=float(request.form['minAlt'])*u.deg
        airmass=float(request.form['airmass'])
        moon=float(request.form['moon'])*u.deg
        
        readout=request.form['readout']
        slew=request.form['slew']
        
        time_start=request.form['start']
        time_end=request.form['end']
        
        scheduler=request.form['scheduler']     
        
        if not(time_start): errors['start']='Start time is missing.'
        if not(time_end): errors['end']='End time is missing.' 
        if time_start and time_end:
            if Time(time_end)<Time(time_start): errors['times']='End time is smaller than Start time.'       
        
        #get file with obj
        if 'file' not in request.files:
            errors['file']="No file part."
        file = request.files['file']
        if file.filename == '':
            errors['file']="No selected file."
            
        if errors:
            return render_template('user.html',obs=obs,lat=lat.value,lon=lon.value,alt=ele.value,minAlt=minAlt.value,airmass=airmass,moon=moon.value,readout=readout,slew=slew,start=time_start, end=time_end, errors=errors,observatories=observatories,scheduler=scheduler)
        
        output = io.StringIO()   # create "file-like" output for writing
        errors['data']=[]
        if file:
            #read data from input file and save them in list
            file_content = [x.decode() for x in file.readlines()]
            csvreader = csv.DictReader(file_content)
            if not (csvreader.fieldnames+['Supervisor'])==header.strip().split(','):
                errors['data'].append('Incorrect CSV format. Check the template!')  
                return render_template('user.html',obs=obs,lat=lat.value,lon=lon.value,alt=ele.value,minAlt=minAlt.value,airmass=airmass,moon=moon.value,readout=readout,slew=slew,start=time_start, end=time_end, errors=errors,observatories=observatories,scheduler=scheduler)
            
            csvwriter = csv.DictWriter(output,fieldnames=csvreader.fieldnames)
            csvwriter.writeheader()
            for row in csvreader:
                #check inputs!!! 
                if len(row['Target'])==0:
                    errors['data'].append('Missing name of target.')
                    continue
                
                row,err=check(row)
                errors['data']+=err                   
                csvwriter.writerow(row)
                
        if len(errors['data'])==0: del(errors['data'])

        if errors:
            return render_template('user.html',obs=obs,lat=lat.value,lon=lon.value,alt=ele.value,minAlt=minAlt.value,airmass=airmass,moon=moon.value,readout=readout,slew=slew,start=start, end=end, errors=errors,observatories=observatories,scheduler=scheduler,azm_start=request.form['azm_start'], azm_end=request.form['azm_end'],azm=(request.form.get('azm')=='checked'))
        
        output.seek(0)
        objects0=load_objects(output,check=False)            
        
        #define observatory
        observatory=Observer(name=obs,longitude=lon,latitude=lat,elevation=ele)
        #general constraints
        constraints = [ModifAltitudeConstraint(min=minAlt,max=90*u.deg,boolean_constraint=False), 
                    AirmassConstraint(max=airmass,boolean_constraint=True),AtNightConstraint.twilight_nautical(), MoonSeparationConstraint(moon),TimeConstraint(Time(time_start),Time(time_end))]
            
        #set azimuth constr.
        if (request.form.get('azm')=='checked'):
            azm0=None
            if request.form['azm_start']: 
                azm0=float(request.form['azm_start'])*u.deg
                if azm0<0*u.deg: azm0+=360*u.deg
                if azm0>360*u.deg: azm0-=360*u.deg
            azm1=None
            if request.form['azm_end']: 
                azm1=float(request.form['azm_end'])*u.deg
                if azm1<0*u.deg: azm1+=360*u.deg
                if azm1>360*u.deg: azm1-=360*u.deg
            constraints.append(AzimuthConstraint(azm0,azm1))
        
        suns=observatory.sun_set_time(Time(time_start),n_grid_points=10, which='nearest')-1*u.hour
        sunr=observatory.sun_rise_time(Time(time_end),n_grid_points=10, which='nearest')+1*u.hour
        
        obstime=Time(time_start)+np.arange((Time(time_end)-Time(time_start)).value,step=10/1440)   #step 10 minutes     
    
        if 'filter' in request.form:
            session['output_ready']=False   
            
            #apply individual const.
            objects=[]
            for obj in objects0:
                cons=list(constraints)                        
                
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
                if not pd.isna(obj['full']['StartPhase']) or not pd.isna(obj['full']['EndPhase']):
                    #phase constraint for EB or exoplanets
                    objPer=PeriodicEvent(epoch=Time(obj['full']['Epoch'],format='jd'),period=obj['full']['Period']*u.day)
                    if pd.isna(obj['full']['StartPhase']): start=None
                    else: start=obj['full']['StartPhase']
                    if pd.isna(obj['full']['EndPhase']): end=None
                    else: end=obj['full']['EndPhase']
                    cons.append(PhaseConstraint(objPer,start,end))
                    
                if is_observable(cons, observatory, obj['target'], obstime): 
                    objects.append(obj)           
                            
            si = io.StringIO()  # create "file-like" output for writing
            
            tab=[x['full'] for x in objects]
            si.write(','.join(header.strip().split(',')[:-1])+'\n')
            for obj in tab:
                tmp=''
                for x in obj:
                    if isinstance(x,str):
                        if ',' in x: tmp+='"'+x+'",'
                        else: tmp+=str(x)+','
                    elif pd.isna(x): tmp+=','
                    else: tmp+=str(x)+','
                si.write(tmp[:-1]+'\n')
            
            output = make_response(si.getvalue())
            output.headers["Content-Disposition"] = "attachment; filename="+os.path.splitext(os.path.basename(file.filename))[0]+"_filtered.csv"
            output.headers["Content-type"] = "text/csv"
            
            session['output_ready']=True
            return output 
        
        if 'scheduler' in request.form:
            session['output_ready']=False    
            
            read_out = float(readout)*u.second     #read_out time of camera + comp (with readout) + ...
            slew_rate = slew*u.deg/u.minute   #slew rate of the telescope
            
            #set used scheduler: SequentialScheduler / PriorityScheduler -> select on web
            if scheduler=='Sequential': Scheduler=SequentialScheduler
            elif scheduler=='Priority': Scheduler=PriorityScheduler
            
            objects1={}
            for obj in objects0:
                objects1[str(uuid.uuid4())]=obj
            
            # Prefiltering
            #only general constraints
            objects=prefilter(objects1,constraints,observatory,obstime)
            
            # presort needed mainly for Seq. scheduler
            if scheduler=='Sequential':
                objects=presort(objects, observatory, Time(time_start)+(Time(time_end)-Time(time_start))/2,key='meridian')     #meridian/set/rise
                
            n_selected=len(objects0)
            n_obs=len(objects)

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
                        blocks.append(ObservingBlock.from_exposures(FixedTarget(name=name1, coord=obj['target'].coord),obj['priority'],obj['exp']*u.second,5,read_out,constraints=cons))
                else:
                    if obj['target'].name in names:  #repeating objects -> NOT replace debug plots
                        names[obj['target'].name]+=1
                        name1=obj['target'].name+'_s'+str(names[obj['target'].name])
                    else:
                        names[obj['target'].name]=0
                        name1=obj['target'].name
                    blocks.append(ObservingBlock.from_exposures(FixedTarget(name=name1, coord=obj['target'].coord),obj['priority'],obj['exp']*u.second,obj['n_exp'],read_out,constraints=cons))
            
            transitioner = Transitioner(slew_rate)
            scheduler = Scheduler(constraints = constraints,observer = observatory,transitioner = transitioner)
            schedule = Schedule(suns,sunr)     #start and end of scheduling interval
            scheduler(blocks, schedule)
            
            code=str(uuid.uuid4())    #unique hash for different users
            cache.set(code,[schedule,objects1])    #save in cache
                
            return redirect(url_for('new_schedule', selected=n_selected, observable=n_obs,code=code,user=True))
           
    
    return render_template('user.html',obs='Ondrejov',lat=49.910555556,lon=14.783611111,alt=528,minAlt=20,airmass=5,moon=15,readout=30,slew=20,start= datetime.now(timezone.utc).strftime('%Y-%m-%d')+'T18:00', end=(datetime.now(timezone.utc)+timedelta(days=1)).strftime('%Y-%m-%d')+'T06:00', errors={},observatories=observatories,scheduler='Priority')


@app.route("/scheduler/stats")
def stats():
    '''display stats of obs'''
    gc.collect()
    if not session.get('logged_in'):
        return redirect(url_for('login', next=request.path))
    make_stats()
    
    if not os.path.isfile('db/statistics.csv'):
        return render_template('stats.html', stats={})
    #load stats from file
    statistics={}
    f=open('db/statistics.csv','r')
    lines=f.readlines()
    f.close()
    for l in lines[1:]:
        tmp=l.strip().split(',')
        target=tmp[0]
        inst=tmp[1]
        exp=float(tmp[2])
        n=int(tmp[3])
        last=tmp[4]
        #utilize similar objects names - spaces, lower/upper case etc.
        tr=target.lower().replace('-','').replace(' ','').replace('+','').replace('.','')
        if tr in statistics:
            if inst in statistics[tr]:
                statistics[tr][inst][exp]={'n':n,'last':last}
            else: statistics[tr][inst]={exp:{'n':n,'last':last}}
        else: statistics[tr]={inst:{exp:{'n':n,'last':last}}, 'name': target}
            
    return render_template('stats.html', stats={x: statistics[x] for x in sorted(list(statistics.keys()), key=lambda v: v.upper())})

@app.route('/scheduler/logs', methods=['GET', 'POST'])
def logs():
    '''show log files'''
    gc.collect()
    if not session.get('logged_in'):
        return redirect(url_for('login', next=request.path))
    directory = "static/logs"   #path to logs
    file_extension=".pdf"   #selected ext. (pdf/csv)
    error=''
    
    #list all log files
    files = [os.path.splitext(os.path.basename(x))[0] for x in sorted(glob.glob(directory+'/*_log'+file_extension))[::-1]]
    if len(files)==0: return render_template('logs.html',night='',type=file_extension,files=[],error='NO log exists!')
    obs=files[0].replace('_log','') #latest log
    
    if request.method == 'POST':
        error=''
        if request.form['night']: obs = request.form['night']  #selected night
        file_extension = request.form['type']   #selected ext. (pdf/csv)
        
        if file_extension=='notes':
            files = [os.path.splitext(os.path.basename(x))[0] for x in sorted(glob.glob(directory+'/general*.csv'))[::-1]]
            files+=[os.path.splitext(os.path.basename(x))[0] for x in sorted(glob.glob(directory+'/meteo*.csv'))[::-1]]
        else:  
            #check if log for selected night exist
            if os.path.isfile(directory+'/'+obs+'_log'+file_extension): error=''
            else: error='NO log exists!'
        
        if 'download' in request.form:
            #download log for selected night
            if os.path.isfile(directory+'/'+obs+'_log'+file_extension):
                return send_file(directory+'/'+obs+'_log'+file_extension, download_name=obs+'_log'+file_extension, as_attachment=True)  
                   
    return render_template('logs.html',night=obs,type=file_extension,files=files,error=error)


@app.route("/scheduler/search", methods=['GET', 'POST'])
def search():
    '''search for obs of obj'''
    gc.collect()
    if not session.get('logged_in'):
        return redirect(url_for('login', next=request.path))
    make_stats()
    if not os.path.isfile('db/observations.json'):
        return render_template('search.html',obj=[],target='',obs={},errors={},obs_plot={},t0='',P='')
    
    #load stats
    f=open('db/observations.json','r')
    obs_all=json.load(f)
    f.close()
    
    obj=sorted(list(obs_all.keys()), key=lambda v: v.replace('-','').replace(' ','').upper())  #get obj names - as list on page, sort ignoring upper/lower case
    
    #utilize similar objects names - spaces, lower/upper case etc.
    modif_obs={x.lower().replace('-','').replace(' ',''): obs_all[x] for x in obs_all}
    
    target=None
    if request.method=='GET':
        #page was run from stats page -> display obs of selected obj
        if 'target' in request.args: target=request.args['target']
    
    if request.method == 'POST' or target is not None:
        if target is None: target=request.form['target']      #selecting target from list
               
        if not target.lower().replace('-','').replace(' ','') in modif_obs:
            #NOT-observed target - shouldn't be, but for sure...
            error={'target':target+' not observed!'}
            return render_template('search.html',obj=obj,target=target,obs={},errors=error,obs_plot={},t0='',P='')
            
        #load DB and get P,t0
        P=''
        t0=''
        f=open('db/objects.csv','r')
        reader = csv.DictReader(f)
        name=target.lower().replace('-','').replace(' ','').replace('_','').replace('+','').replace('.','')
        for row in reader:
            if name==row['Target'].lower().replace('-','').replace(' ','').replace('_','').replace('+','').replace('.',''):
                if len(row['Period'])>0: 
                    P=row['Period']
                    t0=row['Epoch']
                    break
        f.close()
            
        obs={}
        obs_plot={'t':[],'exp':[],'snr':[]}
        #list all obs of selected obj
        for night in sorted(modif_obs[target.lower().replace('-','').replace(' ','').replace('+','').replace('.','')])[::-1]:
            obs[night]={}
            
            #read info from logs
            f=open('static/logs/'+night+'_log.csv','r')
            reader = csv.DictReader(f)
            for row in reader:
                #utilize similar objects names - spaces, lower/upper case etc.
                if target.lower().replace('-','').replace(' ','').replace('+','').replace('.','')==row['object'].replace('?','').replace('ttarget-','').replace('ttarget_','').replace('-thar','').replace('_thar','').replace('_',' ').strip().lower().replace('-','').replace(' ','').replace('+','').replace('.',''):
                    exp=row['exposure']
                    inst=row['instrument']
                    if inst in obs[night]:
                        if exp in obs[night][inst]: obs[night][inst][exp]+=1
                        else: obs[night][inst][exp]=1
                    else: obs[night][inst]={exp: 1}                    
                    if 'snr' in row:
                        if len(row['snr'])>0: 
                            obs_plot['t'].append(row['date']+' '+row['time'])
                            obs_plot['exp'].append(float(row['exposure']))
                            obs_plot['snr'].append(float(row['snr']))
                        else: continue #obs_plot['snr'].append(1)                            
                    else: continue #obs_plot['snr'].append(1)    
            f.close()        
        
        return render_template('search.html',obj=obj,target=target,obs=obs,errors={},obs_plot=obs_plot,t0=t0,P=P)
    
    return render_template('search.html',obj=obj,target='',obs={},errors={},obs_plot={},t0='',P='')

@app.route('/scheduler/test_limits',methods=['GET','POST'])
def test_limits():
    gc.collect()
    if not session.get('logged_in'):
        return redirect(url_for('login', next=request.path))
    
    ra0=None
    
    if request.method == 'GET':
        if 'ra' in request.args:
            ra0=request.args['ra']
            dec0=request.args['dec']
            date=datetime.now(tz=timezone.utc).strftime('%Y-%m-%d')
            time=datetime.now(tz=timezone.utc).strftime('%H:%M')
        
    
    if request.method == 'POST':
        date=request.form['date']
        time=request.form['time']
        ra0=request.form['ra']
        dec0=request.form['dec']
    
    if ra0 is not None:
        sign = lambda x: -1 if x < 0 else 1
        ra=[float(x) for x in ra0.replace(':',' ').split()]
        ra=sign(ra[0])*(abs(ra[0])+ra[1]/60+ra[2]/3600)*15
        
        dec=[float(x) for x in dec0.replace(':',' ').split()]
        dec=sign(dec[0])*(abs(dec[0])+dec[1]/60+dec[2]/3600)
        
        dt=Time(date+' '+time)
        
        lst=dt.sidereal_time('mean',obs_lon*u.deg).degree
        
        ha=(lst-ra)%(360)
        
        if ha<-90: ha+=360
        if ha>270: ha-=360
            
        haW=ha+180
        if haW>270: haW-=360
        decW=-180-dec
        
        eastLim, westLim = load_limits()
        PathE=mplPath.Path(eastLim)
        PathW=mplPath.Path(westLim)
        
        if PathE.contains_point((ha,dec)):
            i=np.where((eastLim[:,0]>ha)*(eastLim[:,0]>5))[0]   #use only right part of limit
            j=np.argmin(np.abs(eastLim[i,1]-dec))
            try:
                f = interpolate.interp1d(eastLim[i,1],eastLim[i,0])
                east=float((f(dec)-ha)/15)
            except: east=(eastLim[i[j],0]-ha)/15
        else: east=0
        
        if PathW.contains_point((haW,decW)):
            i=np.where((westLim[:,0]>haW)*(westLim[:,0]>180))[0]    #use only right part of limit
            j=np.argmin(np.abs(westLim[i,1]-decW))
            try:
                f = interpolate.interp1d(westLim[i,1],westLim[i,0])
                west=float((f(decW)-haW)/15)
            except: west=(westLim[i[j],0]-haW)/15   
        else: west=0     
        
        buf=io.BytesIO()
        fig=plot_limits(ha*u.deg,ha*u.deg,dec*u.deg)
        plt.savefig(buf,format='png',dpi=150)
        plt.close()
        buf.seek(0)
        #load result from buffer to html output
        plot = base64.b64encode(buf.getvalue()).decode('utf8')
        buf.close()
        
        return render_template('limits.html',ra=ra0,dec=dec0,date=date,time=time,plot=plot,east=east,west=west)
    
    gc.collect()
    
    return render_template('limits.html',ra='',dec='',date='',time='')

if __name__ == '__main__':
   app.run('0.0.0.0',5000)
   
