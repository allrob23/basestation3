#!/usr/bin/env python3.10
## Copyright (c) 2023  University of Washington.
## 
## Redistribution and use in source and binary forms, with or without
## modification, are permitted provided that the following conditions are met:
## 
## 1. Redistributions of source code must retain the above copyright notice, this
##    list of conditions and the following disclaimer.
## 
## 2. Redistributions in binary form must reproduce the above copyright notice,
##    this list of conditions and the following disclaimer in the documentation
##    and/or other materials provided with the distribution.
## 
## 3. Neither the name of the University of Washington nor the names of its
##    contributors may be used to endorse or promote products derived from this
##    software without specific prior written permission.
## 
## THIS SOFTWARE IS PROVIDED BY THE UNIVERSITY OF WASHINGTON AND CONTRIBUTORS “AS
## IS” AND ANY EXPRESS OR IMPLIED WARRANTIES, INCLUDING, BUT NOT LIMITED TO, THE
## IMPLIED WARRANTIES OF MERCHANTABILITY AND FITNESS FOR A PARTICULAR PURPOSE ARE
## DISCLAIMED. IN NO EVENT SHALL THE UNIVERSITY OF WASHINGTON OR CONTRIBUTORS BE
## LIABLE FOR ANY DIRECT, INDIRECT, INCIDENTAL, SPECIAL, EXEMPLARY, OR
## CONSEQUENTIAL DAMAGES (INCLUDING, BUT NOT LIMITED TO, PROCUREMENT OF SUBSTITUTE
## GOODS OR SERVICES; LOSS OF USE, DATA, OR PROFITS; OR BUSINESS INTERRUPTION)
## HOWEVER CAUSED AND ON ANY THEORY OF LIABILITY, WHETHER IN CONTRACT, STRICT
## LIABILITY, OR TORT (INCLUDING NEGLIGENCE OR OTHERWISE) ARISING IN ANY WAY OUT
## OF THE USE OF THIS SOFTWARE, EVEN IF ADVISED OF THE POSSIBILITY OF SUCH DAMAGE.


from orjson import dumps,loads
import time
import os
import os.path
from parse import parse
import sys
from zipfile import ZipFile
from io import BytesIO
from anyio import Path
import websockets
import sqlite3
import aiosqlite
import aiofiles
import asyncio
import aiohttp
import sanic
import sanic_gzip
import sanic_ext
from functools import wraps,partial
import jwt
from passlib.hash import sha256_crypt
from types import SimpleNamespace
import yaml
import LogHTML
import summary
import multiprocessing
import getopt
import base64
import re
import zmq
import zmq.asyncio
import Utils
import secrets
import BaseDB
import ExtractTimeseries

PERM_INVALID = -1
PERM_REJECT = 0
PERM_VIEW   = 1
PERM_PILOT  = 2

MODE_PUBLIC  = 0
MODE_PILOT   = 1
MODE_PRIVATE = 2

AUTH_ENDPOINT = 1
AUTH_MISSION  = 2 

runModes = { 'public': MODE_PUBLIC, 'pilot': MODE_PILOT, 'private': MODE_PRIVATE }
modeNames = ['public', 'pilot', 'private']

protectableRoutes = [
                        'plot',     # dive plot webp/div file
                        'map',      # leafly map page
                        'kml',      # glider mission KML
                        'data',     # unused - data download?
                        'proxy',    # mini proxy server (for map pages)
                        'plots',    # get list of plots for a dive
                        'log',      # get log summary 
                        'file',     # get log, eng, cap file
                        'alerts',   # get alerts
                        'deltas',   # get changes (parameters and files) between dives
                        'changes',  # get parameter changes
                        'summary',  # get mission summary for a glider
                        'status',   # get basic mission staus (current dive) and eng plot list
                        'control',  # get a control file (cmdfile, etc.)
                        'db',       # get data for glider mission table
                        'dbvars',   # get list of per dive mission variables
                        'pro',      # get profiles
                        'provars',  # get list of profile variables
                        'time',     # get dive time series
                        'timevars', # get list of dive time series variables
                        'query',    # get select per dive data for interactive plots and tables
                        'selftest', # get latest selftest
                        'save',     # save control file
                        'stream',   # web socket stream for glider app page in pilot mode
                        'watch',    # web socket stream for live updates of mission and index pages
                        'chat',     # post a message to chat
                    ]

    # unprotectable: /auth, /, /GLIDERNUM, /missions
    # but /GLIDERNUM could probably be protected if we wanted to?
    # credentials at the dash level via the Credentials link?

compress = sanic_gzip.Compress()

# making this a dict makes a set intersection simple when
# we use it in filterMission
publicMissionFields = {"glider", "mission", "path", 
                       "started", "ended", "planned",
                       "orgname", "orglink", "contact", "email",
                       "project", "link", "comment", "reason"} 

# which modes need full comm.log stream vs just change notices

def getTokenUser(request):
    if request.app.config.SINGLE_MISSION:
        return (request.app.config.USER, False)

    if not 'token' in request.cookies:
        return (False, False)

    try:
         token = jwt.decode(request.cookies.get("token"), request.app.config.SECRET, algorithms=["HS256"])
    except jwt.exceptions.InvalidTokenError:
        return (False, False)

    if 'user' in token and 'groups' in token:
        return (token['user'], token['groups'])

    return (False, False)

# checks whether the auth token authorizes a user or group in users, groups
def checkToken(request, users, groups, pilots, pilotgroups):
    if not 'token' in request.cookies:
        return PERM_REJECT

    (tokenUser, tokenGroups) = getTokenUser(request)
    if not tokenUser:
        return PERM_REJECT

    perm = PERM_REJECT

    if users and tokenUser in users:
        sanic.log.logger.info(f"{tokenUser} authorized [{request.path}]")
        perm = PERM_VIEW
    elif groups and tokenGroups and len(set(groups) & set(tokenGroups)) > 0:
        sanic.log.logger.info(f"{tokenUser} authorized based on group [{request.path}]")
        perm = PERM_VIEW

    if pilots and tokenUser in pilots:
        perm = PERM_PILOT
        sanic.log.logger.info(f"{tokenUser} authorized to pilot [{request.path}]")
    elif pilotgroups and tokenGroups and len(set(pilotgroups) & set(tokenGroups)) > 0:
        sanic.log.logger.info(f"{tokenUser} authorized to pilot based on group [{request.path}]")
        perm = PERM_PILOT

    return perm

# checks whether access is authorized for the glider,mission
def checkGliderMission(request, glider, mission, perm=PERM_VIEW):

    # find the entry in the table matching glider+mission (mission could be None)
    m = matchMission(glider, request, mission)
    if m:
        # if there are any user/group/pilot restrictions associated with
        # the matched mission, check the token
        if (m['users'] is not None or \
            m['groups'] is not None or \
            m['pilotusers'] is not None or \
            m['pilotgroups'] is not None): 

            grant = checkToken(request, m['users'], m['groups'], m['pilotusers'], m['pilotgroups'])
            if m['users'] is None and m['groups'] is None and grant < PERM_VIEW:
                grant = PERM_VIEW

            return grant
        else:
            return perm
    
    # no matching mission in table - do not allow access
    sanic.log.logger.info(f'rejecting {glider} {mission} for no mission entry')
    return PERM_INVALID

def checkEndpoint(request, e):

    if e['users'] is not None or e['groups'] is not None:
        (tU, tG) = getTokenUser(request)
        allowAccess = False

        if tU and e['users'] and tU in e['users']:
            allowAccess = True
        elif tG and e['groups'] and len(set(tG) & set(e['groups'])) > 0:
            allowAccess = True

        if not allowAccess:
            sanic.log.logger.info(f"rejecting {request.path}: user auth required")
            return PERM_REJECT # so we respond "auth failed"

    return PERM_VIEW # don't make a distinction view/pilot at this level

def authorized(modes=None, check=3, requirePilot=False): # check=3 both endpoint and mission checks applied
    def decorator(f):
        @wraps(f)
        async def decorated_function(request, *args, **kwargs):
            nonlocal modes
            nonlocal check
            nonlocal requirePilot

            url = request.server_path[1:].split('/')[0]
            if check & AUTH_ENDPOINT:
                if url in request.app.ctx.endpoints:
                    e = request.app.ctx.endpoints[url]
                    status = checkEndpoint(request, e)
                    if status == PERM_INVALID:
                        return sanic.response.text("Page not found: {}".format(request.path), status=404)
                    elif status == PERM_REJECT:
                        return sanic.response.text("authorization failed")
                    else:
                        if 'modes' in e and e['modes'] is not None:
                            modes = e['modes']
                        if 'requirepilot' in e and e['requirepilot'] is not None:
                            requirePilot = e['requirepilot']

            runningMode = modeNames[request.app.config.RUNMODE]

            if check & AUTH_MISSION:
                defaultPerm = PERM_VIEW

                # on an open pilot server (typically a non-public server running 443) 
                # we require positive authentication as a pilot against mission specified 
                # list of allowed pilots (and pilotgroups). Access to missions without 
                # pilots: and/or pilotgroups: specs will be denied for all. 
                glider = kwargs['glider'] if 'glider' in kwargs else None
                mission = request.args['mission'][0] if 'mission' in request.args else None

                m = next(filter(lambda d: d['glider'] == glider and d['mission'] == mission, request.app.ctx.missionTable), None)
                if m is not None and 'endpoints' in m and m['endpoints'] is not None and url in m['endpoints']:
                    e = m['endpoints'][url]
                    status = checkEndpoint(request, e)
                    if status == PERM_INVALID:
                        return sanic.response.text("Page not found: {}".format(request.path), status=404)
                    elif status == PERM_REJECT:
                        return sanic.response.text("authorization failed")
                    else:
                        if 'modes' in e and e['modes'] is not None:
                            modes = e['modes']
                        if 'requirepilot' in e and e['requirepilot'] is not None:
                            requirePilot = e['requirepilot']
                    
                # modes now has final possible value - so check for pilot restricted API in public run mode
                if modes is not None and runningMode not in modes:
                    sanic.log.logger.info(f"rejecting {url}: mode not allowed")
                    return sanic.response.text("Page not found: {}".format(request.path), status=404)
                    
                # if we're running a private instance of a pilot server then we only require authentication
                # as a pilot if the pilots/pilotgroups spec is given (similar to how users always work)
                # so our default (no spec) is to grant pilot access
                if requirePilot and request.app.config.RUNMODE == MODE_PRIVATE:
                    defaultPerm = PERM_PILOT 
                
                # this will always fail and return not authorized if glider is None
                status = checkGliderMission(request, glider, mission, perm=defaultPerm)
                if status <= PERM_REJECT or (requirePilot and status < PERM_PILOT):
                    sanic.log.logger.info(f"{url} authorization failed {status}, {requirePilot}")
                    if status == PERM_INVALID:
                        return sanic.response.text("not found")
                    else: 
                        return sanic.response.text("authorization failed")

            elif modes is not None and runningMode not in modes:
                # do the public / pilot mode check for AUTH_ENDPOINT only mode
                sanic.log.logger.info(f"rejecting {url}: mode not allowed")
                return sanic.response.text("Page not found: {}".format(request.path), status=404)

            # the user is authorized.
            # run the handler method and return the response
            response = await f(request, *args, **kwargs)
            return response
        return decorated_function
    return decorator


def rowToDict(cursor: aiosqlite.Cursor, row: aiosqlite.Row) -> dict:
    data = {}
    for idx, col in enumerate(cursor.description):
        data[col[0]] = row[idx]

    return data

def missionFromRequest(request):
    if request and 'mission' in request.args and len(request.args['mission']) > 0 and request.args['mission'][0] != 'current':
        return request.args['mission'][0]

    return None

def matchMission(gld, request, mission=None):
    if mission == None and \
       request and \
       'mission' in request.args and \
       request.args['mission'][0] != 'current' and \
       len(request.args['mission'][0]) > 0:

        mission = request.args['mission'][0]

    return next(filter(lambda d: d['glider'] == int(gld) and (d['mission'] == mission or (mission == None and (d['default'] == True or d['path'] == None))), request.app.ctx.missionTable), None)

def filterMission(gld, request, mission=None):
    m = matchMission(gld, request, mission)    
    return { k: m[k] for k in m.keys() & publicMissionFields } if m else None

def gliderPath(glider, request, path=None):
    if path:
        return f'sg{glider:03d}/{path}'
    else:
        m = matchMission(glider, request)
        if m and 'abs' in m and m['abs']:
            return m['abs'] 
        elif m and 'path' in m and m['path']:
            return f"sg{glider:03d}/{m['path']}"
        else:
            return f'sg{glider:03d}'


#
# GET handlers - most of the API
#

def attachHandlers(app: sanic.Sanic):

    # consider whether any of these need to be protectable?? parms??
    app.static('/favicon.ico', f'{sys.path[0]}/html/favicon.ico', name='favicon.ico')
    app.static('/parms', f'{sys.path[0]}/html/Parameter_Reference_Manual.html', name='parms')
    app.static('/script', f'{sys.path[0]}/scripts', name='script')
    app.static('/help', f'{sys.path[0]}/html/help.html', name='help')
    app.static('/script/images', f'{sys.path[0]}/scripts/images', name='script_images')
    app.static('/manifest.json', f'{sys.path[0]}/scripts/manifest.json', name='manifest')

    @app.exception(sanic.exceptions.NotFound)
    def pageNotFound(request, exception):
        return sanic.response.text("Page not found: {}".format(request.path), status=404)

    @app.post('/auth')
    # description: user authorization 
    # payload: (JSON) username, password
    # returns: none on success (sets cookie with authorization token)
    async def authHandler(request):
        username = request.json.get("username", None).lower()
        password = request.json.get("password", None)

        for user,prop in request.app.ctx.userTable.items():
            if user.lower() == username and sha256_crypt.verify(password, prop['password']):
                token = jwt.encode({ "user": user, "groups": prop['groups']}, request.app.config.SECRET)
                response = sanic.response.text("authorization ok")
                response.cookies["token"] = token
                response.cookies["token"]["max-age"] = 86400
                response.cookies["token"]["samesite"] = "Strict"
                response.cookies["token"]["httponly"] = True
                return response

        return sanic.response.text('authorization failed') 

    @app.route('/user')
    # description: checks whether current session user is valid
    # returns: YES is user is valid
    @authorized(modes=['pilot','private'], check=AUTH_ENDPOINT)
    async def userHandler(request):
        (tU, tG) = getTokenUser(request)
        return sanic.response.text('YES' if tU else 'NO')
 
    @app.route('/plot/<fmt:str>/<which:str>/<glider:int>/<dive:int>/<image:str>')
    # description: get a plot image
    # args: fmt=png|webp|div, which=dv|eng|sg
    # returns: image data (webp, png or plotly div)
    # parameters: mission
    @authorized()
    async def plotHandler(request, fmt:str, which: str, glider: int, dive: int, image: str):
        if fmt not in ['png', 'webp', 'div']:
            return sanic.response.text('not found', status=404)

        if which == 'dv':
            filename = f'{gliderPath(glider,request)}/plots/dv{dive:04d}_{image}.{fmt}'
        elif which == 'eng':
            filename = f'{gliderPath(glider,request)}/plots/eng_{image}.{fmt}'
        elif which == 'section':
            filename = f'{gliderPath(glider,request)}/plots/sg_{image}.{fmt}'
        else:
            return sanic.response.text('not found', status=404)

        if await aiofiles.os.path.exists(filename):
            if 'wrap' in request.args and request.args['wrap'][0] == 'page':
                mission = missionFromRequest(request)
                mission = f"?mission={mission}" if mission else ''
                wrap = '?wrap=page' if mission == '' else '&wrap=page'

                filename = f'{sys.path[0]}/html/wrap.html'
                return await sanic.response.file(filename, mime_type='text/html')
            else:
                if fmt == 'div':
                    async with aiofiles.open(filename, 'rb') as f:
                        cont = await f.read()
                    
                    return sanic.response.raw(cont, headers={'Content-type': 'text/html', 'Content-Encoding': 'br'})
                else:
                    return await sanic.response.file(filename, mime_type=f"image/{fmt}")
        else:
            return sanic.response.text('not found', status=404)
           
    # we don't protect this so they get a blank page with a login option even
    # if not authorized
    @app.route('/<glider:int>')
    # description: main page for glider
    # parameters: mission, plot (starting plot), dive (starting dive), order (plot ribbon order), plot tool values: x,y,z,first,last,step,top,bottom,bin,wholemission|divetimeseries,dives|climbs|both|combine, op (contour,profiles,plot,table,csv)
    # returns: HTML page
    @app.ext.template("vis.html")
    async def mainHandler(request, glider:int):
        runMode = request.app.config.RUNMODE
        if runMode == MODE_PRIVATE:
            runMode = MODE_PILOT

        return {"runMode": modeNames[runMode], "noSave": request.app.config.NO_SAVE, "noChat": request.app.config.NO_CHAT}

    @app.route('/dash')
    # description: dashboard (engineering diagnostic) view of index (all missions) page
    # parameters: plot (which plot to include in tiles, default=diveplot)
    # returns: HTML page
    @authorized(check=AUTH_ENDPOINT)
    @app.ext.template("index.html")
    async def dashHandler(request):
        return {"runMode": "pilot"}

    @app.route('/')
    # description: "public" index (all missions) page
    # parameters: plot (which plot to include in tiles, default=map)
    # returns: HTML page
    @app.ext.template("index.html")
    async def indexHandler(request):
        return {"runMode": "public"}

    @app.route('/map/<glider:int>')
    # description: map tool
    # parameters: mission, tail (number of dives back to show in glider track), also (additional gliders to plot), sa (URL for SA to load)
    # returns: HTML page
    @authorized()
    @app.ext.template("map.html")
    async def mapHandler(request, glider:int):
        # filename = f'{sys.path[0]}/html/map.html'
        # return await sanic.response.file(filename, mime_type='text/html')
        return { "weathermapAppID": request.app.config.WEATHERMAP_APPID }

    @app.route('/mapdata/<glider:int>')
    # description: get map configation (also, sa, kml from missions.yml)
    # parameters: mission
    # returns: JSON dict with configuration variables
    @authorized()
    async def mapdataHandler(request, glider:int):
        mission = matchMission(glider, request) 
        if mission:
            message = {}
            for k in ['sa', 'kml', 'also']:
                if k in mission:
                    message[k] = mission[k]

            return sanic.response.json(message)

        else:
            return sanic.response.text('not found')

    @app.route('/gliderkml/<glider:int>')
    # description: get glider KML
    # parameters: mission
    # returns: KML
    @authorized()
    async def gliderkmlHandler(request, glider:int):
        filename = f'{gliderPath(glider,request)}/sg{glider}.kmz'
        async with aiofiles.open(filename, 'rb') as file:
            zip = ZipFile(BytesIO(await file.read()))
            kml = zip.open(f'sg{glider}.kml', 'r').read()
            return sanic.response.raw(kml)

    # Not currently linked on a public facing page, but available.
    # Protect at the mission level (which protects that mission at 
    # all endpoints) or at the endpoint level with something like
    # users: [download] or groups: [download] and build
    # users.yml appropriately
    #
    # curl -c cookies.txt -X POST http://myhost/auth \
    # -H "Content-type: application/json" \
    # -d '{"username": "joeuser", "password": "abc123"}'
    #
    # curl -b cookies.txt http://myhost/data/nc/237/12 \
    # --output p2370012.nc
    #
    @app.route('/data/<which:str>/<glider:int>/<dive:int>')
    # description: download raw data
    # parameters: mission
    # returns: netCDF file
    @authorized()
    async def dataHandler(request, which:str, glider:int, dive:int):
        path = gliderPath(glider,request)

        if which == 'nc' or which == 'ncfb':
            filename = f'p{glider:03d}{dive:04d}.{which}'
        else:
            sanic.log.logger.info(f'invalid filetype requested {which}')
            return sanic.response.text('not found', status=404)

        fullname = f"{path}/{filename}"           
        if await aiofiles.os.path.exists(fullname):
            return await sanic.response.file(fullname, filename=filename, mime_type='application/x-netcdf4')
        else:
            sanic.log.logger.info(f'{fullname} not found')
            return sanic.response.text('not found', status=404)

    # This is not a great idea to leave this open as a public proxy server,
    # but we need it for all layers to work with public maps at the moment.
    # Need to evaluate what we lose if we turn proxy off or find another solution.
    # Or limit the dictionary of what urls can be proxied ...
    # NOAA forecast, NIC ice edges, iop SA list, opentopo GEBCO bathy
    @app.route('/proxy/<url:path>')
    # description: proxy requests for map tool
    # returns: contents from requested URL
    @authorized(check=AUTH_ENDPOINT)
    async def proxyHandler(request, url):
        allowed = ['https://api.opentopodata.org/v1/gebco2020',
                   'https://marine.weather.gov/MapClick.php',
                   'https://iop.apl.washington.edu/', 
                   'https://usicecenter.gov/File/DownloadCurrent',
                   'https://raw.githubusercontent.com/rwev/leaflet-reticle/master/src',
                  ]

        found = False
        if url.startswith('http:/') and not url.startswith('http://'):
            url = re.sub('http:/', 'http://', url)
        elif url.startswith('https:/') and not url.startswith('https://'):
            url = re.sub('https:/', 'https://', url)
 
        for x in allowed:
            if url.startswith(x):
                found = True
                break

        if found == False:
            return sanic.response.text(f"Page not found: {request.path}", status=404)
              
        if request.args and len(request.args) > 0:
            url = url + '?' + request.query_string
        async with aiohttp.ClientSession() as session:
            async with session.get(url) as response:
                if response.status == 200:
                    body = await response.read()
                    return sanic.response.raw(body)
        
    @app.route('/proxykmz/<url:path>')
    # description: proxy and unzip network KMZ sources
    # returns: KML from KMZ downloaded from requested URL
    @authorized(check=AUTH_ENDPOINT)
    async def proxykmzHandler(request, url):
        allowed = [
                   'https://usicecenter.gov/File/DownloadCurrent',
                   'https://iop.apl.washington.edu/seaglider_ssh',
                  ]

        found = False
        if url.startswith('http:/') and not url.startswith('http://'):
            url = re.sub('http:/', 'http://', url)
        elif url.startswith('https:/') and not url.startswith('https://'):
            url = re.sub('https:/', 'https://', url)
 
        for x in allowed:
            if url.startswith(x):
                found = True
                break

        if found == False:
            return sanic.response.text(f"Page not found: {request.path}", status=404)

        kmz = None
              
        if request.args and len(request.args) > 0:
            url = url + '?' + request.query_string
        async with aiohttp.ClientSession() as session:
            async with session.get(url) as response:
                if response.status == 200:
                    kmz = await response.read()

        if kmz == None:
            return sanic.response.text(f"Page not found: {request.path}", status=404)

        nm = None
        try:
            zip = ZipFile(BytesIO(kmz))
            print(zip.namelist())
            for x in zip.namelist():
                if '.kml' in x:
                    nm = x
                    break
        except Exception as e:
            print(f'exception {e}')
            pass

        if nm == None:
            return sanic.response.text('not found')
 
        kml = zip.open(nm).read()
        return sanic.response.raw(kml)

    @app.route('/plots/<glider:int>/<dive:int>')
    # description: list of plots available for dive
    # parameters: mission
    # returns: JSON dict of available plots, sorted by type
    @authorized()
    async def plotsHandler(request, glider:int, dive:int):
        (dvplots, plotlyplots) = await buildDivePlotList(gliderPath(glider,request), dive)
        message = {}
        message['glider']      = f'SG{glider:03d}'
        message['dive']        = dive
        message['dvplots']     = dvplots
        message['plotlyplots'] = plotlyplots
        # message['engplots']    = engplots
        
        return sanic.response.json(message)

    @app.route('/log/<glider:int>/<dive:int>')
    # description: formatted glider log 
    # parameters: mission
    # returns: summary version of log file in HTML format
    @authorized()
    async def logHandler(request, glider:int, dive:int):
        filename = f'{gliderPath(glider,request)}/p{glider:03d}{dive:04d}.log'
        s = await LogHTML.captureTables(filename)
        return sanic.response.html(s)

    def purgeSensitive(text):
        out = ''
        prior = False
        for line in text.splitlines():
            if 'password = ' in line: 
                line = re.sub(r"password = .*?[^<]*", "password = ----", line)
                sanic.log.logger.info("purging known password")
            elif 'Current password is' in line:
                line = re.sub(r"Current password is .*?[^<]*", "Current password is ----", line)
                sanic.log.logger.info("purging known password")
            elif 'Changing password to' in line:
                line = re.sub(r"Changing password to .*?[^<]*", "Changing password to ----", line)
                sanic.log.logger.info("purging known password change")
            elif 'sent [' in line and prior:
                line = re.sub(r"sent \[.*\]", "sent [----]", line)
                prior = False
                sanic.log.logger.info("purging sent password debugging")
            elif 'sending password [' in line:
                line = re.sub(r"sending password \[.*\]", "sending password [----]", line)
                sanic.log.logger.info("purging known password debugging")
                prior = True
            elif 'assword' in line \
                 and not 'Password:]' in line \
                 and not 'no password: prompt' in line \
                 and not 'no shell prompt' in line:
                line = '---- deleted ----'
                sanic.log.logger.info("purging unknown password")
            else:
                prior = False

            out = out + line + '\n'

        return out


    @app.route('/file/<ext:str>/<glider:int>/<dive:int>')
    # description: processed glider basestation files
    # args: ext=eng|log|cap
    # parameters: mission
    # returns: raw eng, log, or cap file
    @authorized()
    async def logengcapFileHandler(request, ext:str, glider: int, dive: int):
        if ext not in ['log', 'eng', 'cap']:
            return sanic.response.text('not found', status=404)
            
        filename = f'{gliderPath(glider,request)}/p{glider:03d}{dive:04d}.{ext}'
        if await aiofiles.os.path.exists(filename):
            if ext in ['log', 'eng']:
                return await sanic.response.file(filename, mime_type='text/plain')
            else:
                async with aiofiles.open(filename, 'r') as file:
                    out = purgeSensitive(await file.read())
                    return sanic.response.text(out)
        else:
            if ext == 'cap':
                return sanic.response.text('none')
            else:
                return sanic.response.text('not found', status=404)
           
    @app.route('/alerts/<glider:int>/<dive:int>')
    # description: basestation alerts file
    # parameters: mission
    # returns: raw alerts file from basestation (HTML format)
    @authorized()
    async def alertsHandler(request, glider: int, dive: int):
        filename = f'{gliderPath(glider,request)}/alert_message.html.{dive:d}'
        if await aiofiles.os.path.exists(filename):
            return await sanic.response.file(filename, mime_type='text/plain')
        else:
            return sanic.response.text('not found')
     
    @app.route('/deltas/<glider:int>/<dive:int>')
    # description: list of changes between dives
    # parameters: mission
    # returns: JSON formatted dict of control file changes
    @authorized()
    async def deltasHandler(request, glider: int, dive: int):
        dbfile = f'{gliderPath(glider,request)}/sg{glider:03d}.db'
        message = { 'dive': dive, 'parm': [], 'file': [] }
        if await Path(dbfile).exists():
            async with aiosqlite.connect('file:' + dbfile + '?mode=ro', uri=True) as conn:
                conn.row_factory = rowToDict # not async but called from async fetchall
                cur = await conn.cursor()
                try:
                    await cur.execute(f"SELECT * FROM changes WHERE dive={dive} ORDER BY parm ASC;")
                except aiosqlite.OperationalError as e:
                    return sanic.response.text(f'db error {e}')

                message['parm'] = await cur.fetchall()

                try:
                    await cur.execute(f"SELECT * FROM files WHERE dive={dive} ORDER BY file ASC;")
                except aiosqlite.OperationalError as e:
                    return sanic.response.text(f'db error {e}')

                message['file'] = await cur.fetchall()

        return sanic.response.json(message)

    @app.route('/missions/<mask:str>')
    # description: list of missions
    # args: mask is unused
    # returns: JSON formatted dict of missions and mission config
    async def missionsHandler(request, mask:int):
        table = await buildAuthTable(request, mask)
        msg = { "missions": table, "organization": request.app.ctx.organization }
        return sanic.response.json(msg)
     
    @app.route('/summary/<glider:int>')
    # description: summary status of glider
    # parameters: mission
    # returns: JSON formatted dict of glider engineering status variables
    @authorized()
    async def summaryHandler(request, glider:int):
        msg = await summary.collectSummary(glider, gliderPath(glider,request))
        msg['mission'] = filterMission(glider, request)
        return sanic.response.json(msg)

    # this does setup and is generally only called once at page load
    @app.route('/status/<glider:int>')
    # description: glider latest dive number and visualization config (mission.dat variables, mission plots)
    # parameters: mission
    # returns: JSON formatted dict of configuration
    @authorized()
    async def statusHandler(request, glider:int):
        dbfile = f'{gliderPath(glider,request)}/sg{glider:03d}.db'
        if await Path(dbfile).exists():
            async with aiosqlite.connect('file:' + dbfile + '?mode=ro', uri=True) as conn:
                cur = await conn.cursor()
                try:
                    await cur.execute("SELECT dive FROM dives ORDER BY dive DESC LIMIT 1")
                except aiosqlite.OperationalError as e:
                    return sanic.response.text(f'no table {e}')

                try:
                    maxdv = (await cur.fetchone())[0]
                except:
                    maxdv = 0
        else:
            return sanic.response.text('file not found')

        (engplots, sgplots, engplotly, sgplotly) = await buildMissionPlotList(gliderPath(glider, request))

        message = {}
        message['glider'] = f'SG{glider:03d}'
        message['dive'] = maxdv
        message['engplots'] = engplots
        message['sgplots'] = sgplots
        message['engplotly'] = engplotly
        message['sgplotly'] = sgplotly
        message['organization'] = request.app.ctx.organization
        
        message['mission'] = filterMission(glider, request) 
        return sanic.response.json(message)

    @app.route('/control/<glider:int>/<which:str>')
    # description: glider control files
    # args: which=cmdfile|targets|science|scicon.sch|tcm2mat.cal|pdoscmds.bat|sg_calib_constants.m
    # parameters: mission
    # returns: latest version of glider control file
    @authorized()
    async def controlHandler(request, glider:int, which:str):
        ok = ["cmdfile", "targets", "science", "scicon.sch", "tcm2mat.cal", "pdoscmds.bat", "sg_calib_constants.m"]

        if which not in ok:
            return sanic.response.text("oops")

        message = {}

        message['file'] = 'none'
        filename = f'{gliderPath(glider,request)}/{which}'

        if await aiofiles.os.path.exists(filename):
            message['file'] = which
            message['dive'] = -1
        else:
            p = Path(gliderPath(glider,request))
            latest = -1
            call = -1;
            async for fpath in p.glob(f'{which}.*'):
                try:
                    j = parse('%s.{:d}.{:d}' % which, fpath.name)
                    if j and hasattr(j, 'fixed') and len(j.fixed) == 2 and j.fixed[0] > latest and j.fixed[1] > call:
                        latest = j.fixed[0]
                        call = j.fixed[1]
                    else:
                        j = parse('%s.{:d}' % which, fpath.name)
                        if j and hasattr(j, 'fixed') and len(j.fixed) == 1 and j.fixed[0] > latest:
                            latest = j.fixed[0]
                            call = -1
                except Exception as e:
                    sanic.log.logger.info(f"controlHandler: {e}")
                    continue

            if latest > -1:
                message['file'] = which
                message['dive'] = latest
                if call > -1:
                    filename = f'{filename}.{latest}.{call}'
                    message['call'] = call
                else:
                    filename = f'{filename}.{latest}'
                    message['call'] = -1

        if message['file'] == "none":
            return sanic.response.text("none")

        async with aiofiles.open(filename, 'r') as file:
            message['contents']= await file.read() 

        return sanic.response.json(message)

    @app.route('/db/<glider:int>/<dive:int>')
    # description: query database for common engineering variables
    # args: dive=-1 returns whole mission
    # parameters: mission
    # returns: JSON dict of engineering variables
    @authorized()
    async def dbHandler(request, glider:int, dive:int):
        dbfile = f'{gliderPath(glider,request)}/sg{glider:03d}.db'
        if not await aiofiles.os.path.exists(dbfile):
            return sanic.response.text('no db')

        q = "SELECT dive,log_start,log_D_TGT,log_D_GRID,log__CALLS,log__SM_DEPTHo,log__SM_ANGLEo,log_HUMID,log_TEMP,log_INTERNAL_PRESSURE,depth_avg_curr_east,depth_avg_curr_north,max_depth,pitch_dive,pitch_climb,batt_volts_10V,batt_volts_24V,batt_capacity_24V,batt_capacity_10V,total_flight_time_s,avg_latitude,avg_longitude,target_name,magnetic_variation,mag_heading_to_target,meters_to_target,GPS_north_displacement_m,GPS_east_displacement_m,flight_avg_speed_east,flight_avg_speed_north,dog_efficiency,alerts,criticals,capture,error_count,(SELECT COUNT(dive) FROM changes where changes.dive=dives.dive) as changes, (SELECT COUNT(dive) FROM files where files.dive=dives.dive) as files FROM dives"

        if dive > -1:
            q = q + f" WHERE dive={dive};"
        else:
            q = q + " ORDER BY dive ASC;"

        async with aiosqlite.connect('file:' + dbfile + '?mode=ro', uri=True) as conn:
            conn.row_factory = rowToDict # not async but called from async fetchall
            cur = await conn.cursor()
            try:
                await cur.execute(q)
            except aiosqlite.OperationalError as e:
                return sanic.response.text(f'no table {e}')

            data = await cur.fetchall()
            # r = [dict((cur.description[i][0], value) \
            #       for i, value in enumerate(row)) for row in data]
            return sanic.response.json(data)

    @app.route('/changes/<glider:int>/<dive:int>/<which:str>/<sort:str>')
    # description: query database for parameter or control file changes
    # args: dive=-1 returns entire history, which=parms|files, sort=dive|parm|file
    # parameters: mission
    # returns: JSON dict of changes [{(dive,parm,oldval,newval}] or [{dive,file,fullname,contents}]
    async def changesHandler(request, glider:int, dive:int, which:str, sort:str):
        if (which not in ['parms', 'files']) or (sort not in ['dive', 'file', 'parm']):
            return sanic.response.text('no db')
           
        db   = { 'parms': 'changes', 'files': 'files' } 
        col2 = which[0:-1]

        dbfile = f'{gliderPath(glider,request)}/sg{glider:03d}.db'
        if not await aiofiles.os.path.exists(dbfile):
            return sanic.response.text('no db')

        q = f"SELECT * FROM {db[which]}"

        if dive > -1:
            q = q + f" WHERE dive={dive} ORDER BY {col2} ASC;"
        elif sort  == 'dive':
            q = q + f" ORDER BY dive,{col2} ASC;"
        else:
            q = q + f" ORDER BY {col2},dive ASC;"

        async with aiosqlite.connect('file:' + dbfile + '?mode=ro', uri=True) as conn:
            conn.row_factory = rowToDict # not async but called from async fetchall
            cur = await conn.cursor()
            try:
                await cur.execute(q)
            except aiosqlite.OperationalError as e:
                return sanic.response.text(f'no table {e}')

            data = await cur.fetchall()
            return sanic.response.json(data)


    @app.route('/dbvars/<glider:int>')
    # description: list of per dive database variables
    # parameters: mission
    # returns: JSON list of variable names
    @authorized()
    async def dbvarsHandler(request, glider:int):
        dbfile = f'{gliderPath(glider,request)}/sg{glider:03d}.db'
        if not await aiofiles.os.path.exists(dbfile):
            return sanic.response.text('no db')

        async with aiosqlite.connect('file:' + dbfile + '?mode=ro', uri=True) as conn:
            cur = await conn.cursor()
            try:
                await cur.execute('select * from dives')
            except aiosqlite.OperationalError as e:
                return sanic.response.text(f'no table {e}')
            names = list(map(lambda x: x[0], cur.description))
            data = {}
            data['names'] = names
            return sanic.response.json(data)

    @app.route('/pro/<glider:int>/<whichVar:str>/<whichProfiles:int>/<first:int>/<last:int>/<stride:int>/<top:int>/<bot:int>/<binSize:int>')
    # description: extract bin averaged profiles
    # args: whichProfiles=1(dives)|2(climbs)|3(both)|4(combine)
    # parameters: mission
    # returns: compressed JSON dict of binned profiles
    @authorized()
    @compress.compress()
    async def proHandler(request, glider:int, whichVar:str, whichProfiles:int, first:int, last:int, stride:int, top:int, bot:int, binSize:int):
        ncfilename = Utils.get_mission_timeseries_name(None, gliderPath(glider,request))
        if not await aiofiles.os.path.exists(ncfilename):
            return sanic.response.text('no db')

        data = ExtractTimeseries.timeSeriesToProfile(whichVar, whichProfiles, first, last, stride, top, bot, binSize, ncfilename)
        out = ExtractTimeseries.dumps(data[0]) # need custom serializer for the numpy array
        return sanic.response.raw(out, headers={ 'Content-type': 'application/json' })


    @app.route('/timevars/<glider:int>')
    # description: list of timeseries variables
    # parameters: mission
    # returns: JSON list of variable names
    @authorized()
    async def timeSeriesVarsHandler(request, glider:int):
        ncfilename = Utils.get_mission_timeseries_name(None, gliderPath(glider,request))
        if not await aiofiles.os.path.exists(ncfilename):
            return sanic.response.text('no db')

        names = ExtractTimeseries.getVarNames(ncfilename)
        names = sorted([ f['var'] for f in names ])
        return sanic.response.json(names)
        
    @app.route('/time/<glider:int>/<dive:int>/<which:str>')
    # description: extract timeseries data from netCDF
    # parameters: mission
    # returns: compressed JSON dict of timeseries data
    @authorized()
    @compress.compress()
    async def timeSeriesHandler(request, glider:int, dive:int, which:str):
        ncfilename = Utils.get_mission_timeseries_name(None, gliderPath(glider,request))
        if not await aiofiles.os.path.exists(ncfilename):
            return sanic.response.text('no db')

        whichVars = which.split(',')
        dbVars = whichVars
        if 'time' in dbVars:
            dbVars.remove('time')

        data = ExtractTimeseries.extractVars(ncfilename, dbVars, dive, dive)
        return sanic.response.json(data)

    @app.route('/query/<glider:int>/<queryVars:str>')
    # description: query per dive database for arbitrary variables
    # args: queryVars=comma separated list
    # parameters: mission
    # returns: JSON dict of query results
    @authorized()
    async def queryHandler(request, glider, queryVars):
        dbfile = f'{gliderPath(glider,request)}/sg{glider:03d}.db'
        if not await aiofiles.os.path.exists(dbfile):
            return sanic.response.text('no db')

        queryVars = queryVars.rstrip(',')
        pieces = queryVars.split(',')
        if pieces[0] == 'dive':
            q = f"SELECT {queryVars} FROM dives ORDER BY dive ASC"
        else:
            q = f"SELECT {queryVars} FROM dives"

        async with aiosqlite.connect('file:' + dbfile + '?mode=ro', uri=True) as conn:
            # conn.row_factory = rowToDict
            cur = await conn.cursor()
            await cur.execute(q)
            d = await cur.fetchall()
            data = {}
            print(cur.description)
            for i in range(len(cur.description)):
                data[cur.description[i][0]] = [ f[i] for f in d ]

            return sanic.response.json(data)

    @app.route('/selftest/<glider:int>')
    # description: selftest review
    # parameters: mission
    # returns: HTML format summary of latest selftest results
    async def selftestHandler(request, glider:int):
        cmd = f"{sys.path[0]}/SelftestHTML.py"
        proc = await asyncio.create_subprocess_exec(
            cmd, f"{glider:03d}", gliderPath(glider, request), 
            stdout=asyncio.subprocess.PIPE, 
            stderr=asyncio.subprocess.PIPE
        )
        results, err = await proc.communicate()
        return sanic.response.html(purgeSensitive(results.decode('utf-8', errors='ignore')))

    #
    # POST handler - to save files back to basestation
    #

    # first the safety function
    def applyControls(c, text, filename):
        forbidden = ['shutdown', 'scuttle', 'wipe', 'reboot', 'pdos']
        for nono in forbidden:
            if nono in text.lower():
                sanic.log.logger.info(f"{nono} is a nono")
                return True

        d1 = c['global']['deny'] if 'global' in c and 'deny' in c['global'] else []
        d2 = c[filename]['deny'] if filename in c and 'deny' in c[filename] else []
        a1 = c['global']['allow'] if 'global' in c and 'allow' in c['global'] else []
        a2 = c[filename]['allow'] if filename in c and 'allow' in c[filename] else []

        status = False
        for line in text.splitlines():
            for d in d1:
                if d.search(line):
                    status = True
                    sanic.log.logger.info(f"global deny {line} ({d})")
                    for a in a1:
                        if a.search(line):
                            sanic.log.logger.info(f"global allow {line} ({d})")
                            status = False
                            break;
                    if status:
                        for a in a2:
                            if a.search(line):
                                sanic.log.logger.info(f"{filename} allow {line} ({d})")
                                status = False
                                break;

                    if status:
                        return status

            for d in d2:
                if d.search(line):
                    status = True
                    sanic.log.logger.info(f"{filename} deny {line} ({d})")
                    for a in a2:
                        if a.search(line):
                            sanic.log.logger.info(f"{filename} allow {line} ({d})")
                            status = False
                            break

                    if status:
                        return status
    
        return False

    @app.post('/save/<glider:int>/<which:str>')
    # description: save glider control file
    # args: which=cmdfile|targets|science|scicon.sch|tcm2mat.cal|pdoscmds.bat|sg_calib_constants.m
    # payload: (JSON) file, contents
    # parameters: mission
    # returns: validator results and/or error message  
    @authorized(modes=['private', 'pilot'], requirePilot=True)
    async def saveHandler(request, glider:int, which:str):
        # the no save command line flag allows one more layer
        # of protection
        if request.app.config.NO_SAVE:
            return sanic.response.text('not allowed')

        validator = {"cmdfile": "cmdedit", "science": "sciedit", "targets": "targedit"}

        message = request.json
        if 'file' not in message or message['file'] != which:
            return sanic.response.text('oops')

        if applyControls(request.app.ctx.controls, message['contents'], which) == True:
            return sanic.response.text('not allowed')
         
        path = gliderPath(glider, request)
        if which in validator:
            try:
                async with aiofiles.tempfile.NamedTemporaryFile('w', delete=False) as file:
                    await file.write(message['contents'])
                    await file.close()
                    sanic.log.logger.debug("saved to %s" % file.name)

                    (tU, _) = getTokenUser(request)

                    if 'force' in message and message['force'] == 1:
                        cmd = f"{sys.path[0]}/{validator[which]} -d {path} -q -i -f {file.name} -u {tU}"
                    else:
                        cmd = f"{sys.path[0]}/{validator[which]} -d {path} -q -f {file.name} -u {tU}"
            
                    proc = await asyncio.create_subprocess_shell(
                        cmd, 
                        stdout=asyncio.subprocess.PIPE, 
                        stderr=asyncio.subprocess.PIPE
                    )
                    out, err = await proc.communicate()
                    results = out.decode('utf-8', errors='ignore') 
                    await aiofiles.os.remove(file.name)
            except Exception as e:
                results = f"error saving {which}, {str(e)}"

            return sanic.response.text(results)

        else: # no validator for this file type
            try:
                async with aiofiles.open(f'{path}/{which}', 'w') as file:
                    await file.write(message['contents'])
                return sanic.response.text(f"{which} saved ok")
            except Exception as e:
                return sanic.response.text(f"error saving {which}, {str(e)}")
                
    # The get handler for notifications from the basestation.
    # Run a route to receive notifications remotely. Typically only used 
    # when running vis on a server different from the basestation proper
    @app.post('/url')
    # description: .urls notification handler (used when vis server is different from basestation server)
    # parameters: instrument_name, dive, files (), status (), gpsstr ()
    # returns: 'ok' or 'error'
    async def urlHandler(request):
        if 'instrument_name' not in request.args:
            return sanic.response.text('error')

        glider = int(request.args['instrument_name'][0][2:])
        dive   = int(request.args['dive'][0]) if 'dive' in request.args else None
        files  = request.args['files'][0] if 'files' in request.args else None
        status = request.args['status'][0] if 'status' in request.args else None
        gpsstr = request.args['gpsstr'][0] if 'gpsstr' in request.args else None

        if status:
            content = f"status={status}"
            topic = 'status'
            msg = { "glider": glider, "dive": dive, "content": content, "time": time.time() }
        elif gpsstr:
            topic = 'gpsstr'
            try: 
                msg = request.json
            except Exception as e:
                sanic.log.logger.info(f"gpsstr body: {e}")
                msg = {}
        elif files:
            content = f"files={files}" 
            topic = 'files'
            msg = { "glider": glider, "dive": dive, "content": content, "time": time.time() }

        # consider whether this should go to all instances (Utils.notifyVisAsync)
        try:
            socket = zmq.asyncio.Context().socket(zmq.PUSH)
            socket.connect(request.app.config.NOTIFY_IPC)
            socket.setsockopt(zmq.SNDTIMEO, 200)
            socket.setsockopt(zmq.LINGER, 0)
            # these two log lines are critical - the socket send
            # does not go out without them ....  ¯\_(ツ)_/¯
            sanic.log.logger.info(f"sending {glider:03d}-urls-{topic}")
            sanic.log.logger.info(f"msg={msg}")
            await socket.send_multipart([(f"{glider:03d}-urls-{topic}").encode('utf-8'), dumps(msg)]) 
            socket.close()
        except:
            return sanic.response.text('error')
     
        return sanic.response.text('ok')

    async def getChatMessages(request, glider, t, conn=None):
        if conn == None:
            dbfile = f'{gliderPath(glider,request)}/sg{glider:03d}.db'
            if not await aiofiles.os.path.exists(dbfile):
                return (None, time.time())

            myconn = await aiosqlite.connect('file:' + dbfile + '?mode=ro', uri=True)
            myconn.row_factory = rowToDict
        else:
            myconn = conn
    
        try:
            cur = await myconn.cursor()
            q = f"SELECT * FROM chat WHERE timestamp > {t} ORDER BY timestamp;" #  DESC LIMIT 20;"
            await cur.execute(q)
            rows = await cur.fetchall()
            await cur.close()
        except Exception as e:
            sanic.log.logger.info(e)
            rows = None

        if conn == None:
            await myconn.close()

        if rows:
            for r in rows:
                if 'attachment' in r and r['attachment'] is not None:
                    b = r['attachment']
                    r['attachment'] = base64.b64encode(b).decode('utf-8')

            return (rows, rows[-1]['timestamp'])

        return (None, time.time())

    @app.route('/chat/history/<glider:int>')
    # description: chat history from database
    # parameters: mission
    # returns: JSON dict of chat history
    @authorized(modes=['private', 'pilot'])
    async def chatHistoryHandler(request, glider:int):
        if request.app.config.NO_CHAT:
            return sanic.response.text('not allowed')

        (tU, _) = getTokenUser(request)
        if tU == False:
            return sanic.response.text('authorization failed')

        (rows, _) = await getChatMessages(request, glider, 0)
        return sanic.response.json(rows)

    @app.post('/chat/send/<glider:int>')
    # description: POST a message to chat
    # parameters: mission
    # payload: (form) attachment, message
    # returns: JSON dict of chat history
    @authorized(modes=['private', 'pilot'])
    async def chatHandler(request, glider:int):
        if request.app.config.NO_CHAT:
            return sanic.response.text('not allowed')

        # we could have gotten here by virtue of no restrictions specified for this glider/mission,
        # but chat only worked if someone is logged in, so we check that we have a user
        (tU, _) = getTokenUser(request)
        if tU == False:
            return sanic.response.text('authorization failed')

        attach = None
        if 'attachment' in request.files:
            attach = request.files['attachment'][0]
 
        msg = None
        if 'message' in request.form:
            msg = request.form['message'][0]

        if not msg and not attach:
            return sanic.response.text('oops')

        dbfile = f'{gliderPath(glider,request)}/sg{glider:03d}.db'
        if not await aiofiles.os.path.exists(dbfile):
            return sanic.response.text('no db')

        now = time.time()
 
        async with aiosqlite.connect(dbfile) as conn:
            cur = await conn.cursor()
            try:
                if attach:
                    q = f"INSERT INTO chat(timestamp, user, message, attachment, mime) VALUES(?, ?, ?, ?, ?)"
                    values = (now, tU, msg, attach.body, attach.type)
                else:
                    q = f"INSERT INTO chat(timestamp, user, message) VALUES(?, ?, ?)"
                    values = ( now, tU, msg )
                await cur.execute(q, values)
                await conn.commit()

                await Utils.notifyVisAsync(glider, 'chat', f"{now}:{'attachment' if attach else 'none'}:{msg}")
                return sanic.response.text('SENT')
            except aiosqlite.OperationalError as e:
                sanic.log.logger.info(e)
                return sanic.response.text('oops')

            await cur.close()
            # await conn.close()

    @app.route('/pos/<glider:int>')
    # description: position display
    # parameters: mission
    # returns: HTML page with position display
    @authorized()
    async def posHandler(request, glider:int):
        filename = f'{sys.path[0]}/html/pos.html'
        return await sanic.response.file(filename, mime_type='text/html')

    @app.route('/pos/poll/<glider:int>')
    # description: get latest glider position
    # parameters: mission
    # returns: JSON dict of glider position
    @authorized()
    async def posPollHandler(request: sanic.Request, glider:int):
        if 't' in request.args and len(request.args['t'][0]) > 0:
            t = int(request.args['t'][0])
            q = f"SELECT * FROM calls WHERE epoch > {t} ORDER BY epoch DESC LIMIT 1;"
        else:
            q = f"SELECT * FROM calls ORDER BY epoch DESC LIMIT 1;"

        # xurvey uses this but nothing else - easy enough to add
        # nmea = 'format' in request.args and request.args['format'][0] == 'nmea'

        dbfile = f'{gliderPath(glider,request)}/sg{glider:03d}.db'
        if not await aiofiles.os.path.exists(dbfile):
            return sanic.response.text('no db')

        try:
            conn = await aiosqlite.connect('file:' + dbfile + '?mode=ro', uri=True)
            conn.row_factory = rowToDict
            cur = await conn.cursor()
            await cur.execute(q)
            row = await cur.fetchone()
            await cur.close()
            if row:
                return sanic.response.json(row)
            else:
                return sanic.response.text('none')
        except Exception as e:
            sanic.log.logger.info(e)
            return sanic.response.text('oops')
           
    async def getLatestCall(request, glider, conn=None, limit=1):
        if conn == None:
            dbfile = f'{gliderPath(glider,request)}/sg{glider:03d}.db'
            if not await aiofiles.os.path.exists(dbfile):
                return None

            myconn = await aiosqlite.connect('file:' + dbfile + '?mode=ro', uri=True)
            myconn.row_factory = rowToDict
        else:
            myconn = conn

        row = None
        try:
            cur = await myconn.cursor()
            q = f"SELECT * FROM calls ORDER BY epoch DESC LIMIT {limit};"
            await cur.execute(q)
            row = await cur.fetchall()
            await cur.close()
        except Exception as e:
            sanic.log.logger.info(e)

        if conn == None:
            await myconn.close()

        return row
 
    #
    # web socket (real-time streams), 
    #

    @app.websocket('/pos/stream/<glider:int>')
    # description: stream real-time position updates
    # parameters: mission
    @authorized()
    async def posStreamHandler(request: sanic.Request, ws: sanic.Websocket, glider:int):
        socket = zmq.asyncio.Context().socket(zmq.SUB)
        socket.setsockopt(zmq.LINGER, 0)
        socket.connect(request.app.config.WATCH_IPC)
        socket.setsockopt(zmq.SUBSCRIBE, (f"{glider:03d}-urls-gpsstr").encode('utf-8'))

        # we get the first fix out of the db so the user gets the latest 
        # position if we're between calls

        row = await getLatestCall(request, glider)
        if row:
            await ws.send(dumps(row[0]).decode('utf-8'))

        # after that we rely on the notification payload because if we're
        # running as a remote instance the database won't be synced until
        # much later
        while True:
            try:
                msg = await socket.recv_multipart()
                # sanic.log.logger.info(f"got msg={msg[1]}")
                await ws.send(msg[1].decode('utf-8'))
                # sanic.log.logger.info("ws sent")
            except BaseException as e: # websockets.exceptions.ConnectionClosed:
                sanic.log.logger.info(f'ws connection closed {e}')
                await ws.close()
                socket.close()
                return
 
    @app.websocket('/stream/<which:str>/<glider:int>')
    # description: stream real-time glider information (comm.log, chat, cmdfile changed, glider calling, etc.)
    # parameters: mission
    @authorized()
    async def streamHandler(request: sanic.Request, ws: sanic.Websocket, which:str, glider:int):
        filename = f'{gliderPath(glider,request)}/comm.log'
        if not await aiofiles.os.path.exists(filename):
            await ws.send('no')
            return

        await ws.send(f"START") # send something to ack the connection opened

        sanic.log.logger.debug(f"streamHandler start {filename}")

        if request.app.config.RUNMODE > MODE_PUBLIC:
            statinfo = await aiofiles.os.stat(filename)
            if statinfo.st_size < 10000:
                start = 0
            else:
                start = statinfo.st_size - 10000

            commFile = await aiofiles.open(filename, 'rb')
            if which == 'init':
                await commFile.seek(start, 0)
                data = await commFile.read()
                if data:
                    await ws.send(data.decode('utf-8', errors='ignore'))
            else:
                await commFile.seek(0, 2)
          
            try:
                row = await getLatestCall(request, glider, limit=3)
                for i in range(len(row)-1, -1, -1):
                    await ws.send(f"NEW={dumps(row[i]).decode('utf-8')}")
            except:
                pass

        (tU, _) = getTokenUser(request)
        
        prev_db_t = time.time()
        if tU and request.app.config.RUNMODE > MODE_PUBLIC:
            dbfile = f'{gliderPath(glider,request)}/sg{glider:03d}.db'
            if await aiofiles.os.path.exists(dbfile):
                conn = await aiosqlite.connect('file:' + dbfile + '?mode=ro', uri=True)
                conn.row_factory = rowToDict 
                if which == 'history' or which == 'init':
                    (rows, prev_db_t) = await getChatMessages(request, glider, 0, conn)
                    if rows:
                        await ws.send(f"CHAT={dumps(rows).decode('utf-8')}")
            else:
                conn = None

        socket = zmq.asyncio.Context().socket(zmq.SUB)
        socket.setsockopt(zmq.LINGER, 0)
        socket.connect(request.app.config.WATCH_IPC)
        socket.setsockopt(zmq.SUBSCRIBE, (f"{glider:03d}-").encode('utf-8'))

        
        prev = ""
        while True:
            try:
                msg = await socket.recv_multipart()
                topic = msg[0].decode('utf-8')
                body  = msg[1].decode('utf-8')

                if 'chat' in topic and tU and request.app.config.RUNMODE > MODE_PUBLIC:
                    if conn == None and await aiofiles.os.path.exists(dbfile):
                        conn = await aiosqlite.connect('file:' + dbfile + '?mode=ro', uri=True)
                        conn.row_factory = rowToDict 
                        
                    if conn:
                        (rows, prev_db_t) = await getChatMessages(request, glider, prev_db_t, conn)
                        if rows:
                            await ws.send(f"CHAT={dumps(rows).decode('utf-8')}")

                elif 'comm.log' in topic and request.app.config.RUNMODE > MODE_PUBLIC:
                    data = (await commFile.read()).decode('utf-8', errors='ignore')
                    if data:
                        await ws.send(data)
                elif 'urls' in topic:
                    # m = loads(body)
                    await ws.send(f"NEW={body}")
                elif 'file' in topic and request.app.config.RUNMODE > MODE_PUBLIC:
                    m = loads(body) 
                    
                    async with aiofiles.open(m['full'], 'rb') as file:
                        body = (await file.read()).decode('utf-8', errors='ignore')
                        m.update( { "body": body } )
                        await ws.send(f"FILE={dumps(m).decode('utf-8')}")
                elif 'file-cmdfile' in topic:
                    directive = await summary.getCmdfileDirective(cmdfilename)
                    await ws.send(f"CMDFILE={directive}")
                else:
                    sanic.log.logger.info(f"unhandled topic {topic}")

            except BaseException as e: # websockets.exceptions.ConnectionClosed:
                sanic.log.logger.info(f'ws connection closed {e}')
                await ws.close()
                socket.close()
                return


    # not protected by decorator - buildAuthTable only returns authorized missions
    @app.websocket('/watch/<mask:str>')
    # description: stream real-time glider summary state (call status, cmdfile directive)
    # parameters: mission
    ## @authorized(protections=['pilot'])
    async def watchHandler(request: sanic.Request, ws: sanic.Websocket, mask: str):

        sanic.log.logger.debug("watchHandler start")
        opTable = await buildAuthTable(request, mask)
        await ws.send(f"START") # send something to ack the connection opened

        socket = zmq.asyncio.Context().socket(zmq.SUB)
        socket.setsockopt(zmq.LINGER, 0)
        socket.connect(request.app.config.WATCH_IPC)
        socket.setsockopt(zmq.SUBSCRIBE, b'')
        sanic.log.logger.info('context opened')

        while True:
            try:
                msg = await socket.recv_multipart()
                topic = msg[0].decode('utf-8')
                body  = msg[1].decode('utf-8')

                pieces = topic.split('-', maxsplit=1)
                glider = int(pieces[0])
                topic  = pieces[1]

                m = next(filter(lambda d: d['glider'] == glider and d['path'] == '', opTable), None)
                if m is None: # must not be authorized
                    continue

                if 'cmdfile' in topic:
                    cmdfile = f"sg{glider:03d}/cmdfile"
                    directive = await summary.getCmdfileDirective(cmdfile)
                    sanic.log.logger.debug(f"watch {glider} cmdfile modified")
                    await ws.send(f"CMDFILE={glider:03d},{directive}")
                elif 'urls' in topic:
                    try:
                        m = loads(body)
                        if 'glider' not in m:
                            m.update({ "glider": glider} ) # in case it's not in the payload (session), watch payloads must always include it
                        await ws.send(f"NEW={dumps(m).decode('utf-8')}")
                    except Exception as e:
                        sanic.log.logger.info(f"watchHandler {e}")
            except BaseException as e: # websockets.exceptions.ConnectionClosed:
                sanic.log.logger.info(f'ws connection closed {e}')
                await ws.close()
                socket.close()
                return

    @app.listener("after_server_start")
    async def initApp(app, loop):
        await buildMissionTable(app)
        await buildUserTable(app)

        sanic.log.logger.info(f'STARTING runMode {modeNames[app.config.RUNMODE]}')

    

    @app.middleware('request')
    async def checkRequest(request):
        
        if request.app.config.RUNMODE != MODE_PRIVATE and ('FQDN' in request.app.config and request.app.config.FQDN and request.app.config.FQDN != '' and request.app.config.FQDN not in request.headers['host']):
            sanic.log.logger.info(f"request for {request.headers['host']} blocked for lack of FQDN {request.app.config.FQDN}")
            return sanic.response.text('not found', status=502)
        if request.app.config.FORWARDED_SECRET and not request.forwarded:
            return sanic.response.text('Not Found', status=502)
       
        if 'secret' in request.forwarded:
            del request.forwarded['secret']

        return None


#
#  setup / config file readers
#

async def buildUserTable(app):

    if await aiofiles.os.path.exists(app.config.USERS_FILE):
        async with aiofiles.open(app.config.USERS_FILE, "r") as f:
            d = await f.read()
            try:
                x = yaml.safe_load(d)
            except Exception as e:
                sanic.log.logger.info(f"users parse error {e}")
                x = {}
    else:
        x = {}

    userDictKeys = [ "groups", "password" ]

    dflts = None
    for user in list(x.keys()):
        if user == 'default':
            dflts = x[user]
            del x[user]
            continue
        
        for uk in userDictKeys:
            if uk not in x[user].keys():
                x[user].update( { uk: dflts[uk] if dflts and uk in dflts else None } )

    app.ctx.userTable = x
    return x

async def buildMissionTable(app, config=None):
    if config == None:
        config = app.config

    if 'SINGLE_MISSION' in config and config.SINGLE_MISSION:
        sanic.log.logger.info(f'building table for single mission {config.SINGLE_MISSION}')
        pieces = config.SINGLE_MISSION.split(':')
        x = { 'missions': { pieces[0]: { 'abs': pieces[1] } } }
    else: 
        if await aiofiles.os.path.exists(config['MISSIONS_FILE']):
            async with aiofiles.open(config['MISSIONS_FILE'], "r") as f:
                d = await f.read()
                x = yaml.safe_load(d)
        else:
            x = {}

    if 'includes' in x:
        missionTable = []
        if 'organization' in x and app:
            app.ctx.organization = x['organization']

        for group in list(x['includes'].keys()):     
            c = { 'MISSIONS_FILE': x['includes'][group]['missions'] }
            tbl = await buildMissionTable(None, config=c)
            for k in tbl:
                if 'abs' in k and k['abs']:
                    k['abs'] = x['includes'][group]['root'] + '/' + k['abs']
                elif 'path' in k and k['path']:
                    k['abs'] = x['includes'][group]['root'] + f"/sg{k['glider']:03d}/{k['path']}"
                else:
                    k['abs'] = x['includes'][group]['root'] + f"/sg{k['glider']:03d}"


            missionTable = missionTable + tbl

        if app:
            app.ctx.missionTable = missionTable

        print(missionTable)
        return missionTable

    if 'organization' not in x:
        x['organization'] = {}
    if 'missions' not in x:
        x['missions'] = []
    if 'endpoints' not in x:
        x['endpoints'] = {}
    if 'controls' not in x:
        x['controls'] = {}

    orgDictKeys = ["orgname", "orglink", "text", "contact", "email"]
    for ok in orgDictKeys:
        if ok not in x['organization'].keys():
            x['organization'].update( { ok: None } )


    missionDictKeys = [ "glider", "path", "abs", "mission", "users", "pilotusers", "groups", "pilotgroups", 
                        "started", "ended", "planned", 
                        "orgname", "orglink", "contact", "email", 
                        "project", "link", "comment", "reason", "endpoints",
                        "sa", "also", "kml", 
                      ]
    
    dflts         = None
    mode_dflts    = None
    missions = []
    gliders = []
    for k in list(x['missions'].keys()):
        if k == 'defaults':
            dflts = x['missions'][k]
            del x['missions'][k]
            continue

        if 'defaults' in k:
            if k == (modeNames[config.RUNMODE] + 'defaults'):
                mode_dflts = x['missions'][k]

            del x['missions'][k]
            continue

        pieces = k.split('/')
        if len(pieces) == 1:
            path = None
        else:
            path = pieces[1]

        try:
            glider = int(pieces[0][2:])
            if glider in gliders:
                x['missions'][k].update({ "glider":glider, "path":path, "default":False })
            else:
                x['missions'][k].update({ "glider":glider, "path":path, "default":True })

            gliders.append(glider)

            for mk in missionDictKeys:
                if mk not in x['missions'][k].keys():
                    if mode_dflts and mk in mode_dflts:
                        x['missions'][k].update( { mk: mode_dflts[mk] })
                    elif dflts and mk in dflts:
                        x['missions'][k].update( { mk: dflts[mk] })
                    elif mk in x['organization']:
                        x['missions'][k].update( { mk: x['organization'][mk] })
                    else:
                        x['missions'][k].update( { mk: None })

            if x['missions'][k]['mission'] == None and path is not None:
                x['missions'][k]['mission'] = path

            missions.append(x['missions'][k])
        except Exception as e:
            sanic.log.logger.info(f"error on key {k}, {e}")
            continue 
       
    endpointsDictKeys = [ "modes", "users", "groups", "requirepilot" ]
    dflts = None
    for k in list(x['endpoints'].keys()):
        if k == 'defaults':
            dflts = x['endpoints'][k]
            del x['endpoints'][k]
            continue    

        for ek in endpointsDictKeys:
            if ek not in x['endpoints'][k].keys():
                if dflts and ek in dflts:
                    x['endpoints'][k].update( { ek: dflts[ek] })
                else:
                    x['endpoints'][k].update( { ek: None } )
                    
    if dflts:
        for k in protectableRoutes:
            if k not in x['endpoints'].keys():
                x['endpoints'][k] = dict.fromkeys(endpointsDictKeys)
                x['endpoints'][k].update( dflts )        

    for k in x['controls'].keys():
        for da in x['controls'][k].keys():
            for index,exp in enumerate(x['controls'][k][da]):
                x['controls'][k][da][index] = re.compile(exp, re.IGNORECASE)

    if app:
        app.ctx.missionTable = missions
        app.ctx.organization = x['organization']
        app.ctx.endpoints = x['endpoints']
        app.ctx.controls = x['controls']

    return missions
 
async def buildAuthTable(request, mask):
    opTable = []
    for m in request.app.ctx.missionTable:
        status = checkGliderMission(request, m['glider'], m['mission'])
        if status == PERM_REJECT:
            continue

        path    = m['path'] if m['path'] else ""
        mission = m['mission'] if m['mission'] else ''
        opTable.append({ "mission": mission, "glider": m['glider'], "path": path })

    return opTable

async def buildDivePlotList(path, dive):
    exts = [".webp", ".div"] 
    plots = { ".webp": [], ".div": [] }
    p = Path(path)
    p = p / 'plots' 
    
    async for fpath in p.glob(f"dv{dive:04d}_*.*"):
        if fpath.suffix in exts:
            x = parse('dv{}_{}.{}', fpath.name)
            plot = x[1] 
            plots[fpath.suffix].append(plot)
    
    return (plots[".webp"], plots[".div"])
 
async def buildMissionPlotList(path):
    plots = { "eng": { ".webp": [], ".div": [] }, "sg": { ".webp": [], ".div": [] } }
    maxdv = -1
    p = Path(path)
    p = p / 'plots' 
    exts = ['.div', '.webp']
    for prefix in ['eng', 'sg']:
        async for fpath in p.glob(f"{prefix}_*.*"):
            # if prefix == 'sg' and '_section_' in fpath.name:
            #    continue

            if fpath.suffix in exts:
                plot = '_'.join(fpath.stem.split('_')[1:])
                plots[prefix][fpath.suffix].append(plot)

    return (plots['eng']['.webp'], plots['sg']['.webp'], plots['eng']['.div'], plots['sg']['.div'])

#
# background main task 
#

async def checkFilesystemChanges(files):
    mods = []
    for f in files:
        if await aiofiles.os.path.exists(f['full']):
            n = await aiofiles.os.path.getctime(f['full'])
            if n > f['ctime']:
                f['ctime'] = n
                mods.append(f)

    return mods

async def configWatcher(app):
    socket = zmq.asyncio.Context().socket(zmq.SUB)
    socket.setsockopt(zmq.LINGER, 0)
    socket.connect(app.config.WATCH_IPC)
    sanic.log.logger.info('opened context for configWatcher')
    socket.setsockopt(zmq.SUBSCRIBE, (f"000-file-").encode('utf-8'))
    while True:
        try:
            msg = await socket.recv_multipart()
            sanic.log.logger.info(msg[1])
            topic = msg[0].decode('utf-8')
            if 'missions' in topic:
                await buildMissionTable(app)
            elif 'users' in topic:
                await buildUserTable(app)

        except BaseException as e: # websockets.exceptions.ConnectionClosed:
            socket.close()
            return

        # app.m.name.restart() 

async def notifier(config):
    msk = os.umask(0o000)
    ctx = zmq.asyncio.Context()
    socket = ctx.socket(zmq.PUB)
    socket.bind(config.WATCH_IPC)
    socket.setsockopt(zmq.SNDTIMEO, 200)
    socket.setsockopt(zmq.LINGER, 0)

    inbound = ctx.socket(zmq.PULL)
    inbound.bind(config.NOTIFY_IPC)
    os.umask(msk)

    missions = await buildMissionTable(None, config=config)
    files = [ ]
    for f in [ 'missions.yml', 'users.yml' ]:
        files.append( { 'glider': 0, 'full': f, 'file': f, 'ctime': 0 } )

    for m in missions:
        if m['path'] == None:
            for f in ["comm.log", "cmdfile", "science", "targets", "scicon.sch", "tcm2mat.cal", "sg_calib_constants.m", "pdoscmds.bat"]:
                fname = f"sg{m['glider']:03d}/{f}"
                files.append( { "glider": m['glider'], "full": fname, "file": f, "ctime": 0 } )

    await checkFilesystemChanges(files) # load initial mod times

    while True:
        stat = await inbound.poll(2000)
        if stat:
            r = await inbound.recv_multipart()
            sanic.log.logger.info("notifier got {r[0].decode('utf-8')}")
            await socket.send_multipart(r)

        mods = await checkFilesystemChanges(files)
        for f in mods:
            msg = [(f"{f['glider']:03d}-file-{f['file']}").encode('utf-8'), dumps(f)]
            await socket.send_multipart(msg)

def backgroundWatcher(config):
    loop = asyncio.get_event_loop()
    loop.create_task(notifier(config))
    loop.run_forever()

async def mainProcessReady(app):
    print('main process ready')
    app.manager.manage("backgroundWatcher", backgroundWatcher, { "config": app.config })

async def mainProcessStop(app):
    os.remove(app.config.WATCH_IPC[6:])
    os.remove(app.config.NOTIFY_IPC[6:])

def createApp(overrides: dict) -> sanic.Sanic:

    d = { "missionTable": [],   # list of missions (each a dict)
          "userTable": {},      # dict (keyed by username) of dict
          "organization": {},
          "endpoints": {},   # dict of url level protections (keyed by url name)
        }

    app = sanic.Sanic("SGpilot", ctx=SimpleNamespace(**d), dumps=dumps)

    # config values loaded from SANIC_ environment variables first
    # then get overridden by anything from command line
    # then get filled in by hard coded defaults as below if
    # not previously provided
    app.config.update(overrides)

    if 'SECRET' not in app.config:
        app.config.SECRET = secrets.token_hex()
    if 'MISSIONS_FILE' not in app.config:
        app.config.MISSIONS_FILE = "missions.yml"
    if 'USERS_FILE' not in app.config:
        app.config.USERS_FILE = "users.yml"
    if 'FQDN' not in app.config:
        app.config.FQDN = None;
    if 'USER' not in app.config:
        app.config.USER = os.getlogin()
    if 'SINGLE_MISSION' not in app.config:
        app.config.SINGLE_MISSION = None
    if 'WEATHERMAP_APPID' not in app.config:
        app.config.WEATHERMAP_APPID = ''

    app.config.TEMPLATING_PATH_TO_TEMPLATES=f"{sys.path[0]}/html"

    attachHandlers(app)

    app.add_task(configWatcher)

    return app

def usage():
    print("vis.py glider mission visualization server")
    print("  --mission=|-m      [sgNNN:/abs/mission/path] run in single mission mode")
    print("  --mode=|-o         private|pilot|public")
    print("  --port=|-p         portNumber (ex. 20000)")
    print("  --root=|-r         baseDirectory (ex. /home/seaglider)")
    print("  --domain=|-d       fully-qualified-domain-name (optional)")
    print("  --missionsfile=|-f missions.yml file (default ROOT/missions.yml)")
    print("  --usersfile=|-u    users.yml file (default ROOT/users.yml)")
    print("  --certs=|-c        certificate file for SSL")
    print("  --ssl|-s           boolean enable SSL")
    print("  --inspector|-i     boolean enable SANIC inspector")
    print("  --nochat           boolean run without chat support")
    print("  --nosave           boolean run without save support")
    print()
    print("  Environment variables: ")
    print("    SANIC_CERTPATH, SANIC_ROOTDIR, SANIC_SECRET, ")
    print("    SANIC_MISSIONS_FILE, SANIC_USERS_FILE, SANIC_FQDN, ")
    print("    SANIC_USER, SANIC_SINGLE_MISSION")

if __name__ == '__main__':

    root = os.getenv('SANIC_ROOTDIR')
    runMode = MODE_PRIVATE
    port = 20001
    ssl = False
    certPath = os.getenv("SANIC_CERTPATH") 
    noSave = False
    noChat = False

    overrides = {}

    try:
        opts, args = getopt.getopt(sys.argv[1:], 'm:p:o:r:d:f:u:c:sih', ["mission=", "port=", "mode=", "root=", "domain=", "missionsfile=", "usersfile=", "certs=", "ssl", "inspector", "help", "nosave", "nochat"])
    except getopt.GetoptError as err:
        print(err)
        sys.exit(1)

    for o,a in opts:
        if o in ['-p', '--port']:
            port = int(a)
        elif o in ['-o', '--mode']:
            runMode = runModes[a]
        elif o in ['-r', '--root']:
            root = a
        elif o in ['-d', '--domain']:
            overrides['FQDN'] = a
        elif o in ['-f', '--missionsfile']:
            overrides['MISSIONS_FILE'] = a
        elif o in ['-u', '--usersfile']:
            overrides['USERS_FILE'] = a
        elif o in ['--nosave']:
            noSave = True
        elif o in ['--nochat']:
            noChat = True
        elif o in ['-c', '--certs']:
            certPath = a
        elif o in ['-s', '--ssl']:
            ssl = True
        elif o in ['-i', '--inspector']:
            overrides['INSPECTOR'] = True
        elif o in ['-m', '--mission']:
            overrides['SINGLE_MISSION'] = a
            pieces = a.split(':')
            if len(pieces) != 2:
                print("-m sgNNN:/abs/mission/path")
                sys.exit(1)
        elif o in ['-h', '--help']:
            usage()
            sys.exit(1)
                 
    if root is None:
        root = '/home/seaglider'

    if root is not None:
        os.chdir(os.path.expanduser(root))

    # we always load RUNMODE based on startup conditions
    overrides['RUNMODE'] = runMode
    overrides['NO_SAVE'] = noSave
    overrides['NO_CHAT'] = noChat

    overrides['NOTIFY_IPC'] = f"ipc:///tmp/sanic-{os.getpid()}-notify.ipc" 
    overrides['WATCH_IPC']  = f"ipc:///tmp/sanic-{os.getpid()}-watch.ipc" 

    # set a random SECRET here to be shared by all instances
    # running on this main process. Restarting the process will
    # mean all session tokens are invalidated 
    # use an environment variable SANIC_SECRET to 
    # make sessions persist across processes
    if "SANIC_SECRET" not in os.environ:
        overrides["SECRET"] = secrets.token_hex()

    loader = sanic.worker.loader.AppLoader(factory=partial(createApp, overrides))
    app = loader.load()
    app.register_listener(mainProcessReady, "main_process_ready")
    app.register_listener(mainProcessStop, "main_process_stop")

    if ssl:
        certs = {
            "cert": f"{certPath}/fullchain.pem",
            "key": f"{certPath}/privkey.pem",
            # "password": "for encrypted privkey file",   # Optional
        }
        app.prepare(host="0.0.0.0", port=port, ssl=certs, access_log=True, debug=False, fast=True)

        sanic.Sanic.serve(primary=app, app_loader=loader)
        #app.run(host="0.0.0.0", port=443, ssl=ssl, access_log=True, debug=False)
    else:
        app.prepare(host="0.0.0.0", port=port, access_log=True, debug=False, fast=True)
        sanic.Sanic.serve(primary=app, app_loader=loader)
        # app.run(host='0.0.0.0', port=port, access_log=True, debug=True, fast=True)
